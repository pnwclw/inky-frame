"""Runtime device settings — the knobs that describe *this frame on this wall*.

Deliberately separate from `app/config.py`:

    Settings (config.py)   deploy-time, read-only, from env/.env. Which panel, which
                           GPIOs, broker credentials, where /data is. Changing one
                           means editing .env and recreating the container.
    Prefs    (this file)   runtime, user-editable, persisted to <OUTPUT_DIR>/prefs.json.
                           Changed from Home Assistant (MQTT selects) or
                           PATCH /prefs, and they survive a restart.

The settings here are the ones you can't know at deploy time because they depend on
how the frame ended up hanging and what the owner likes:

    orientation        how the frame is MOUNTED (landscape / portrait). Every photo
                       that doesn't say otherwise is laid out for this.
    fit                what to do when the photo's aspect doesn't match the frame:
                       cover (fill + crop), contain (pad white), or auto.
    auto_fit_max_crop  the threshold `auto` uses (see below).
    dither             default dithering algorithm.
    background         what fills the canvas where the photo doesn't reach — the
                       margin `contain` leaves and the corners a free rotation
                       exposes. One of the panel's six colours.

**`fit=auto`** is the interesting one, and the reason this file exists. `cover` looks
best — a photo edge to edge — but only while the aspect ratios are close: forcing a
portrait photo onto a landscape frame with `cover` throws away nearly half of it.
`auto` measures how much `cover` would crop (`crop_loss` in dithering.py) and picks
`cover` when that stays under `auto_fit_max_crop`, `contain` otherwise. With the
default 0.30 on a 4:3 panel: a 4:3 photo crops 0% -> cover, 3:2 crops 11% -> cover,
16:9 crops 25% -> cover, and a portrait 3:4 photo crops 44% -> contain.

The env values (DEFAULT_ORIENTATION / DEFAULT_FIT / AUTO_FIT_MAX_CROP / DITHER_MODE)
only SEED prefs.json the first time. After that the stored file wins — otherwise a
change made from Home Assistant would be silently reverted by the next deploy.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from pathlib import Path
from typing import Callable

from .config import Settings
from .dithering import (
    AVAILABLE_DITHER_MODES,
    BACKGROUND_NAMES,
    crop_loss,
)

log = logging.getLogger(__name__)

ORIENTATIONS = ("landscape", "portrait")
FITS = ("cover", "contain")
FIT_MODES = ("auto",) + FITS  # what may be *requested*; auto resolves to one of FITS


class Prefs:
    """Mutable device settings, persisted as JSON. Thread-safe; callers on the event
    loop and paho's network thread both reach it."""

    def __init__(self, settings: Settings, on_change: Callable[[dict], None] | None = None):
        self.settings = settings
        self.path = Path(settings.output_dir) / "prefs.json"
        self._lock = threading.RLock()
        self.on_change = on_change
        # Seeds; _load() overwrites from disk if prefs.json exists.
        self.orientation: str = settings.default_orientation
        self.fit: str = settings.default_fit
        self.auto_fit_max_crop: float = settings.auto_fit_max_crop
        self.dither: str = settings.dither_mode.upper()
        self.background: str = settings.default_background
        self._load()

    # -- persistence ---------------------------------------------------------
    def _load(self) -> None:
        if not self.path.exists():
            log.info("no prefs.json yet; seeding from env: %s", self.as_dict())
            return
        try:
            stored = json.loads(self.path.read_text("utf-8"))
        except (OSError, ValueError):
            log.exception("prefs.json unreadable; keeping env seeds (%s)", self.path)
            return
        try:
            self._apply_locked(stored, strict=False)
        except ValueError:
            log.exception("prefs.json holds invalid values; keeping env seeds")
        log.info("prefs loaded from %s: %s", self.path, self.as_dict())

    def _save_locked(self) -> None:
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(json.dumps(self.as_dict(), indent=1), "utf-8")
        os.replace(tmp, self.path)  # atomic within the same directory

    # -- reading -------------------------------------------------------------
    def as_dict(self) -> dict:
        return {
            "orientation": self.orientation,
            "fit": self.fit,
            "auto_fit_max_crop": self.auto_fit_max_crop,
            "dither": self.dither,
            "background": self.background,
        }

    def resolve_fit(
        self,
        requested: str | None,
        image_size: tuple[int, int],
        canvas_size: tuple[int, int],
    ) -> str:
        """Turn a requested fit (or the stored default) into a concrete cover/contain.

        `canvas_size` must be the WORKING canvas — the panel rotated for a portrait
        mount — so the decision is made against the shape the photo actually lands on.
        """
        fit = (requested or self.fit).lower()
        if fit in FITS:
            return fit
        loss = crop_loss(image_size, canvas_size)
        chosen = "cover" if loss <= self.auto_fit_max_crop else "contain"
        log.info("fit=auto: cover would crop %.0f%% (max %.0f%%) -> %s",
                 loss * 100, self.auto_fit_max_crop * 100, chosen)
        return chosen

    # -- writing -------------------------------------------------------------
    def patch(self, changes: dict) -> dict:
        """Validate and apply a partial update, persist it, and fire on_change.
        Raises ValueError (-> HTTP 400) on an unknown key or a bad value; nothing is
        applied unless every change validates."""
        with self._lock:
            before = self.as_dict()
            self._apply_locked(changes, strict=True)
            after = self.as_dict()
            if after == before:
                return after
            self._save_locked()
        if self.on_change:
            try:
                self.on_change(after)
            except Exception:  # noqa: BLE001 - a listener must not break the write
                log.exception("prefs on_change listener failed")
        log.info("prefs updated: %s", {k: v for k, v in after.items() if before[k] != v})
        return after

    def _apply_locked(self, changes: dict, *, strict: bool) -> None:
        """Validate everything first, then assign — a rejected value leaves prefs
        untouched rather than half-applied. `strict=False` ignores unknown keys, which
        is what loading a prefs.json written by another version needs."""
        staged: dict = {}
        for key, value in changes.items():
            if key not in ("orientation", "fit", "auto_fit_max_crop", "dither",
                           "background"):
                if strict:
                    raise ValueError(
                        f"Unknown pref {key!r}. Valid: orientation, fit, "
                        f"auto_fit_max_crop, dither, background"
                    )
                continue
            staged[key] = _validate(key, value)
        for key, value in staged.items():
            setattr(self, key, value)


def _validate(key: str, value):
    if key == "orientation":
        text = str(value).strip().lower()
        if text not in ORIENTATIONS:
            raise ValueError(f"orientation must be one of {', '.join(ORIENTATIONS)}")
        return text
    if key == "fit":
        text = str(value).strip().lower()
        if text not in FIT_MODES:
            raise ValueError(f"fit must be one of {', '.join(FIT_MODES)}")
        return text
    if key == "dither":
        text = str(value).strip().upper()
        if text not in AVAILABLE_DITHER_MODES:
            raise ValueError(f"dither must be one of {', '.join(AVAILABLE_DITHER_MODES)}")
        return text
    if key == "background":
        text = str(value).strip().lower()
        if text not in BACKGROUND_NAMES:
            raise ValueError(f"background must be one of {', '.join(BACKGROUND_NAMES)}")
        return text
    if key == "auto_fit_max_crop":
        try:
            number = float(value)
        except (TypeError, ValueError):
            raise ValueError("auto_fit_max_crop must be a number between 0 and 1") from None
        if not 0.0 <= number <= 1.0:
            raise ValueError("auto_fit_max_crop must be between 0 and 1")
        return round(number, 3)
    raise ValueError(f"Unknown pref {key!r}")
