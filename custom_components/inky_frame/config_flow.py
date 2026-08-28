"""Config flow: ask where the frame is, then confirm by reading GET /status.

`/status` reports the frame's device identity, so the entry's unique id is the same
`DEVICE_ID` the MQTT discovery uses — adding the same frame twice is rejected, and
swapping the physical panel (which changes DEVICE_ID) is correctly a different device.
"""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.const import CONF_HOST, CONF_PORT, CONF_URL
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import InkyFrameClient, InkyFrameError
from .const import CONF_GALLERY_URL, DEFAULT_HOST, DEFAULT_PORT, DOMAIN

STEP_USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_HOST, default=DEFAULT_HOST): str,
        vol.Required(CONF_PORT, default=DEFAULT_PORT): int,
    }
)


class InkyFrameConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return InkyFrameOptionsFlow()

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            url = f"http://{user_input[CONF_HOST]}:{user_input[CONF_PORT]}"
            client = InkyFrameClient(async_get_clientsession(self.hass), url)
            try:
                status = await client.status()
            except InkyFrameError:
                errors["base"] = "cannot_connect"
            else:
                device = status.get("device") or {}
                if not device.get("id"):
                    errors["base"] = "not_a_frame"
                else:
                    await self.async_set_unique_id(device["id"])
                    self._abort_if_unique_id_configured(updates={CONF_URL: url})
                    return self.async_create_entry(
                        title=device.get("name") or "Inky Frame", data={CONF_URL: url}
                    )
        return self.async_show_form(
            step_id="user", data_schema=STEP_USER_SCHEMA, errors=errors
        )


class InkyFrameOptionsFlow(OptionsFlow):
    """One setting: where the sidebar panel should point.

    It is separate from the host/port above because the two are answered by different
    machines. Home Assistant may reach the frame on 127.0.0.1; the *browser* showing
    the panel needs an address it can route to, over https:// whenever Home Assistant
    itself is served over https, or it blocks the iframe as mixed content.
    """

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(data=user_input)

        current = self.config_entry.options.get(CONF_GALLERY_URL)
        if not current:
            coordinator = getattr(self.config_entry, "runtime_data", None)
            base = getattr(coordinator, "browser_base_url", None) or ""
            current = f"{base}/gallery" if base else ""
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {vol.Optional(CONF_GALLERY_URL, default=current): str}
            ),
        )
