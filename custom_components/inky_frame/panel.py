"""The sidebar panel the integration adds out of the box.

A `panel_custom` panel rather than a generated Lovelace dashboard: creating a
dashboard from an integration means reaching into `lovelace`'s private
`DashboardsCollection`, while a custom panel is a supported, stable API — and it puts
the page itself under our control for whatever the panel grows into later.

The panel is global (one sidebar entry), so it is registered once for the first config
entry that asks and removed when that entry unloads.
"""

from __future__ import annotations

import logging
from pathlib import Path

from homeassistant.components import frontend, panel_custom
from homeassistant.components.http import StaticPathConfig
from homeassistant.core import HomeAssistant

from .const import (
    DOMAIN,
    PANEL_ICON,
    PANEL_JS_FILE,
    PANEL_MODULE,
    PANEL_TITLE,
    PANEL_URL_PATH,
    PANEL_WEBCOMPONENT,
    STATIC_REGISTERED,
)

_LOGGER = logging.getLogger(__name__)


async def async_register(hass: HomeAssistant, gallery_url: str) -> bool:
    """Add the sidebar panel. Returns True if this call is the one that owns it."""
    if not hass.data.get(STATIC_REGISTERED):
        await hass.http.async_register_static_paths(
            [
                StaticPathConfig(
                    PANEL_MODULE,
                    hass.config.path(f"custom_components/{DOMAIN}/{PANEL_JS_FILE}"),
                    # No cache headers: the file changes when the integration is
                    # updated, and a stale cached panel is a confusing failure.
                    False,
                )
            ]
        )
        hass.data[STATIC_REGISTERED] = True

    if frontend.async_panel_exists(hass, PANEL_URL_PATH):
        return False

    await panel_custom.async_register_panel(
        hass,
        frontend_url_path=PANEL_URL_PATH,
        webcomponent_name=PANEL_WEBCOMPONENT,
        # Versioned by the file's own mtime. Browsers cache ES modules hard and Home
        # Assistant's service worker caches static assets, so without this an updated
        # panel keeps running the old code until someone clears their cache — which is
        # exactly what happens after every HACS update.
        module_url=f"{PANEL_MODULE}?v={_module_version(hass)}",
        sidebar_title=PANEL_TITLE,
        sidebar_icon=PANEL_ICON,
        # The page is served by the frame, not by us, so it has to run in an iframe.
        embed_iframe=False,
        require_admin=False,
        config={"url": gallery_url},
    )
    _LOGGER.debug("Registered the %s panel -> %s", PANEL_URL_PATH, gallery_url)
    return True


def _module_version(hass: HomeAssistant) -> int:
    try:
        return int(Path(hass.config.path(
            f"custom_components/{DOMAIN}/{PANEL_JS_FILE}")).stat().st_mtime)
    except OSError:
        return 0


def async_remove(hass: HomeAssistant) -> None:
    if frontend.async_panel_exists(hass, PANEL_URL_PATH):
        frontend.async_remove_panel(hass, PANEL_URL_PATH)
