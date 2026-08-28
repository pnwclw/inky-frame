"""Turn an arbitrary photo into something the Inky Impression 13.3" can show.

The important subtlety here is AVOIDING A DOUBLE DITHER:

  * epaper-dithering already maps the photo down to the 6 Spectra colours with a
    high-quality error-diffusion pass.
  * inky.set_image() will, for any non-"P" image, run ITS OWN dithering pass
    (PIL's `im.convert("P", dither=True, palette)`), undoing/degrading our work.

inky's escape hatch: if we hand it an image that is already mode "P", it uses the
raw palette indices directly with NO second pass. So we:

  1. dither the photo to 6 colours with epaper-dithering (-> RGB), then
  2. quantize that result onto inky's exact palette/index order with dither=NONE
     (a lossless 6->6 nearest-colour relabel), producing a "P" image, and
  3. hand that "P" image to inky.set_image().

Index order must match inky's DESATURATED_PALETTE: 0=black 1=white 2=yellow
3=red 4=blue 5=green. We read it from the live driver when available so we stay
correct even if Pimoroni reorders it; otherwise we use the constant below.
"""

from __future__ import annotations

import logging

from PIL import Image

from epaper_dithering import ColorScheme, DitherMode, dither_image

from .config import Settings

log = logging.getLogger(__name__)

# inky EL133UF1 DESATURATED_PALETTE order (first 6 entries), pure colours.
INKY_PALETTE_RGB: list[tuple[int, int, int]] = [
    (0, 0, 0),        # 0 black
    (255, 255, 255),  # 1 white
    (255, 255, 0),    # 2 yellow
    (255, 0, 0),      # 3 red
    (0, 0, 255),      # 4 blue
    (0, 255, 0),      # 5 green
]


# Every dithering algorithm the installed epaper-dithering exposes, discovered at
# import so the list tracks the library version. Names match DitherMode members.
AVAILABLE_DITHER_MODES: tuple[str, ...] = tuple(
    sorted(m for m in dir(DitherMode) if m.isupper() and not m.startswith("_"))
)
_DITHER_MODE_SET = frozenset(AVAILABLE_DITHER_MODES)


def resolve_dither_mode(name: str):
    """Map a mode name (case-insensitive) to a DitherMode, or raise ValueError
    listing the valid options."""
    key = name.strip().upper()
    if key not in _DITHER_MODE_SET:
        raise ValueError(
            f"Unknown dither mode {name!r}. Available: {', '.join(AVAILABLE_DITHER_MODES)}"
        )
    return getattr(DitherMode, key)


def _palette_image(palette_rgb: list[tuple[int, int, int]]) -> Image.Image:
    flat: list[int] = []
    for r, g, b in palette_rgb:
        flat += [int(r), int(g), int(b)]
    flat += [0, 0, 0] * (256 - len(palette_rgb))  # pad to 256 colours
    pal = Image.new("P", (1, 1))
    pal.putpalette(flat)
    return pal


def _cover(img: Image.Image, size: tuple[int, int]) -> Image.Image:
    """Scale + centre-crop to completely fill `size` (may crop edges)."""
    tw, th = size
    w, h = img.size
    scale = max(tw / w, th / h)
    nw, nh = round(w * scale), round(h * scale)
    img = img.resize((nw, nh), Image.LANCZOS)
    left, top = (nw - tw) // 2, (nh - th) // 2
    return img.crop((left, top, left + tw, top + th))


# Degrees CLOCKWISE (what a "rotate right" button means) -> the PIL transpose that
# does it. PIL's own names count counter-clockwise, hence the flip.
ROTATIONS: dict[int, int | None] = {
    0: None,
    90: Image.ROTATE_270,
    180: Image.ROTATE_180,
    270: Image.ROTATE_90,
}


def apply_rotation(img: Image.Image, rotate: int) -> Image.Image:
    """Rotate the PHOTO before it is placed on the canvas. Independent of the frame's
    mounting: that turns the canvas, this turns the picture inside it."""
    transpose = ROTATIONS.get(int(rotate) % 360)
    return img if transpose is None else img.transpose(transpose)


def rotated_size(size: tuple[int, int], rotate: int) -> tuple[int, int]:
    """The photo's size after `rotate`. `fit=auto` compares aspect ratios, so it has to
    measure the rotated photo, not the original."""
    return (size[1], size[0]) if int(rotate) % 180 else (size[0], size[1])


def working_canvas(size: tuple[int, int], orientation: str) -> tuple[int, int]:
    """The canvas a photo is actually laid out on. The panel is always physically
    landscape W×H; a frame MOUNTED vertically composes on the rotated H×W canvas and
    the finished image is rotated back at the end of render_for_inky()."""
    return (size[1], size[0]) if orientation == "portrait" else size


def crop_loss(image_size: tuple[int, int], canvas_size: tuple[int, int]) -> float:
    """Fraction of the photo that `fit=cover` would crop away, 0.0-1.0.

    cover scales until both axes are covered and centre-crops the overflow on the one
    axis that overflows, so the loss is entirely down to the aspect mismatch:
    a 3:4 photo on a 4:3 canvas loses 1 - 0.75/1.333 = 44%. Used by `fit=auto`
    (app/prefs.py) to decide when cover would throw away too much.
    """
    iw, ih = image_size
    cw, ch = canvas_size
    if not (iw and ih and cw and ch):
        return 0.0
    image_aspect, canvas_aspect = iw / ih, cw / ch
    return 1.0 - min(image_aspect, canvas_aspect) / max(image_aspect, canvas_aspect)


