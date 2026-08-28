"""Owns the Inky panel: driver init, rendering, and serialized refreshes.

A full Spectra refresh takes ~30s and drives SPI, so every refresh runs in a
worker thread guarded by a single lock — two refreshes never overlap. The button
watcher uses different GPIO lines and never touches the panel, so it runs
independently.

Which panel (7.3" vs 13.3") is driven comes from ``settings.panel`` /
``settings.panel_spec`` (app/panels.py); this module reads the resolution and the
driver class from there rather than hard-coding either.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from pathlib import Path

from PIL import Image

from .config import Settings
from .dithering import INKY_PALETTE_RGB, render_for_inky

log = logging.getLogger(__name__)


class DisplayManager:
    def __init__(self, settings: Settings, on_state_change=None):
        self.settings = settings
        # Called with status() whenever a refresh starts or finishes, so MQTT can
        # publish `busy` without polling. Fires from the refresh WORKER THREAD as well
        # as the event loop — the listener must be thread-safe (paho.publish is).
        self.on_state_change = on_state_change
        self._lock = asyncio.Lock()
        # Latest-wins slot for pushed commands; see _queue_latest().
        self._pending: Image.Image | None = None
        self._draining = False
        self._driver = None
        # Pre-hardware default from the panel spec; the live driver's real
        # ``.resolution`` overwrites this in init_driver(). In mock mode this is the
        # size everything renders at.
        self.size: tuple[int, int] = tuple(settings.panel_spec.resolution)
        self.inky_palette = list(INKY_PALETTE_RGB)
        self.busy = False
        self.last_shown_at: float | None = None
        self.last_error: str | None = None
        Path(settings.output_dir).mkdir(parents=True, exist_ok=True)

    # -- lifecycle -----------------------------------------------------------
    def init_driver(self) -> None:
        if self.settings.inky_mock:
            log.warning(
                "INKY_MOCK enabled — no hardware; previews go to %s", self.preview_path
            )
            return
        # Imported lazily so the app starts on non-Pi hosts when mocked.
        try:
            if not self.settings.inky_auto_detect:
                raise RuntimeError("auto-detect disabled by config")
            from inky.auto import auto

            self._driver = auto()
        except Exception as exc:  # noqa: BLE001 - fall back to a direct construct
            import inky

            driver_class = self.settings.panel_spec.driver_class
            log.warning("inky.auto() failed (%s); constructing %s directly", exc, driver_class)
            self._driver = getattr(inky, driver_class)()

        self.size = tuple(self._driver.resolution)
        # Stay correct even if Pimoroni reorders the palette in a future release.
        desat = getattr(self._driver, "DESATURATED_PALETTE", None)
        if desat:
            self.inky_palette = [tuple(c) for c in desat[:6]]
        log.info("Inky driver ready: %s resolution=%s", type(self._driver).__name__, self.size)

    # -- helpers -------------------------------------------------------------
    @property
    def preview_path(self) -> str:
        return os.path.join(self.settings.output_dir, "latest.png")

    def render(
        self,
        image: Image.Image,
        fit: str,
        orientation: str = "landscape",
        mode: str | None = None,
        rotate: int = 0,
        crop: list[float] | None = None,
    ) -> Image.Image:
        return render_for_inky(
            image,
            self.settings,
            size=self.size,
            fit=fit,
            orientation=orientation,
            rotate=rotate,
            crop=crop,
            inky_palette=self.inky_palette,
            mode=mode,
        )

    def _save_preview(self, p_image: Image.Image) -> None:
        p_image.convert("RGB").save(self.preview_path)

    def _drive(self, p_image: Image.Image) -> None:
        """Blocking ~30s SPI refresh. Runs in a worker thread under the lock."""
        self.busy = True
        self._notify()
        try:
            if self._driver is not None:
                self._driver.set_image(p_image, saturation=self.settings.inky_saturation)
                self._driver.show()
            self.last_shown_at = time.time()
            self.last_error = None
            log.info("Display refreshed")
        except BaseException as exc:  # noqa: BLE001
            # inky's gpiodevice raises SystemExit (not Exception) on a pin conflict
            # (e.g. the SPI chip-select GPIO held by the kernel — needs
            # `dtoverlay=spi0-0cs`, see CLAUDE.md §7/§12). Catch BaseException so a
            # background (wait=false) refresh can't crash the process, but let real
            # cancellation/interrupts through. Record it, then re-raise as an ordinary
            # Exception so `_show_guarded` swallows it and `wait=true` returns a 500.
            if isinstance(exc, (KeyboardInterrupt, asyncio.CancelledError)):
                raise
            self.last_error = repr(exc)
            log.exception("Display refresh failed")
            raise RuntimeError(f"panel refresh failed: {exc!r}") from exc
        finally:
            self.busy = False
            self._notify()

    def _notify(self) -> None:
        if self.on_state_change is None:
            return
        try:
            self.on_state_change(self.status())
        except Exception:  # noqa: BLE001 - a listener must never break a refresh
            log.exception("display state listener failed")

    # -- public API ----------------------------------------------------------
    async def render_preview(
        self,
        image: Image.Image,
        fit: str = "cover",
        orientation: str = "landscape",
        mode: str | None = None,
        rotate: int = 0,
        crop: list[float] | None = None,
    ) -> Image.Image:
        """Render + write preview without touching the panel. Returns the mode-"P"
        image so the caller can archive it (app/library.py) without re-rendering."""
        p_image = await asyncio.to_thread(
            self.render, image, fit, orientation, mode, rotate, crop)
        await asyncio.to_thread(self._save_preview, p_image)
        return p_image

    async def show_image(
        self,
        image: Image.Image,
        *,
        fit: str = "cover",
        orientation: str = "landscape",
        wait: bool = False,
        mode: str | None = None,
        rotate: int = 0,
        crop: list[float] | None = None,
        coalesce: bool = False,
    ) -> Image.Image:
        """Render, then push to the panel. Returns the mode-"P" image that was sent.

        With wait=False this returns as soon as the refresh is queued, so the caller
        archiving the result records "queued at", not "on the wall at" —
        ``status()["last_shown_at"]`` stays the authority on the actual refresh.

        `coalesce` picks the scheduling policy for that background refresh:
        False (HTTP) queues behind whatever is already running, so every photo posted
        is eventually shown; True (MQTT commands) is latest-wins — see _queue_latest.
        """
        p_image = await asyncio.to_thread(
            self.render, image, fit, orientation, mode, rotate, crop)
        return await self.show_rendered(p_image, wait=wait, coalesce=coalesce)

    async def show_rendered(
        self, p_image: Image.Image, *, wait: bool = False, coalesce: bool = False
    ) -> Image.Image:
        """Push an image that is ALREADY the panel buffer — a mode-"P" render the
        library kept. There is nothing to re-run: the stored PNG holds the exact
        palette indices that went to the panel last time, so this skips the whole
        pipeline and puts back precisely the look that was chosen."""
        await asyncio.to_thread(self._save_preview, p_image)
        if wait:
            await self._show_locked(p_image)
        elif coalesce:
            self._queue_latest(p_image)
        else:
            asyncio.create_task(self._show_guarded(p_image))
        return p_image

    def _queue_latest(self, p_image: Image.Image) -> None:
        """Latest-wins scheduling for pushed commands. A refresh takes ~30 s, so five
        rapid "next photo" presses from Home Assistant must not queue two and a half
        minutes of refreshes and crawl through five images: each submission REPLACES
        the one still waiting, and only the last one reaches the panel."""
        self._pending = p_image
        if not self._draining:
            self._draining = True
            asyncio.create_task(self._drain())

    async def _drain(self) -> None:
        try:
            while self._pending is not None:
                p_image, self._pending = self._pending, None
                try:
                    await self._show_locked(p_image)
                except Exception:  # noqa: BLE001 - logged + recorded in last_error
                    pass
        finally:
            # No await between the loop test and here, so a _queue_latest() racing us
            # either lands in the loop or starts a fresh drain — never gets stranded.
            self._draining = False

    async def clear(self, *, wait: bool = False, cycles: int = 1, delay: float = 1.5) -> dict:
        """Blank the panel to white. `cycles` <= 1 does a single white flush; `cycles`
        > 1 runs a deep DE-GHOST: flash solid black<->white `cycles` times, then a pass
        of each of the 6 colours, ending white. e-paper (esp. 6-colour Spectra) retains
        faint 'ghosts' that one white flush won't clear; swinging the pixels to their
        extremes clears the residual charge, and `delay` seconds of rest between flushes
        lets them settle (clears stubborn residue better). The de-ghost blocks until
        finished (~10-20 s per frame)."""
        if cycles <= 1:
            white = Image.new("RGB", self.size, (255, 255, 255))
            await self.show_image(white, fit="contain", wait=wait)
            return {"cleared": True}
        seq: list[tuple[int, int, int]] = []
        for _ in range(cycles):
            seq += [(0, 0, 0), (255, 255, 255)]
        seq += [(255, 0, 0), (0, 255, 0), (0, 0, 255), (255, 255, 0), (255, 255, 255)]
        async with self._lock:
            for i, rgb in enumerate(seq, 1):
                p = await asyncio.to_thread(
                    self.render, Image.new("RGB", self.size, rgb), "contain", "landscape", "NONE")
                await asyncio.to_thread(self._drive, p)
                log.info("clear/de-ghost %d/%d rgb=%s", i, len(seq), rgb)
                if delay > 0 and i < len(seq):
                    await asyncio.sleep(delay)
        self._save_preview(self.render(
            Image.new("RGB", self.size, (255, 255, 255)), "contain", "landscape", "NONE"))
        return {"cleared": True, "deghost": True, "frames": len(seq), "delay": delay}

    async def _show_locked(self, p_image: Image.Image) -> None:
        async with self._lock:
            await asyncio.to_thread(self._drive, p_image)

    async def _show_guarded(self, p_image: Image.Image) -> None:
        try:
            await self._show_locked(p_image)
        except Exception:  # noqa: BLE001 - already logged in _drive
            pass

    def status(self) -> dict:
        return {
            "mock": self.settings.inky_mock,
            "panel": self.settings.panel,
            "driver": type(self._driver).__name__ if self._driver else None,
            "resolution": list(self.size),
            "busy": self.busy,
            # When THIS process last drove the panel. None after a restart even though
            # e-paper is still showing something — which is why preview_updated_at
            # exists next to it.
            "last_shown_at": self.last_shown_at,
            # When the preview file was last written, i.e. when the image now on the
            # panel was produced. Survives a restart, so it is the right thing to
            # version a "current frame" image against.
            "preview_updated_at": self.preview_updated_at,
            "last_error": self.last_error,
        }

    @property
    def preview_updated_at(self) -> float | None:
        try:
            return os.path.getmtime(self.preview_path)
        except OSError:
            return None
