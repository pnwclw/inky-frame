"""Status sensor: what the panel is doing, with everything else as attributes."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from homeassistant.components.sensor import SensorEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import InkyFrameConfigEntry
from .entity import InkyFrameEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: InkyFrameConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    async_add_entities([InkyFrameStatusSensor(entry.runtime_data)])


class InkyFrameStatusSensor(InkyFrameEntity, SensorEntity):
    _attr_name = "Status"
    _attr_icon = "mdi:image-frame"
    _attr_device_class = None

    def __init__(self, coordinator) -> None:
        super().__init__(coordinator, "status")

    @property
    def native_value(self) -> str:
        if self._display.get("busy"):
            return "refreshing"
        return "error" if self._display.get("last_error") else "idle"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        shown_at = self._display.get("last_shown_at")
        return {
            "panel": self._display.get("panel"),
            "resolution": self._display.get("resolution"),
            "driver": self._display.get("driver"),
            "mock": self._display.get("mock"),
            "last_shown_at": (
                datetime.fromtimestamp(shown_at).astimezone().isoformat()
                if shown_at
                else None
            ),
            "last_error": self._display.get("last_error"),
            "photo_id": self._library.get("current_id"),
            "photo_name": self._library.get("current_name"),
            "library_count": self._library.get("count"),
            "library_bytes": self._library.get("bytes_on_disk"),
            **self._prefs,
        }
