"""Constants for the Inky Frame integration."""

from __future__ import annotations

from datetime import timedelta

DOMAIN = "inky_frame"

# The device this integration attaches to is created by MQTT discovery, and the MQTT
# integration namespaces the `device.identifiers` from a discovery payload under its
# own domain — a live device registry entry reads `identifiers: [["mqtt",
# "inky_frame_13"]]`. Matching that exact tuple is what merges our media_player into
# the SAME device card instead of creating a twin. Declared as a literal rather than
# imported from homeassistant.components.mqtt so this integration still loads when the
# MQTT integration isn't set up (it then simply creates the device itself).
MQTT_DOMAIN = "mqtt"

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8080

# The frame refreshes in ~30 s and its state also rides on MQTT, so there is nothing
# to gain from polling harder than this.
SCAN_INTERVAL = timedelta(seconds=30)

# How many library photos to offer in the media browser.
LIBRARY_LIMIT = 500

# media_content_id prefix for a photo held by the frame's own library, as opposed to
# a `media-source://…` id coming from somewhere else in Home Assistant.
LIBRARY_PREFIX = "library/"
ROOT_ID = "root"

# Physical buttons on the panel. The frame publishes a press to
# `<base>/button/<label>`; there is no HTTP equivalent, so these are the one thing
# this integration genuinely needs MQTT for.
PANEL_BUTTONS = ("A", "B", "C", "D")

# --- the sidebar panel (panel.py) ---
PANEL_URL_PATH = "inky-frame"
PANEL_TITLE = "Inky Frame"
PANEL_ICON = "mdi:image-frame"
PANEL_WEBCOMPONENT = "inky-frame-panel"
PANEL_JS_FILE = "inky-frame-panel.js"
# One file, served at one URL — no directory, so nothing shadows the panel.py module.
PANEL_MODULE = f"/{DOMAIN}/{PANEL_JS_FILE}"
STATIC_REGISTERED = f"{DOMAIN}_static_registered"
# Config-entry option: the address a BROWSER can reach the gallery on. Not the address
# this integration talks to — Home Assistant may reach the frame on localhost while the
# browser needs a routable https:// URL.
CONF_GALLERY_URL = "gallery_url"
