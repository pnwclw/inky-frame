"""The frame as a media_player: browse the library, play a photo onto the panel.

Two things Home Assistant's built-in media browser cannot do on its own, and the
reason this entity exists:

* **Thumbnails.** The `local_source` media source never sets `thumbnail` (grep it —
  the word does not appear), so a folder of renders mounted under /media browses as a
  list of filenames. Here we set it, and let Home Assistant proxy the image with
  `get_browse_image_url()` + `async_get_browse_image()`. That keeps the frame's URL
  out of the frontend entirely: the browser only ever talks to Home Assistant, so it
  works over HTTPS, from outside the LAN, with Home Assistant's own auth.
* **Playing.** A media browser can only "play" to a media_player, and MQTT discovery
  has no media_player platform. With this entity, "Play on Inky Frame" appears on the
  frame's own photos *and* on every other Home Assistant media source.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from homeassistant.components import media_source
from homeassistant.components.media_player import (
    BrowseError,
    BrowseMedia,
    MediaClass,
    MediaPlayerEntity,
    MediaPlayerEntityFeature,
    MediaPlayerState,
    MediaType,
    async_process_play_media_url,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import InkyFrameConfigEntry, InkyFrameCoordinator
from .api import InkyFrameError
from .const import LIBRARY_LIMIT, LIBRARY_PREFIX, ROOT_ID
from .entity import InkyFrameEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: InkyFrameConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    async_add_entities([InkyFrameMediaPlayer(entry.runtime_data)])


def _is_image(item: BrowseMedia) -> bool:
    return str(item.media_content_type or "").startswith("image/")


def _photo_title(photo: dict) -> str:
    if photo.get("name"):
        return str(photo["name"])
    added = photo.get("added_at")
    if added:
        return datetime.fromtimestamp(added).strftime("%d %b %H:%M")
    return str(photo.get("id", "photo"))


class InkyFrameMediaPlayer(InkyFrameEntity, MediaPlayerEntity):
    _attr_name = None  # this entity IS the device, so it takes the device's name
    _attr_media_content_type = MediaType.IMAGE
    _attr_supported_features = (
        MediaPlayerEntityFeature.PLAY_MEDIA
        | MediaPlayerEntityFeature.BROWSE_MEDIA
        | MediaPlayerEntityFeature.NEXT_TRACK
        | MediaPlayerEntityFeature.PREVIOUS_TRACK
    )

    def __init__(self, coordinator: InkyFrameCoordinator) -> None:
        super().__init__(coordinator, "media_player")

    # -- state ---------------------------------------------------------------
    @property
    def state(self) -> MediaPlayerState:
        if self._display.get("busy"):
            # e-paper takes ~30 s to redraw; that is genuinely "buffering".
            return MediaPlayerState.BUFFERING
        return (
            MediaPlayerState.PLAYING
            if self._library.get("current_id")
            else MediaPlayerState.IDLE
        )

    @property
    def media_content_id(self) -> str | None:
        current = self._library.get("current_id")
        return f"{LIBRARY_PREFIX}{current}" if current else None

    @property
    def media_title(self) -> str | None:
        return self._library.get("current_name") or self._library.get("current_id")

    @property
    def media_image_url(self) -> str | None:
        # ?view=true: a render is the panel BUFFER, always landscape, so a portrait
        # mount would otherwise show sideways here. media_image_remotely_accessible
        # stays False, so Home Assistant fetches this itself and serves it on — the
        # frontend never needs to reach the frame.
        version = int(self._display.get("last_shown_at") or 0)
        return self.coordinator.client.url(f"/display/preview?view=true&v={version}")

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "photo_id": self._library.get("current_id"),
            "library_count": self._library.get("count"),
            "last_error": self._display.get("last_error"),
        }

    # -- browsing ------------------------------------------------------------
    async def async_browse_media(
        self,
        media_content_type: MediaType | str | None = None,
        media_content_id: str | None = None,
    ) -> BrowseMedia:
        if media_content_id and media_source.is_media_source_id(media_content_id):
            return await media_source.async_browse_media(
                self.hass, media_content_id, content_filter=_is_image
            )
        if media_content_id in (None, "", ROOT_ID):
            return await self._async_browse_root()
        raise BrowseError(f"Cannot browse {media_content_id}")

    async def _async_browse_root(self) -> BrowseMedia:
        try:
            library = await self.coordinator.client.library(LIBRARY_LIMIT)
        except InkyFrameError as err:
            raise BrowseError(str(err)) from err

        children: list[BrowseMedia] = [
            self._photo_item(photo) for photo in library.get("photos", [])
        ]
        try:
            # Everything else Home Assistant can offer, so anything with an image can
            # be sent to the frame — not just what the frame already holds.
            children.append(
                await media_source.async_browse_media(
                    self.hass, None, content_filter=_is_image
                )
            )
        except BrowseError:
            pass

        return BrowseMedia(
            media_class=MediaClass.DIRECTORY,
            media_content_id=ROOT_ID,
            media_content_type=MediaType.IMAGE,
            title=self.device_entry.name if self.device_entry else "Inky Frame",
            can_play=False,
            can_expand=True,
            children=children,
            children_media_class=MediaClass.IMAGE,
        )

    def _photo_item(self, photo: dict) -> BrowseMedia:
        content_id = f"{LIBRARY_PREFIX}{photo['id']}"
        return BrowseMedia(
            media_class=MediaClass.IMAGE,
            media_content_id=content_id,
            media_content_type=MediaType.IMAGE,
            title=_photo_title(photo),
            can_play=True,
            can_expand=False,
            # Routed through Home Assistant's own media_player_proxy, which calls
            # async_get_browse_image() below.
            thumbnail=self.get_browse_image_url(MediaType.IMAGE, content_id),
        )

    async def async_get_browse_image(
        self,
        media_content_type: MediaType | str,
        media_content_id: str,
        media_image_id: str | None = None,
    ) -> tuple[bytes | None, str | None]:
        if not media_content_id.startswith(LIBRARY_PREFIX):
            return None, None
        photo_id = media_content_id[len(LIBRARY_PREFIX):]
        try:
            return await self.coordinator.client.fetch(f"/library/{photo_id}/thumb")
        except InkyFrameError:
            return None, None

    # -- commands ------------------------------------------------------------
    async def async_play_media(
        self, media_type: MediaType | str, media_id: str, **kwargs: Any
    ) -> None:
        client = self.coordinator.client
        try:
            if media_source.is_media_source_id(media_id):
                # Resolve to a URL and hand the frame the URL — the bytes go straight
                # from Home Assistant to the frame over HTTP, never through here.
                play = await media_source.async_resolve_media(
                    self.hass, media_id, self.entity_id
                )
                await client.show_url(async_process_play_media_url(self.hass, play.url))
            elif media_id.startswith(LIBRARY_PREFIX):
                await client.show_photo(media_id[len(LIBRARY_PREFIX):])
            elif media_id.startswith(("http://", "https://")):
                await client.show_url(media_id)
            else:
                raise HomeAssistantError(
                    f"Don't know how to show {media_id!r} on the frame"
                )
        except InkyFrameError as err:
            raise HomeAssistantError(str(err)) from err
        await self.coordinator.async_request_refresh()

    async def async_media_next_track(self) -> None:
        await self._nav("next")

    async def async_media_previous_track(self) -> None:
        await self._nav("prev")

    async def _nav(self, direction: str) -> None:
        try:
            await self.coordinator.client.nav(direction)
        except InkyFrameError as err:
            raise HomeAssistantError(str(err)) from err
        await self.coordinator.async_request_refresh()
