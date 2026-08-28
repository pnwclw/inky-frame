"""One-shot actions: step through the library, clear the panel, show a dashboard.

Every one of these touches the panel, and the frame coalesces pushed commands
latest-wins — five taps on "Next photo" land on the fifth photo instead of crawling
through all five at 30 s apiece.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from homeassistant.components.button import ButtonEntity, ButtonEntityDescription
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import InkyFrameConfigEntry, InkyFrameCoordinator
from .api import InkyFrameClient, InkyFrameError
from .entity import InkyFrameEntity


@dataclass(frozen=True, kw_only=True)
class InkyButtonDescription(ButtonEntityDescription):
    press: Callable[[InkyFrameClient], Awaitable[None]]


BUTTONS: tuple[InkyButtonDescription, ...] = (
    InkyButtonDescription(
        key="next", name="Next photo", icon="mdi:skip-next",
        press=lambda client: client.nav("next"),
    ),
    InkyButtonDescription(
        key="prev", name="Previous photo", icon="mdi:skip-previous",
        press=lambda client: client.nav("prev"),
    ),
    InkyButtonDescription(
        key="random", name="Random photo", icon="mdi:shuffle-variant",
        press=lambda client: client.nav("random"),
    ),
    InkyButtonDescription(
        key="clear", name="Clear panel", icon="mdi:eraser",
        press=lambda client: client.clear(),
    ),
    InkyButtonDescription(
        key="deghost", name="Deep de-ghost", icon="mdi:auto-fix",
        entity_category=EntityCategory.CONFIG,
        # Flashes the panel black/white then through all six colours; holds it for
        # minutes. Worth it when a faint ghost of an old image persists.
        press=lambda client: client.clear(cycles=3),
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: InkyFrameConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    entities: list[ButtonEntity] = [InkyFrameButton(coordinator, d) for d in BUTTONS]
    entities.extend(
        InkyFrameButton(
            coordinator,
            InkyButtonDescription(
                key=f"dashboard_{name}",
                name=f"Dashboard: {name.replace('_', ' ')}",
                icon="mdi:view-dashboard",
                press=lambda client, name=name: client.dashboard(name),
            ),
        )
        for name in coordinator.dashboards
    )
    async_add_entities(entities)


class InkyFrameButton(InkyFrameEntity, ButtonEntity):
    entity_description: InkyButtonDescription

    def __init__(
        self, coordinator: InkyFrameCoordinator, description: InkyButtonDescription
    ) -> None:
        super().__init__(coordinator, description.key)
        self.entity_description = description

    async def async_press(self) -> None:
        try:
            await self.entity_description.press(self.coordinator.client)
        except InkyFrameError as err:
            raise HomeAssistantError(str(err)) from err
        await self.coordinator.async_request_refresh()
