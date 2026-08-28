"""Watch the four panel buttons with gpiod (v2) and call back on each press.

Modelled on Pimoroni's spectra6/buttons.py example. Runs in a daemon thread; the
edge wait has a timeout so the thread can be stopped cleanly. gpiod and the panel
driver request different GPIO lines on the same chip, so they coexist fine.

Buttons A/B/D are BCM GPIO 5/6/24 on every Impression. Button C is the one that
moves: GPIO 16 on the 7.3", but GPIO 25 on the 13.3" (there GPIO 16 is the
display's CS1). The GPIO comes from ``settings.button_gpio_c``, which the PANEL
spec fills in (app/panels.py) unless BUTTON_GPIO_C is set explicitly.
"""

from __future__ import annotations

import logging
import threading
import time

from .config import Settings

log = logging.getLogger(__name__)


class ButtonWatcher:
    def __init__(self, settings: Settings, on_press):
        self.settings = settings
        self.on_press = on_press
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._request = None

    def start(self) -> None:
        if self.settings.inky_mock:
            log.info("INKY_MOCK enabled — button watcher disabled")
            return
        try:
            import gpiod  # noqa: F401
            import gpiodevice  # noqa: F401
        except Exception as exc:  # noqa: BLE001
            log.warning("gpiod unavailable (%s) — buttons disabled", exc)
            return
        self._thread = threading.Thread(target=self._run, name="buttons", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
        if self._request is not None:
            try:
                self._request.release()
            except Exception:  # noqa: BLE001
                pass

    def _run(self) -> None:
        import gpiod
        import gpiodevice
        from gpiod.line import Bias, Direction, Edge

        s = self.settings
        pins = {
            s.button_gpio_a: "A",
            s.button_gpio_b: "B",
            s.button_gpio_c: "C",
            s.button_gpio_d: "D",
        }
        line_settings = gpiod.LineSettings(
            direction=Direction.INPUT, bias=Bias.PULL_UP, edge_detection=Edge.FALLING
        )
        chip = gpiodevice.find_chip_by_platform()
        offsets = {chip.line_offset_from_id(gpio): label for gpio, label in pins.items()}
        config = dict.fromkeys(offsets, line_settings)
        self._request = chip.request_lines(consumer="inky-frame-buttons", config=config)
        log.info("Button watcher started on GPIOs %s", list(pins))

        debounce = s.button_debounce_ms / 1000.0
        last: dict[str, float] = {label: 0.0 for label in pins.values()}
        while not self._stop.is_set():
            # Wake periodically so _stop is observed even with no presses.
            if not self._request.wait_edge_events(timeout=0.5):
                continue
            for event in self._request.read_edge_events():
                label = offsets.get(event.line_offset)
                now = time.monotonic()
                if label and (now - last[label]) >= debounce:
                    last[label] = now
                    try:
                        self.on_press(label)
                    except Exception:  # noqa: BLE001
                        log.exception("button press handler failed")