def _contain(img: Image.Image, size: tuple[int, int], bg=(255, 255, 255)) -> Image.Image:
    """Scale to fit inside `size`, padding with white (no cropping)."""
    tw, th = size
    w, h = img.size
    scale = min(tw / w, th / h)
    nw, nh = round(w * scale), round(h * scale)
    img = img.resize((nw, nh), Image.LANCZOS)
    canvas = Image.new("RGB", size, bg)
    canvas.paste(img, ((tw - nw) // 2, (th - nh) // 2))
    return canvas


def default_crop(
    image_size: tuple[int, int], canvas_size: tuple[int, int], fit: str
) -> list[float]:
    """The crop rectangle a `cover` or `contain` preset means, in image pixels.

    `cover` is the largest canvas-shaped rectangle that fits INSIDE the image (so the
    overhang is cropped); `contain` is the smallest one that CONTAINS it (so the
    shortfall becomes white margin). Everything a person can drag to sits between the
    two, which is why both are expressed as the same thing — a rectangle."""
    iw, ih = image_size
    cw, ch = canvas_size
    if not (iw and ih and cw and ch):
        return [0.0, 0.0, float(iw or 1), float(ih or 1)]
    image_aspect, canvas_aspect = iw / ih, cw / ch
    wider = image_aspect > canvas_aspect
    if fit == "contain":
        w, h = (iw, iw / canvas_aspect) if wider else (ih * canvas_aspect, ih)
    else:
        w, h = (ih * canvas_aspect, ih) if wider else (iw, iw / canvas_aspect)
    return [(iw - w) / 2, (ih - h) / 2, w, h]


def _crop_to(img: Image.Image, size: tuple[int, int], box: list[float]) -> Image.Image:
    """Map an explicit crop rectangle onto the canvas.

    The rectangle is in image pixels and MAY extend past the edges — that is how
    `contain` and every dragged position between the presets are expressed. Whatever
    falls outside the photo becomes white, so one code path covers all of them."""
    tw, th = size
    x, y, w, h = box
    canvas = Image.new("RGB", (tw, th), (255, 255, 255))
    if w <= 0 or h <= 0:
        return canvas
    sx, sy = tw / w, th / h
    left, top = max(0.0, x), max(0.0, y)
    right, bottom = min(float(img.width), x + w), min(float(img.height), y + h)
    if right <= left or bottom <= top:
        return canvas  # dragged entirely off the photo
    src = img.crop((round(left), round(top), round(right), round(bottom)))
    src = src.resize(
        (max(1, round((right - left) * sx)), max(1, round((bottom - top) * sy))),
        Image.LANCZOS,
    )
    canvas.paste(src, (round((left - x) * sx), round((top - y) * sy)))
    return canvas


def _resolve_palette(settings: Settings):
    """A measured palette constant overrides the generic ColorScheme."""
    if settings.dither_palette:
        import epaper_dithering as ed

        pal = getattr(ed, settings.dither_palette, None)
        if pal is not None:
            return pal
        log.warning(
            "Unknown dither_palette %r; falling back to ColorScheme.%s",
            settings.dither_palette,
            settings.dither_color_scheme,
        )
    return getattr(ColorScheme, settings.dither_color_scheme)


def render_for_inky(
    image: Image.Image,
    settings: Settings,
    *,
    size: tuple[int, int] | None = None,
    fit: str = "cover",
    orientation: str = "landscape",
    rotate: int = 0,
    crop: list[float] | None = None,
    inky_palette: list[tuple[int, int, int]] | None = None,
    mode: str | None = None,
) -> Image.Image:
    """Return a mode-"P" image ready to pass straight to inky.set_image().

    `size` is the panel's native landscape resolution W×H (defaults to the active
    PANEL's — 800×480 on the 7.3", 1600×1200 on the 13.3"). For orientation=
    "portrait" (frame mounted vertically) we lay the photo out on a rotated H×W
    canvas and rotate the finished image 90° so it ends up W×H for the panel.

    `rotate` turns the PHOTO (0/90/180/270, clockwise) before it is placed — that is a
    property of the picture, where `orientation` is a property of the frame. `crop` is
    the rectangle of the ROTATED photo that lands on the canvas, in its pixels; it may
    extend past the edges, and what does becomes white. Omit it and `fit` picks one.
    """
    if size is None:
        size = tuple(settings.panel_spec.resolution)
    work = working_canvas(size, orientation)

    rgb = apply_rotation(image.convert("RGB"), rotate)
    # `crop` is the real placement — cover/contain are just the two rectangles you get
    # from the presets, so the picker can hand back anything in between.
    rgb = _crop_to(rgb, work, crop or default_crop(rgb.size, work, fit))

    dithered = dither_image(
        rgb,
        _resolve_palette(settings),
        mode=resolve_dither_mode(mode or settings.dither_mode),
        serpentine=True,
        exposure=settings.dither_exposure,
        saturation=settings.dither_saturation,
        shadows=settings.dither_shadows,
        highlights=settings.dither_highlights,
        tone=settings.dither_tone,
        gamut=settings.dither_gamut,
    )

    pal = _palette_image(inky_palette or INKY_PALETTE_RGB)
    # dither=NONE: the input already contains only the 6 colours, so this is a
    # lossless relabel into inky's index order, not a second dithering pass.
    p = dithered.convert("RGB").quantize(palette=pal, dither=Image.Dither.NONE)

    if orientation == "portrait":
        # transpose keeps the "P" mode + palette; 1200×1600 -> 1600×1200.
        p = p.transpose(Image.ROTATE_90)
    return p
