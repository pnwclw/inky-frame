"""MQTT bridge to Home Assistant: the frame's control plane.

The split is deliberate (see CLAUDE.md §6): **MQTT carries control and state, HTTP
carries pixels.** Photos are megabytes — a retained image topic would be re-sent to
every subscriber on connect, there is no back-pressure for a 30 s refresh, and iOS
Shortcuts can't speak MQTT anyway. So nothing here ever moves an image; the `image`
entity publishes a *URL* that Home Assistant fetches over HTTP.

Topics (``<base>`` = MQTT_BASE_TOPIC, default ``inky-frame``):

    <base>/availability     pub, retained, LWT   online | offline
    <base>/button/A..D      pub                  PRESS
    <base>/state            pub, retained        JSON: what the panel is doing
    <base>/prefs            pub, retained        JSON: current device settings
    <base>/cmd/nav          sub                  next | prev | random
    <base>/cmd/show         sub                  a photo id
    <base>/cmd/url          sub                  an http(s) image URL
    <base>/cmd/dashboard    sub                  a dashboard name
    <base>/cmd/clear        sub                  {} or {"cycles": 3, "delay": 1.5}
    <base>/cmd/prefs        sub                  {"orientation": "portrait"}
    <base>/command          sub                  legacy alias: "verb:payload"

**This module no longer defines any Home Assistant entities.** They all live in the
custom integration (``homeassistant/custom_components/inky_frame``), which reads the
same topics and drives the same HTTP API — one place describing the device instead of
two, and no hand-written discovery JSON. What stays here is pure transport: the
retained state/prefs snapshots, the physical button presses, and the command topics.
The one leftover duty is clearing the discovery configs this module used to publish;
see ``_retire_legacy_discovery``.

This module owns no application state: main.py calls ``publish_state`` /
``publish_prefs`` when something changes.
"""

from __future__ import annotations

import json
import logging

import paho.mqtt.client as mqtt

from .config import Settings

log = logging.getLogger(__name__)

BUTTON_LABELS = ("A", "B", "C", "D")


