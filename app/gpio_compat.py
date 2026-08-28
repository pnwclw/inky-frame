"""Container-friendly gpiochip resolution.

`gpiodevice.find_chip_by_platform()` identifies the board by reading
`/proc/device-tree/model`. That path is **not** visible inside our container:
procfs occupies `/proc`, so the device-tree can't be bind-mounted over it, and
the runtime doesn't expose it. Both the inky driver (`InkyEL133UF1` calls it
lazily at `show()` time) and the button watcher call `find_chip_by_platform()`,
so a failed lookup breaks panel refreshes *and* buttons with the same
`RuntimeError("No compatible platform detected!")`.

We monkeypatch the lookup to fall back to opening a known gpiochip by path when
platform detection fails. On bare metal (device-tree visible) nothing changes —
the fallback only fires on failure. One patch fixes both call sites because they
both look the function up as a module attribute at call time.
"""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)

_installed = False


def install(chip_path: str = "/dev/gpiochip0") -> None:
    """Make ``gpiodevice.find_chip_by_platform()`` fall back to ``chip_path``.

    Idempotent; call once before any inky / button GPIO use. ``/dev/gpiochip0``
    is the Pi 5 header chip (see CLAUDE.md §7) and is the node mapped into the
    container, so it matches what platform detection would have returned.
    """
    global _installed
    if _installed:
        return
    try:
        import gpiod
        import gpiodevice
    except Exception as exc:  # noqa: BLE001 - not on a Pi / gpiod missing
        log.warning("gpiod/gpiodevice unavailable (%s) — gpio fallback not installed", exc)
        return

    _orig = gpiodevice.find_chip_by_platform

    def _find_chip_by_platform():
        try:
            return _orig()
        except Exception as exc:  # noqa: BLE001 - platform detection failed (e.g. in-container)
            log.warning(
                "gpiodevice platform detection failed (%s); falling back to gpiod.Chip(%s)",
                exc,
                chip_path,
            )
            return gpiod.Chip(chip_path)

    gpiodevice.find_chip_by_platform = _find_chip_by_platform
    _installed = True
    log.info("gpio compat installed: find_chip_by_platform() falls back to %s", chip_path)


_busy_patch_installed = False


def install_busy_wait_fix(driver_class: str = "InkyEL133UF1", ceiling: float = 50.0) -> None:
    """Make the panel wait for the refresh to *actually finish* before powering off.

    The stock ``inky`` Spectra driver assumes it "won't get a BUSY signal" and its
    ``_busy_wait`` mishandles the long refresh: ``_update`` fires ``POF`` (power off)
    a fraction of a second into a multi-second waveform — a power-off mid-refresh,
    which is the classic cause of Spectra image retention / ghosting. BUSY is in fact
    reliable — LOW while refreshing, HIGH (ACTIVE) the instant the waveform completes
    — so we simply poll it. (Originally found on the 13.3" ``InkyEL133UF1``; the 7.3"
    ``InkyE673`` ships the byte-for-byte identical ``_busy_wait``, so the same patch
    applies — ``driver_class`` just selects which one to patch.)

    Short settle/handshake waits (<=5 s) keep their stock timing; only the long
    refresh wait is corrected, with `ceiling` seconds of headroom against a stuck
    signal (a cold refresh can run a little longer). `ceiling` is per-panel (see
    app/panels.py): smaller for the faster 7.3", but always with margin."""
    global _busy_patch_installed
    if _busy_patch_installed:
        return
    try:
        import time
        import warnings

        import inky
        from gpiod.line import Value

        cls = getattr(inky, driver_class)
    except Exception as exc:  # noqa: BLE001 - not on a Pi / inky missing / bad class name
        log.warning("inky/gpiod unavailable (%s) — busy-wait fix not installed", exc)
        return

    def _busy_wait(self, timeout: float = 40.0) -> None:
        # BUSY: ACTIVE (high) = idle/done, INACTIVE (low) = refreshing.
        if timeout <= 5.0:
            # short settle/handshake — preserve stock timing
            if self._gpio.get_value(self.busy_pin) == Value.ACTIVE:
                time.sleep(timeout)
                return
            start = time.time()
            while self._gpio.get_value(self.busy_pin) != Value.ACTIVE:
                if time.time() - start > timeout:
                    return
                time.sleep(0.05)
            return
        # long refresh — poll until the panel reports done, THEN let POF fire
        start = time.time()
        while self._gpio.get_value(self.busy_pin) != Value.ACTIVE:
            if time.time() - start > ceiling:
                warnings.warn(f"Busy Wait: timed out after {ceiling:.0f}s")
                return
            time.sleep(0.05)
        log.info("inky refresh complete in %.1fs (BUSY idle)", time.time() - start)

    cls._busy_wait = _busy_wait
    _busy_patch_installed = True
    log.info(
        "inky busy-wait fix installed on %s: poll BUSY to completion, %.0fs ceiling",
        driver_class, ceiling,
    )
