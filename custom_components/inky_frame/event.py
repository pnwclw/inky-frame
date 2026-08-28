"""The four physical buttons on the panel.

This is the one thing that genuinely needs MQTT: a press is a hardware event with no
HTTP equivalent, published by the frame to `<base>/button/<A|B|C|D>`. Without the MQTT
integration set up these entities simply never fire — everything else in this
integration keeps working over HTTP.

They replace the MQTT device triggers this project used to publish. An `event` entity
is the modern equivalent and shows the last press in the UI, which a trigger never
did; automations use its state change instead of a device trigger.
"""

from __future__ import annotations

from homeassistant.components.event import EventDeviceClass, EventEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import InkyFrameConfigEntry
from .const import PANEL_BUTTONS
from .entity import InkyFrameEntity

PRESS = "press"


async def async_setup_entry(
    hass: HomeAssistant,
    entry: InkyFrameConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    async_add_entities(InkyFrameButtonEvent(coordinator, label) for label in PANEL_BUTTONS)


class InkyFrameButtonEvent(InkyFrameEntity, EventEntity):
    _attr_device_class = EventDeviceClass.BUTTON
    _attr_event_types = [PRESS]
    _attr_icon = "mdi:gesture-tap-button"

    def __init__(self, coordinator, label: str) -> None:
        super().__init__(coordinator, f"button_{label.lower()}")
        self._label = label
        self._attr_name = f"Button {label}"

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(
            self.coordinator.async_add_button_listener(self._label, self._pressed)
        )

    @callback
    def _pressed(self) -> None:
        self._trigger_event(PRESS)
        self.async_write_ha_state()