class MqttBridge:
    def __init__(self, settings: Settings, on_command=None):
        self.settings = settings
        # on_command(verb, payload) — called on paho's NETWORK THREAD. The handler in
        # main.py hops to the event loop with run_coroutine_threadsafe.
        self.on_command = on_command
        self.connected = False
        self._client: mqtt.Client | None = None
        self.base = settings.mqtt_base_topic
        self.availability_topic = f"{self.base}/availability"
        self.state_topic = f"{self.base}/state"
        self.prefs_topic = f"{self.base}/prefs"
        self.command_topic = f"{self.base}/command"  # legacy free-form
        self.cmd_prefix = f"{self.base}/cmd"

        # Last state/prefs we published, replayed on reconnect so HA is never blank.
        self._last_state: dict | None = None
        self._last_prefs: dict | None = None

    def start(self) -> None:
        s = self.settings
        if not s.mqtt_enabled:
            log.info("MQTT disabled (MQTT_ENABLED=0)")
            return
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=s.mqtt_client_id)
        if s.mqtt_username:
            client.username_pw_set(s.mqtt_username, s.mqtt_password)
        client.will_set(self.availability_topic, "offline", qos=1, retain=True)
        client.on_connect = self._on_connect
        client.on_disconnect = self._on_disconnect
        client.on_message = self._on_message
        client.connect_async(s.mqtt_host, s.mqtt_port, keepalive=60)
        client.loop_start()  # background network thread
        self._client = client
        log.info("MQTT connecting to %s:%s", s.mqtt_host, s.mqtt_port)

    def stop(self) -> None:
        if self._client is None:
            return
        self.publish(self.availability_topic, "offline", retain=True)
        self._client.loop_stop()
        self._client.disconnect()

    # -- callbacks -----------------------------------------------------------
    def _on_connect(self, client, userdata, flags, reason_code, properties=None):
        self.connected = reason_code == 0
        log.info("MQTT connected (rc=%s)", reason_code)
        self.publish(self.availability_topic, "online", retain=True)
        client.subscribe([(self.command_topic, self.settings.mqtt_qos),
                          (f"{self.cmd_prefix}/#", self.settings.mqtt_qos)])
        self._retire_legacy_discovery()
        # Retained topics survive the broker, but not a broker restart — replay what
        # we know so the entities aren't stuck as unknown after a reconnect.
        if self._last_prefs is not None:
            self.publish_prefs(self._last_prefs)
        if self._last_state is not None:
            self.publish_state(self._last_state)

    def _on_disconnect(self, client, userdata, *args):
        self.connected = False
        log.warning("MQTT disconnected")

    def _on_message(self, client, userdata, msg):
        payload = msg.payload.decode("utf-8", "replace").strip()
        if msg.topic == self.command_topic:
            verb, _, rest = payload.partition(":")  # legacy "dashboard:guest_wifi"
            verb, payload = verb.strip().lower(), rest.strip()
        elif msg.topic.startswith(f"{self.cmd_prefix}/"):
            verb = msg.topic[len(self.cmd_prefix) + 1:].strip().lower()
        else:
            return
        log.info("MQTT command %s = %r", verb, payload)
        if self.on_command:
            try:
                self.on_command(verb, payload)
            except Exception:  # noqa: BLE001
                log.exception("command handler failed")

    # -- publishing ----------------------------------------------------------
    def publish(self, topic: str, payload: str, retain: bool = False) -> None:
        if self._client is not None:
            self._client.publish(topic, payload, qos=self.settings.mqtt_qos, retain=retain)

    def publish_button(self, label: str) -> None:
        topic = f"{self.base}/button/{label}"
        self.publish(topic, "PRESS")
        log.info("button %s -> %s", label, topic)

    def publish_state(self, state: dict) -> None:
        """Retained snapshot of what the panel is doing. Safe to call from any thread
        (paho's publish is thread-safe) — the refresh worker calls it on both edges."""
        self._last_state = state
        self.publish(self.state_topic, json.dumps(state), retain=True)

    def publish_prefs(self, prefs: dict) -> None:
        self._last_prefs = prefs
        self.publish(self.prefs_topic, json.dumps(prefs), retain=True)

    # -- retiring the entities this service used to publish -------------------
    # Entity definitions now live in the Home Assistant custom integration
    # (homeassistant/custom_components/inky_frame), so this service publishes NO
    # discovery configs. Their old configs are RETAINED on the broker, though, so
    # Home Assistant would resurrect every one of them on the next connect. Clearing
    # them is therefore not a one-off migration but a standing duty: publish an empty
    # payload to each on every connect. It costs nothing on a broker that no longer
    # holds them.
    LEGACY_ENTITIES: tuple[tuple[str, str], ...] = (
        ("device_automation", "button_a"),
        ("device_automation", "button_b"),
        ("device_automation", "button_c"),
        ("device_automation", "button_d"),
        ("sensor", "status"),
        ("binary_sensor", "busy"),
        ("image", "current"),
        ("select", "orientation"),
        ("select", "fit"),
        ("select", "dither"),
        ("select", "recent"),
        ("number", "auto_crop"),
        ("button", "next"),
        ("button", "prev"),
        ("button", "random"),
        ("button", "clear"),
        ("button", "deghost"),
        ("button", "dashboard_guest_wifi"),
    )

    def _retire_legacy_discovery(self) -> None:
        node = self.settings.device_id
        prefix = self.settings.mqtt_discovery_prefix
        for component, object_id in self.LEGACY_ENTITIES:
            self.publish(f"{prefix}/{component}/{node}/{object_id}/config", "", retain=True)
        log.info("Cleared %d legacy Home Assistant discovery configs for %s",
                 len(self.LEGACY_ENTITIES), node)
