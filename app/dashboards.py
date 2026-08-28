"""Dashboard rendering.

A renderer returns an RGB PIL.Image at the panel resolution; the shared pipeline
(dither -> "P" image -> inky) then displays it. See CLAUDE.md "Dashboards".

One dashboard: `guest_wifi` — a web-style "bento" of neubrutalist sticker cards
(SSID, password, signal, QR) in JetBrains Mono. The card fills are dithered into a
soft riso texture, then crisp black outlines + text + QR are composited on top, so
it MUST be shown with dither=NONE (POST /display/dashboard?name=guest_wifi&dither=NONE).

Landscape. The layout is authored at a 1600×1200 reference and scaled uniformly to
`self.size` (the active panel's resolution), centred, so it renders on both the
13.3" (1600×1200 → scale 1, pixel-identical) and the 7.3" (800×480 → scale 0.4,
centred with white side margins since its 5:3 aspect is wider than the 4:3
reference). Text/QR are drawn at the scaled sizes, so they stay crisp — nothing is
resampled.
"""

from __future__ import annotations

import glob
import re

import qrcode
from PIL import Image, ImageChops, ImageDraw, ImageFont

from .config import Settings

BLACK = (0, 0, 0)
WHITE = (255, 255, 255)
BLUE = (0, 0, 255)
RED = (255, 0, 0)

_FONT_CACHE: dict[tuple[int, str, str], ImageFont.FreeTypeFont] = {}


def _font(size: int, weight: str = "Bold", family: str = "Lato") -> ImageFont.FreeTypeFont:
    """Load <family>-<weight> from any system font dir; fall back to Lato, then DejaVu."""
    key = (size, weight, family)
    cached = _FONT_CACHE.get(key)
    if cached is not None:
        return cached
    dejavu = "DejaVuSans-Bold.ttf" if weight in ("Black", "ExtraBold", "Bold", "SemiBold") else "DejaVuSans.ttf"
    patterns = [
        f"/usr/share/fonts/**/{family}-{weight}.ttf",
        f"/usr/share/fonts/**/{family}-{weight}.otf",
        f"/usr/share/fonts/**/{family}*{weight}*.ttf",
        f"/usr/share/fonts/**/Lato-{weight}.ttf",
        "/usr/share/fonts/**/Lato-Bold.ttf",
        f"/usr/share/fonts/**/{dejavu}",
    ]
    font: ImageFont.FreeTypeFont | None = None
    for pat in patterns:
        for hit in sorted(glob.glob(pat, recursive=True)):
            try:
                font = ImageFont.truetype(hit, size)
                break
            except OSError:
                font = None
        if font is not None:
            break
    if font is None:
        font = ImageFont.load_default()
    _FONT_CACHE[key] = font
    return font


def _wifi_payload(ssid: str, password: str, security: str) -> str:
    r"""The de-facto Wi-Fi QR string. Backslash-escape \ ; , : " inside values."""
    def esc(v: str) -> str:
        return re.sub(r'([\\;,:"])', r"\\\1", v)

    sec = (security or "WPA").strip().upper()
    if sec in ("", "NONE", "OPEN", "NOPASS"):
        return f"WIFI:T:nopass;S:{esc(ssid)};;"
    return f"WIFI:T:{sec};S:{esc(ssid)};P:{esc(password)};;"


