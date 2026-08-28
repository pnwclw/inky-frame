"""Thin async client for the frame's HTTP API.

The frame is LAN-only and unauthenticated by design (CLAUDE.md §1), so this is just
aiohttp plus error wrapping. It deliberately holds no state: the coordinator owns
`GET /status`, and the entity calls the rest on demand.
"""

from __future__ import annotations

from typing import Any

import aiohttp


class InkyFrameError(Exception):
    """The frame could not be reached, or answered with an error."""


class InkyFrameClient:
    def __init__(self, session: aiohttp.ClientSession, base_url: str) -> None:
        self._session = session
        self.base_url = base_url.rstrip("/")

    def url(self, path: str) -> str:
        return f"{self.base_url}{path}"

    async def _request(self, method: str, path: str, **kwargs: Any) -> aiohttp.ClientResponse:
        try:
            response = await self._session.request(
                method, self.url(path), timeout=aiohttp.ClientTimeout(total=20), **kwargs
            )
        except (TimeoutError, aiohttp.ClientError) as err:
            raise InkyFrameError(f"{method} {path} failed: {err}") from err
        if response.status >= 400:
            detail = await response.text()
            response.release()
            raise InkyFrameError(f"{method} {path} returned {response.status}: {detail[:200]}")
        return response

    async def _json(self, method: str, path: str, **kwargs: Any) -> dict:
        response = await self._request(method, path, **kwargs)
        async with response:
            return await response.json()

    # -- reads ---------------------------------------------------------------
    async def status(self) -> dict:
        return await self._json("GET", "/status")

    async def library(self, limit: int) -> dict:
        return await self._json("GET", f"/library?limit={limit}&offset=0")

    async def fetch(self, path: str) -> tuple[bytes | None, str | None]:
        """Raw bytes + content type, for proxying a thumbnail through Home Assistant."""
        response = await self._request("GET", path)
        async with response:
            return await response.read(), response.headers.get("Content-Type")

    # -- commands ------------------------------------------------------------
    async def show_photo(self, photo_id: str) -> None:
        await self._json("POST", f"/display/library/{photo_id}")

    async def show_url(self, url: str) -> None:
        await self._json("POST", "/display/url", params={"url": url})

    async def nav(self, direction: str) -> None:
        await self._json("POST", "/display/nav", params={"direction": direction})

    async def set_prefs(self, changes: dict) -> dict:
        return await self._json("PATCH", "/prefs", json=changes)

    async def clear(self, cycles: int = 1) -> None:
        """cycles=1 is a single white flush; more runs the deep de-ghost, which holds
        the panel for minutes — the frame does it in the background either way."""
        await self._json("POST", "/display/clear", params={"cycles": cycles})

    async def dashboard(self, name: str) -> None:
        await self._json("POST", "/display/dashboard", params={"name": name})
