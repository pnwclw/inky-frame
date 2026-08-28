"""The device settings that are a choice: how the frame is mounted, how a photo is
placed, which dithering algorithm new photos get.

These are the frame's own persisted prefs (`<OUTPUT_DIR>/prefs.json`), not Home
Assistant state — `PATCH /prefs` is the single write path whether the change came from
here, from the gallery page, or from curl.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from homeassistant.components.select import SelectEntity, SelectEntityDescription
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import InkyFrameConfigEntry, InkyFrameCoordinator
from .api import InkyFrameError
from .entity import InkyFrameEntity


@dataclass(frozen=True, kw_only=True)
class InkySelectDescription(SelectEntityDescription):
    pref: str
    options_from: Callable[[dict], list[str]]


SELECTS: tuple[InkySelectDescription, ...] = (
    InkySelectDescription(
        key="orientation",
        name="Mounting",
        icon="mdi:phone-rotate-landscape",
        entity_category=EntityCategory.CONFIG,
        pref="orientation",
        options_from=lambda _status: ["landscape", "portrait"],
    ),
    InkySelectDescription(
        key="fit",
        name="Fit",
        icon="mdi:fit-to-screen-outline",
        entity_category=EntityCategory.CONFIG,
        pref="fit",
        options_from=lambda _status: ["auto", "cover", "contain"],
    ),
    InkySelectDescription(
        key="dither",
        name="Dithering",
        icon="mdi:dots-grid",
        entity_category=EntityCategory.CONFIG,
        pref="dither",
        # Straight from the frame, so the list tracks whatever epaper-dithering ships.
        options_from=lambda status: list((status.get("dither") or {}).get("available") or []),
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: InkyFrameConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    async_add_entities(InkyFrameSelect(coordinator, d) for d in SELECTS)


class InkyFrameSelect(InkyFrameEntity, SelectEntity):
    entity_description: InkySelectDescription

    def __init__(
        self, coordinator: InkyFrameCoordinator, description: InkySelectDescription
    ) -> None:
        super().__init__(coordinator, description.key)
        self.entity_description = description

    @property
    def options(self) -> list[str]:
        return self.entity_description.options_from(self.coordinator.data or {})

    @property
    def current_option(self) -> str | None:
        return self._prefs.get(self.entity_description.pref)

    async def async_select_option(self, option: str) -> None:
        try:
            await self.coordinator.client.set_prefs({self.entity_description.pref: option})
        except InkyFrameError as err:
            raise HomeAssistantError(str(err)) from err
        # Changing the mounting also re-shows the current photo on the frame, so the
        # panel state changes too — re-read rather than assuming.
        await self.coordinator.async_request_refresh()