class DashboardRenderer:
    def __init__(self, settings: Settings, size: tuple[int, int] | None = None):
        self.settings = settings
        # Defaults to the active panel's resolution; main.py also refreshes this from
        # the live driver after init_driver().
        self.size = size or tuple(settings.panel_spec.resolution)

    def available(self) -> list[str]:
        return ["guest_wifi"]

    def render(self, name: str) -> Image.Image:
        if name == "guest_wifi":
            return self._guest_wifi()
        raise NotImplementedError(
            f"dashboard {name!r} is not implemented yet — see app/dashboards.py and CLAUDE.md"
        )

    # -- helpers -------------------------------------------------------------
    def _make_qr(self, payload: str, target_px: int) -> Image.Image:
        qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M, border=4)
        qr.add_data(payload)
        qr.make(fit=True)
        # px per module to land near target_px (border=4 => +8 modules). Floor of 4
        # keeps it scannable on the small 7.3" while still fitting its QR card; on the
        # 13.3" the computed value is already well above 4, so output is unchanged.
        qr.box_size = max(4, round(target_px / (qr.modules_count + 8)))
        return qr.make_image(fill_color="black", back_color="white").get_image().convert("RGB")

    # -- renderer ------------------------------------------------------------
    def _guest_wifi(self) -> Image.Image:
        """Web-dashboard 'bento', all JetBrains Mono. Two-pass: the card FILLS +
        shadows are dithered first (colour fills become soft dithered texture), then
        crisp black outlines / text / QR are composited on top — show with dither=NONE.
        Each shadow shares the card's rounding: corners are X at the top-left &
        bottom-right and X+Y at the top-right & bottom-left, so the card's tight
        corner flows tangentially into the softer shadow corner (see PASS 1)."""
        from .dithering import render_for_inky

        w, h = self.size
        s = self.settings

        # Author at a 1600×1200 reference and scale uniformly to the panel, centred.
        # k==1 on the 13.3" (pixel-identical); ~0.4 on the 7.3" (centred, white side
        # margins, since its 5:3 is wider than the 4:3 reference). px()/py() place a
        # reference point; sv() scales a length (radius/stroke/font/gap); rbox() maps a
        # reference box. Everything is drawn at the scaled size, so text/QR stay crisp.
        RW, RH = 1600, 1200
        k = min(w / RW, h / RH)
        ox, oy = (w - RW * k) / 2, (h - RH * k) / 2

        def px(x):
            return round(ox + x * k)

        def py(y):
            return round(oy + y * k)

        def sv(v, lo=1):
            return max(lo, round(v * k))

        def rbox(x0, y0, x1, y1):
            return (px(x0), py(y0), px(x1), py(y1))

        def mono(sz: int, wt: str = "Bold"):
            return _font(sv(sz, 8), wt, "JetBrainsMono")

        if not s.guest_wifi_ssid:
            img = Image.new("RGB", self.size, WHITE)
            ImageDraw.Draw(img).text((w // 2, h // 2), "GUEST_WIFI_SSID is not set",
                                     fill=RED, font=mono(64), anchor="mm")
            return img

        ACC = BLUE
        CARD_R, SHADOW = sv(26), sv(14)  # card corner radius; shadow depth (drop offset)
        header = rbox(50, 48, 1550, 180)
        ssid_c = rbox(50, 204, 1064, 512)
        haslo = rbox(50, 536, 545, 1010)
        sygnal = rbox(567, 536, 1064, 1010)
        qr_c = rbox(1086, 204, 1550, 1010)
        footer = rbox(50, 1034, 1550, 1156)
        # (box, fill) — tasteful non-palette tones that DITHER into soft texture.
        # Each card gets a black drop-shadow whose corners flow out of the card's own
        # rounding + one black outline (neubrutalist sticker); see PASS 1.
        cards = [
            (header, (128, 78, 192)),   # violet
            (ssid_c, WHITE),
            (haslo, (236, 118, 72)),    # tangerine
            (sygnal, WHITE),            # white — the signal bars carry the colour
            (qr_c, WHITE),
            (footer, (108, 184, 178)),  # teal
        ]
        # Signal bars (phone-style): the `signal_level` lit bars ALL share ONE colour,
        # and that colour is chosen by the level — weak=red, medium=amber, strong=green.
        # Dithered fill (PASS 1) + crisp black outline (PASS 2), like the cards. Unlit
        # bars stay white (hollow). `signal_level` is a static placeholder — nothing
        # measures the guest signal; bump it or wire it to a real reading later.
        bars = [rbox(607 + i * 66, 906 - (46 + i * 36), 607 + i * 66 + 48, 906) for i in range(5)]
        signal_level = 4  # of 5
        bar_color = ((214, 60, 48) if signal_level <= 2       # red
                     else (240, 180, 45) if signal_level == 3  # amber
                     else (40, 158, 72))                       # green

        # PASS 1 — the shadow. NOT a naive offset duplicate (that leaves an ugly
        # stepped notch on the anti-diagonal corners). Each card's silhouette is ONE
        # rounded rect grown by SHADOW to the right and down, with corners CARD_R at the
        # top-left & bottom-right but CARD_R+SHADOW at the top-right & bottom-left. The
        # card (uniform radius CARD_R, aligned top-left) then nests inside: its top &
        # left edges sit flush (no shadow there), while its tight CARD_R corners flow
        # tangentially into the shadow's gentle CARD_R+SHADOW corners down the right &
        # bottom — the card's rounding becomes the shadow's rounding. Built as the
        # intersection (darker) of a CARD_R-rounded TL/BR rect and a (CARD_R+SHADOW)-
        # rounded TR/BL rect, because PIL can't round each corner differently in one call.
        fills = Image.new("RGB", self.size, WHITE)
        sil = Image.new("L", self.size, 0)
        for (x0, y0, x1, y1), _fill in cards:
            box = (x0, y0, x1 + SHADOW, y1 + SHADOW)
            a = Image.new("L", self.size, 0)
            ImageDraw.Draw(a).rounded_rectangle(box, radius=CARD_R, fill=255, corners=(True, False, True, False))
            b = Image.new("L", self.size, 0)
            ImageDraw.Draw(b).rounded_rectangle(box, radius=CARD_R + SHADOW, fill=255, corners=(False, True, False, True))
            sil = ImageChops.lighter(sil, ImageChops.darker(a, b))
        fills.paste((0, 0, 0), mask=sil)
        fd = ImageDraw.Draw(fills)
        for (x0, y0, x1, y1), fill in cards:
            fd.rounded_rectangle((x0, y0, x1, y1), radius=CARD_R, fill=fill)
        for i, bar in enumerate(bars):
            if i < signal_level:
                fd.rounded_rectangle(bar, radius=sv(10), fill=bar_color)
        bg = render_for_inky(fills, s, size=self.size, fit="contain", mode=None).convert("RGB")

        # PASS 2 — ONE crisp black outline per card (the shadow stays unoutlined) + text.
        d = ImageDraw.Draw(bg)
        for box, _fill in cards:
            d.rounded_rectangle(box, radius=CARD_R, outline=BLACK, width=sv(4))

        d.text((px(90), py(114)), "wi-fi // dla gości", fill=WHITE, font=mono(52), anchor="lm")
        d.ellipse(rbox(1356, 100, 1384, 128), fill=(0, 255, 0))
        d.text((px(1400), py(114)), "OPEN", fill=WHITE, font=mono(44), anchor="lm")

        d.text((px(92), py(264)), "SSID", fill=ACC, font=mono(34, "Medium"), anchor="lm")
        d.text((px(90), py(388)), s.guest_wifi_ssid, fill=BLACK, font=mono(88), anchor="lm")

        d.text((px(90), py(600)), "HASŁO", fill=BLACK, font=mono(34, "Medium"), anchor="lm")
        d.text((px(90), py(720)), "brak", fill=BLACK, font=mono(72), anchor="lm")
        d.text((px(90), py(828)), "sieć otwarta", fill=BLACK, font=mono(40), anchor="lm")

        d.text((px(607), py(600)), "SYGNAŁ", fill=BLACK, font=mono(34, "Medium"), anchor="lm")
        for bar in bars:
            d.rounded_rectangle(bar, radius=sv(10), outline=BLACK, width=sv(3))

        d.text((px(1318), py(278)), "[ scan ]", fill=ACC, font=mono(40, "Medium"), anchor="mm")
        qr = self._make_qr(
            _wifi_payload(s.guest_wifi_ssid, s.guest_wifi_password, s.guest_wifi_security), sv(420))
        bg.paste(qr, (px(1318) - qr.width // 2, py(640) - qr.height // 2))

        d.text((px(90), py(1095)), "> zeskanuj kod, aby dołączyć do sieci", fill=BLACK, font=mono(40), anchor="lm")
        return bg
