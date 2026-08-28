"""The current frame, as an image entity."""

from __future__ import annotations

from datetime import UTC, datetime

from homeassistant.components.image import ImageEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import InkyFrameConfigEntry, InkyFrameCoordinator
from .api import InkyFrameError
from .entity import InkyFrameEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: InkyFrameConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    async_add_entities([InkyFrameImage(hass, entry.runtime_data)])


class InkyFrameImage(InkyFrameEntity, ImageEntity):
    _attr_name = "Current frame"
    _attr_content_type = "image/png"

    def __init__(self, hass: HomeAssistant, coordinator: InkyFrameCoordinator) -> None:
        InkyFrameEntity.__init__(self, coordinator, "current")
        ImageEntity.__init__(self, hass)
        self._attr_image_last_updated = self._shown_at()

    def _shown_at(self) -> datetime | None:
        # preview_updated_at first: last_shown_at is per-process and goes None after
        # the frame service restarts, but e-paper is still showing the last image, so
        # the entity would sit at `unknown` until the next refresh.
        shown = self._display.get("preview_updated_at") or self._display.get("last_shown_at")
        return datetime.fromtimestamp(shown, tz=UTC) if shown else None

    @callback
    def _handle_coordinator_update(self) -> None:
        # ImageEntity keeps `_cached_image` forever once it has one and never
        # invalidates it itself, so a new frame has to clear it here.
        updated = self._shown_at()
        if updated != self._attr_image_last_updated:
            self._attr_image_last_updated = updated
            self._cached_image = None
        super()._handle_coordinator_update()

    async def async_image(self) -> bytes | None:
        # ?view=true: a render is the panel BUFFER, always landscape, so a portrait
        # mount would read sideways without it. Fetched by Home Assistant, never by
        # the browser, so the frame's address stays private to HA.
        try:
            data, _ = await self.coordinator.client.fetch("/display/preview?view=true")
        except InkyFrameError:
            return None
        return data
