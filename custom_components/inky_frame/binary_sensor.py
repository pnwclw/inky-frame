"""Is the panel mid-refresh? A Spectra redraw takes ~30 s, so this matters."""

from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import InkyFrameConfigEntry
from .entity import InkyFrameEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: InkyFrameConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    async_add_entities([InkyFrameBusySensor(entry.runtime_data)])


class InkyFrameBusySensor(InkyFrameEntity, BinarySensorEntity):
    _attr_name = "Refreshing"
    _attr_device_class = BinarySensorDeviceClass.RUNNING
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator) -> None:
        super().__init__(coordinator, "busy")

    @property
    def is_on(self) -> bool:
        return bool(self._display.get("busy"))
