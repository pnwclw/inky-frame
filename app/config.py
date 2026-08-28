"""Runtime configuration, loaded from environment variables / a .env file.

Every field maps 1:1 to an UPPER_CASE env var (pydantic-settings is
case-insensitive), e.g. `inky_mock` <- `INKY_MOCK`. See .env.example.
"""

from __future__ import annotations

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from .panels import DEFAULT_PANEL, PanelSpec, get_panel


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- HTTP server ---
    host: str = "0.0.0.0"
    port: int = 8080

    # --- Panel selection (the one switch) ---
    # PANEL picks which Inky Impression this service drives; it derives the driver,
    # resolution, button-C GPIO, busy-wait ceiling, and Home-Assistant identity from
    # app/panels.py. Valid: "impression_73" (7.3", 800×480) or "impression_13"
    # (13.3", 1600×1200). Change this one value to switch panels.
    panel: str = DEFAULT_PANEL

    # --- Display ---
    # INKY_MOCK=1 disables all hardware access (no inky/gpiod import). The service
    # still dithers and writes the result to <output_dir>/latest.png. Use this on a
    # laptop or in CI.
    inky_mock: bool = False
    # Prefer inky.auto.auto() (reads the on-board EEPROM over I2C). If that fails we
    # fall back to constructing InkyEL133UF1() directly.
    inky_auto_detect: bool = True
    # Passed to inky's set_image(): blends the desaturated/saturated palette.
    inky_saturation: float = 0.5
    # Where rendered previews (and mock output) are written.
    output_dir: str = "/data"
    # Whether POST /display/image blocks until the (~30s) refresh finishes by
    # default. False = return immediately, refresh in the background.
    display_wait_default: bool = False
    # Override the panel's busy-wait ceiling (seconds the patched _busy_wait polls
    # BUSY before giving up). Leave unset to use the per-panel default from panels.py
    # (13.3"=50, 7.3"=40). Only a safety cap — the poll returns when the panel is done.
    busy_wait_ceiling: float | None = None

    # --- Dithering (epaper-dithering) ---
    # ColorScheme name used when dither_palette is empty. BWGBRY == 6-colour Spectra.
    dither_color_scheme: str = "BWGBRY"
    # Optional measured palette constant from epaper_dithering, e.g.
    # "SPECTRA_7_3_6COLOR_V2". When set it overrides dither_color_scheme and usually
    # gives more accurate photo tones. Leave empty to use the generic scheme.
    dither_palette: str = ""
    # DitherMode name: FLOYD_STEINBERG, ATKINSON, STUCKI, SIERRA, JARVIS_JUDICE_NINKE,
    # BURKES, SIERRA_LITE, ORDERED, NONE.
    dither_mode: str = "JARVIS_JUDICE_NINKE"
    dither_exposure: float = 1.0
    dither_saturation: float = 1.0
    dither_shadows: float = 0.0
    dither_highlights: float = 0.0
    dither_tone: str = "auto"
    dither_gamut: str = "auto"

    # --- Device prefs SEEDS (app/prefs.py) ---
    # These only seed <OUTPUT_DIR>/prefs.json the FIRST time the service runs. After
    # that the stored prefs win, because they are what Home Assistant edits — a deploy
    # must not silently revert a choice made from HA. Delete prefs.json to re-seed.
    # How the frame is physically mounted: landscape | portrait.
    default_orientation: str = "landscape"
    # What to do when a photo's aspect doesn't match the frame: auto | cover | contain.
    # auto = cover while it crops less than AUTO_FIT_MAX_CROP, else contain.
    default_fit: str = "auto"
    # Most of the photo `fit=auto` will let `cover` crop away, 0-1. At 0.30 on a 4:3
    # panel: 4:3 (0%), 3:2 (11%) and 16:9 (25%) get cover; a portrait 3:4 photo (44%)
    # and a 2:1 panorama (33%) get contain.
    auto_fit_max_crop: float = 0.30
    # What fills the canvas where the photo doesn't reach — the margin `contain`
    # leaves and the corners a free rotation exposes. One of the panel's six colours:
    # white | black | red | green | blue | yellow. Only these, because a palette colour
    # dithers to a flat block while anything else becomes a stipple.
    default_background: str = "white"

    # --- Absolute URL other machines use to reach this service ---
    # Baked into the Home Assistant MQTT `image` entity so HA can fetch the current
    # frame. Leave empty to autodetect the primary LAN address (http://<ip>:<port>).
    # Set it if the frame is reached over Tailscale/a reverse proxy instead.
    public_base_url: str = ""

    # --- Photo library (data/photos, data/renders, data/thumbs, data/library.json) ---
    # Keeps every photo the frame has been sent so it can be shown again later
    # (GET /library, POST /display/library/<id>). See app/library.py.
    library_enabled: bool = True
    # Hard cap on stored photos; the oldest are pruned past it. Budget roughly
    # ~1 MB original + ~0.3 MB render + ~10 KB thumb per photo, so 300 ~= 400 MB
    # on the 13.3" (less on the 7.3", whose renders are 800x480).
    library_max_items: int = 300
    # Originals whose long edge exceeds this multiple of the panel's long edge are
    # downscaled on the way in (2.0 -> 3200 px on the 13.3"). Keeps enough detail to
    # re-dither well without archiving full-size phone photos.
    library_original_max_scale: float = 2.0
    library_thumb_size: int = 320
    library_jpeg_quality: int = 90

    # --- POST /display/url (fetch an image by URL and show it) ---
    url_fetch_max_bytes: int = 32 * 1024 * 1024
    url_fetch_timeout: float = 15.0

    # --- Buttons (BCM GPIO numbers) ---
    # A/B/D are the same on every Impression. Button C moves per panel (25 on the
    # 13.3", 16 on the 7.3"), so its default comes from the PANEL spec below unless
    # you set BUTTON_GPIO_C explicitly. See app/panels.py.
    button_gpio_a: int = 5
    button_gpio_b: int = 6
    button_gpio_c: int = 16  # panel default; overwritten from PANEL unless set
    button_gpio_d: int = 24
    button_debounce_ms: int = 200

    # --- MQTT (connect to your existing Home Assistant / Mosquitto broker) ---
    mqtt_enabled: bool = True
    mqtt_host: str = "home.local"
    mqtt_port: int = 1883
    mqtt_username: str = "mqttuser"
    mqtt_password: str = "mqttuser"
    mqtt_client_id: str = "inky-frame"
    mqtt_base_topic: str = "inky-frame"
    mqtt_qos: int = 0
    # Home Assistant entities come from the custom integration, not from discovery
    # configs published here (see app/mqtt.py). This prefix is still needed: the
    # bridge clears the configs it used to publish under it.
    mqtt_discovery_prefix: str = "homeassistant"

    # --- Device identity (shown in Home Assistant) ---
    # Defaults come from the PANEL spec (app/panels.py) unless set explicitly.
    device_id: str = "inky_frame_73"    # panel default; overwritten from PANEL unless set
    device_name: str = "Inky Frame 7.3"  # panel default; overwritten from PANEL unless set

    # --- Guest Wi-Fi QR dashboard (POST /display/dashboard?name=guest_wifi) ---
    # Credentials for the guest network's connect-QR. A guest password is meant to
    # be shared, so this is low-sensitivity. security: WPA (covers WPA2/WPA3) or
    # `nopass` for an open network.
    guest_wifi_ssid: str = ""
    guest_wifi_password: str = ""
    guest_wifi_security: str = "WPA"

    # -- panel-derived defaults ---------------------------------------------
    @model_validator(mode="after")
    def _apply_panel_defaults(self) -> "Settings":
        """Fill the panel-specific fields from the PANEL spec, but only where the
        user didn't set them explicitly (env / .env). ``model_fields_set`` holds the
        names that came from init or the environment, so an explicit BUTTON_GPIO_C /
        DEVICE_ID / DEVICE_NAME still wins; otherwise the panel drives them."""
        spec = get_panel(self.panel)  # also validates PANEL, raising on a typo
        if "button_gpio_c" not in self.model_fields_set:
            self.button_gpio_c = spec.button_gpio_c
        if "device_id" not in self.model_fields_set:
            self.device_id = spec.device_id
        if "device_name" not in self.model_fields_set:
            self.device_name = spec.device_name
        return self

    @property
    def panel_spec(self) -> PanelSpec:
        """The resolved PanelSpec for the active PANEL."""
        return get_panel(self.panel)

    @property
    def resolved_busy_ceiling(self) -> float:
        """BUSY_WAIT_CEILING override if set, else the panel's default."""
        if self.busy_wait_ceiling is not None:
            return self.busy_wait_ceiling
        return self.panel_spec.busy_wait_ceiling


settings = Settings()
