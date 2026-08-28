"""Per-panel facts, in one place, so the whole service can be switched between
Inky Impression sizes by flipping a single ``PANEL`` value (env / .env).

Everything that genuinely differs between the two panels lives on a ``PanelSpec``:
the inky driver class, the native (landscape) resolution, which GPIO button C is
wired to, the busy-wait ceiling, and the Home-Assistant identity. Anything that
does *not* differ (the 6-colour Spectra palette and its index order, the A/B/D
button GPIOs, the dithering handoff, the SPI/DC/RESET/BUSY control pins) is shared
and stays where it was.

Both panels are **Spectra 6 (E6)**: same six colours, same DESATURATED_PALETTE
order (black, white, yellow, red, blue, green), and — importantly — the 7.3"
``InkyE673`` driver ships the *identical* ``_busy_wait`` implementation as the
13.3" ``InkyEL133UF1`` (inky 2.4.0), so the busy-wait fix in ``gpio_compat`` ports
across unchanged; only the ceiling differs.

To add a third panel: add one ``PanelSpec`` entry below. To switch panels: set
``PANEL`` (see ``.env.example``); ``DEFAULT_PANEL`` is the fallback when it's unset.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PanelSpec:
    key: str
    # inky class name, re-exported from the ``inky`` package top level. Used for the
    # direct-construct fallback when EEPROM auto-detect is off/failing, and to know
    # which driver class to install the busy-wait fix onto.
    driver_class: str
    # Native landscape resolution (width, height). The live driver's own
    # ``.resolution`` still wins at runtime; this is the pre-hardware / mock default
    # and the canvas the dashboards lay out on.
    resolution: tuple[int, int]
    # Button C's BCM GPIO. It is the ONE button that moves between panels: on the
    # 13.3" it is GPIO 25 because GPIO 16 is the display's second chip-select (CS1);
    # the 7.3" has a single chip-select, so C sits on the usual GPIO 16. A/B/D are
    # 5/6/24 on both and stay in config.
    button_gpio_c: int
    # Seconds the patched ``_busy_wait`` will poll BUSY before giving up (a safety
    # cap against a stuck signal, NOT the expected refresh time — the poll returns as
    # soon as the panel reports done). Kept comfortably above inky's own 32 s refresh
    # budget. The smaller 7.3" refreshes faster, so it gets a smaller ceiling — but
    # still with margin.
    busy_wait_ceiling: float
    # Home Assistant identity (device registry + MQTT discovery).
    device_id: str
    device_name: str
    ha_model: str


PANELS: dict[str, PanelSpec] = {
    "impression_13": PanelSpec(
        key="impression_13",
        driver_class="InkyEL133UF1",
        resolution=(1600, 1200),
        button_gpio_c=25,          # GPIO 16 is CS1 on the 13.3"
        busy_wait_ceiling=50.0,
        device_id="inky_frame_13",
        device_name="Inky Frame 13.3",
        ha_model="Inky Impression 13.3 (EL133UF1)",
    ),
    "impression_73": PanelSpec(
        key="impression_73",
        driver_class="InkyE673",
        resolution=(800, 480),
        button_gpio_c=16,          # single chip-select → C on the usual GPIO 16
        busy_wait_ceiling=40.0,    # < the 13.3", still > inky's 32 s refresh budget
        device_id="inky_frame_73",
        device_name="Inky Frame 7.3",
        ha_model="Inky Impression 7.3 (E673)",
    ),
}

# The panel the service drives when PANEL is unset. Flip this (or set PANEL) to
# switch the whole service between sizes.
DEFAULT_PANEL = "impression_73"


def get_panel(key: str) -> PanelSpec:
    """Resolve a PANEL value to its spec, raising a clear error on a typo."""
    try:
        return PANELS[key]
    except KeyError:
        raise ValueError(
            f"Unknown PANEL {key!r}. Valid panels: {', '.join(sorted(PANELS))}"
        ) from None
