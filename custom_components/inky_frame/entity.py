"""Shared base for every Inky Frame entity."""

from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import InkyFrameCoordinator
from .const import DOMAIN, MQTT_DOMAIN


class InkyFrameEntity(CoordinatorEntity[InkyFrameCoordinator]):
    _attr_has_entity_name = True

    def __init__(self, coordinator: InkyFrameCoordinator, key: str) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.device_id}_{key}"
        # TWO identifiers on purpose. Ours says who owns the device now that every
        # entity comes from this integration. The ("mqtt", …) one is what the MQTT
        # integration created back when it published discovery configs, and keeping it
        # makes the registry match the EXISTING device record — so the area, any
        # renamed entities and the config entry the user already added all survive the
        # move instead of a fresh duplicate device appearing beside them.
        self._attr_device_info = DeviceInfo(
            identifiers={
                (DOMAIN, coordinator.device_id),
                (MQTT_DOMAIN, coordinator.device_id),
            },
            name=coordinator.device_name,
            manufacturer="Pimoroni",
            model=coordinator.device_model,
        )

    @property
    def _display(self) -> dict:
        return (self.coordinator.data or {}).get("display") or {}

    @property
    def _library(self) -> dict:
        return (self.coordinator.data or {}).get("library") or {}

    @property
    def _prefs(self) -> dict:
        return (self.coordinator.data or {}).get("prefs") or {}
