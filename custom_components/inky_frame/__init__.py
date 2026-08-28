"""The Inky Frame integration — every Home Assistant entity for the frame.

The frame used to describe itself with MQTT discovery configs, which meant two places
defined one device: hand-written JSON in `app/mqtt.py` and this integration's
`media_player`. Everything now lives here, and the frame publishes no discovery at all
(it still clears the configs it once published — see `MqttBridge._retire_legacy_
discovery`). What it publishes is transport: retained state/prefs snapshots, physical
button presses, and the command topics.

**Control goes over HTTP, state comes over both.** Every action — nav, clear,
dashboards, prefs, playing a photo — is a call to the frame's own API, so the
integration works with the MQTT integration absent. MQTT, when it is there, is a
"something changed, re-read" signal that makes state instant instead of up to 30 s
stale, and it is the only source for the four physical buttons, which have no HTTP
equivalent.
"""

from __future__ import annotations

from collections.abc import Callable
import logging

from homeassistant.components import mqtt
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_URL, Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from . import panel
from .api import InkyFrameClient, InkyFrameError
from .const import CONF_GALLERY_URL, DOMAIN, SCAN_INTERVAL

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
    Platform.EVENT,
    Platform.IMAGE,
    Platform.MEDIA_PLAYER,
    Platform.NUMBER,
    Platform.SELECT,
    Platform.SENSOR,
]


class InkyFrameCoordinator(DataUpdateCoordinator[dict]):
    """Polls GET /status. The frame also pushes the same state over MQTT, but this
    integration must work without the MQTT integration being set up, and a refresh
    takes ~30 s anyway — there is nothing to gain from polling harder."""

    def __init__(
        self, hass: HomeAssistant, entry: InkyFrameConfigEntry, client: InkyFrameClient
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=SCAN_INTERVAL,
        )
        self.client = client
        self.owns_panel = False
        self._button_listeners: dict[str, list[Callable[[], None]]] = {}

    # -- physical panel buttons, fed by MQTT ----------------------------------
    @callback
    def async_add_button_listener(
        self, label: str, listener: Callable[[], None]
    ) -> Callable[[], None]:
        self._button_listeners.setdefault(label, []).append(listener)

        @callback
        def _remove() -> None:
            self._button_listeners[label].remove(listener)

        return _remove

    @callback
    def async_button_pressed(self, label: str) -> None:
        for listener in self._button_listeners.get(label, ()):
            listener()

    async def _async_update_data(self) -> dict:
        try:
            return await self.client.status()
        except InkyFrameError as err:
            raise UpdateFailed(str(err)) from err

    # -- identity, read from the frame rather than configured twice ------------
    @property
    def _device(self) -> dict:
        return (self.data or {}).get("device") or {}

    @property
    def device_id(self) -> str:
        return self._device.get("id") or "inky_frame"

    @property
    def device_name(self) -> str | None:
        return self._device.get("name")

    @property
    def device_model(self) -> str | None:
        return self._device.get("model")

    @property
    def mqtt_base_topic(self) -> str | None:
        return self._device.get("mqtt_base_topic")

    @property
    def browser_base_url(self) -> str | None:
        """What the frame thinks other machines reach it on (PUBLIC_BASE_URL), used
        only as the default for the panel's URL — the browser may need a different
        one."""
        return self._device.get("base_url")

    @property
    def dashboards(self) -> list[str]:
        return list((self.data or {}).get("dashboards") or [])


# A plain alias rather than a `type` statement: this file is linted by the frame
# project (whose floor is Python 3.11) even though Home Assistant runs it on 3.14.
InkyFrameConfigEntry = ConfigEntry[InkyFrameCoordinator]


async def async_setup_entry(hass: HomeAssistant, entry: InkyFrameConfigEntry) -> bool:
    client = InkyFrameClient(async_get_clientsession(hass), entry.data[CONF_URL])
    coordinator = InkyFrameCoordinator(hass, entry, client)
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator
    await _async_setup_mqtt(hass, entry, coordinator)

    gallery_url = entry.options.get(CONF_GALLERY_URL) or (
        f"{coordinator.browser_base_url}/gallery" if coordinator.browser_base_url else ""
    )
    coordinator.owns_panel = await panel.async_register(hass, gallery_url)

    # Changing the panel URL has to re-register the panel, and reloading is the
    # simplest way to get there.
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def _async_options_updated(hass: HomeAssistant, entry: InkyFrameConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def _async_setup_mqtt(
    hass: HomeAssistant, entry: InkyFrameConfigEntry, coordinator: InkyFrameCoordinator
) -> None:
    """Subscribe for push updates and button presses, if MQTT is set up at all.

    Deliberately NOT a hard dependency: everything except the physical buttons works
    over HTTP, so a missing MQTT integration degrades to 30 s polling rather than
    failing setup."""
    base = coordinator.mqtt_base_topic
    if not base:
        return
    if not await mqtt.async_wait_for_mqtt_client(hass):
        _LOGGER.debug("MQTT not available; falling back to polling only")
        return

    @callback
    def _state_changed(_msg: mqtt.ReceiveMessage) -> None:
        # The retained payload has a different shape from GET /status, so treat this
        # purely as "something changed" and re-read. The request is debounced by the
        # coordinator and goes to the same host, so it costs nothing.
        hass.async_create_task(coordinator.async_request_refresh())

    @callback
    def _button_pressed(msg: mqtt.ReceiveMessage) -> None:
        coordinator.async_button_pressed(msg.topic.rsplit("/", 1)[-1].upper())

    entry.async_on_unload(await mqtt.async_subscribe(hass, f"{base}/state", _state_changed))
    entry.async_on_unload(
        await mqtt.async_subscribe(hass, f"{base}/button/+", _button_pressed)
    )
    _LOGGER.debug("Subscribed to %s/state and %s/button/+", base, base)


async def async_unload_entry(hass: HomeAssistant, entry: InkyFrameConfigEntry) -> bool:
    if entry.runtime_data.owns_panel:
        panel.async_remove(hass)
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
