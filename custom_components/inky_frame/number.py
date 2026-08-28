"""The `fit=auto` threshold, as a percentage slider.

The frame stores it as 0.0-1.0 (`auto_fit_max_crop`); a percentage is what makes sense
on a slider, so this converts in both directions. At the default 30% a 16:9 photo
(25% cropped) still gets `cover`, while a portrait photo on a landscape frame (44%)
falls back to `contain`.
"""

from __future__ import annotations

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import InkyFrameConfigEntry
from .api import InkyFrameError
from .entity import InkyFrameEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: InkyFrameConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    async_add_entities([InkyFrameAutoCrop(entry.runtime_data)])


class InkyFrameAutoCrop(InkyFrameEntity, NumberEntity):
    _attr_name = "Auto-fit max crop"
    _attr_icon = "mdi:crop"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_native_min_value = 0
    _attr_native_max_value = 100
    _attr_native_step = 5
    _attr_native_unit_of_measurement = "%"
    _attr_mode = NumberMode.SLIDER

    def __init__(self, coordinator) -> None:
        super().__init__(coordinator, "auto_crop")

    @property
    def native_value(self) -> float | None:
        value = self._prefs.get("auto_fit_max_crop")
        return None if value is None else round(float(value) * 100)

    async def async_set_native_value(self, value: float) -> None:
        try:
            await self.coordinator.client.set_prefs({"auto_fit_max_crop": value / 100})
        except InkyFrameError as err:
            raise HomeAssistantError(str(err)) from err
        await self.coordinator.async_request_refresh()
