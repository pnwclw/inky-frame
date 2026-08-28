"""FastAPI entrypoint: wires display + buttons + MQTT together.

Endpoints:
  GET  /healthz                 -> liveness
  GET  /status                  -> display + MQTT status
  POST /display/image           -> accept an image (raw body OR multipart "file"),
                                    dither it, show it
  POST /display/clear           -> blank the panel to white
  POST /display/dashboard       -> render + show a dashboard (stub)
  POST /display/url             -> fetch an image by URL, dither it, show it
  POST /display/library/<id>    -> show a stored photo (a saved render, or a fresh one)
  POST /display/nav             -> show the next/previous/random library photo
  GET  /display/preview         -> last rendered image as PNG
  POST /library                 -> upload a photo WITHOUT rendering or showing it
  GET  /library                 -> paged index (?collection=<id>)
  GET  /library/<id>            -> one photo, with every render made from it
  PATCH /library/<id>           -> rename / set which collections it is in
  GET  /library/<id>/original|thumb
  POST /library/<id>/render     -> make (or reuse) a render with given parameters
  GET  /library/<id>/renders/<key>[/thumb]
  DELETE /library/<id>/renders/<key>
  DELETE /library/<id>          -> forget a photo (index + all its files)
  GET/POST /collections, PATCH/DELETE /collections/<id>
  GET  /                        -> the gallery (also served at /gallery)
  GET  /setup                   -> iOS Shortcut download + instructions
  GET  /prefs                   -> device settings + their valid values
  PATCH /prefs                  -> change device settings (also editable from HA)
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import math
import os
import socket
import urllib.parse
import urllib.request
from contextlib import asynccontextmanager
from datetime import datetime

from fastapi import Body, FastAPI, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, Response
from PIL import Image, UnidentifiedImageError

from .buttons import ButtonWatcher
from .config import settings
from .dashboards import DashboardRenderer
from .display import DisplayManager
from .dithering import (
    AVAILABLE_DITHER_MODES,
    default_crop,
    normalise_angle,
    resolve_dither_mode,
    rotated_size,
    working_canvas,
)
from .gallery import PAGE as GALLERY_PAGE
from .gpio_compat import install as install_gpio_compat
from .gpio_compat import install_busy_wait_fix
from .library import PhotoEntry, PhotoLibrary
from .mqtt import MqttBridge
from .prefs import FIT_MODES, ORIENTATIONS, Prefs
from .shortcut import MENU_OPTIONS, build_shortcut_plist

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
)
log = logging.getLogger("inky-frame")

display = DisplayManager(settings)
dashboards = DashboardRenderer(settings, size=display.size)
library = PhotoLibrary(settings, size=display.size)
prefs = Prefs(settings)
mqtt_bridge = MqttBridge(settings)
buttons = ButtonWatcher(settings, on_press=mqtt_bridge.publish_button)

# Set in the lifespan. MQTT callbacks arrive on paho's network thread and need the
# loop to hand work back to; the base URL is what Home Assistant fetches previews on.
APP_LOOP: asyncio.AbstractEventLoop | None = None
PUBLIC_BASE_URL = ""


def _primary_ip() -> str:
    """The LAN address other machines reach this host on. Connecting a UDP socket
    sends no packets — it just asks the kernel which source address the default route
    would use, which beats gethostbyname() inside a container."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 80))
        return sock.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        sock.close()


def _resolve_public_base_url() -> str:
    if settings.public_base_url:
        return settings.public_base_url.rstrip("/")
    return f"http://{_primary_ip()}:{settings.port}"


@asynccontextmanager
async def lifespan(_app: FastAPI):
    global APP_LOOP, PUBLIC_BASE_URL
    APP_LOOP = asyncio.get_running_loop()
    PUBLIC_BASE_URL = _resolve_public_base_url()
    if not settings.inky_mock:
        install_gpio_compat()  # tolerate the container's missing /proc/device-tree
        # poll BUSY to completion — stock POF truncates the refresh. Patch the active
        # panel's driver class with its own (panel-specific) ceiling.
        install_busy_wait_fix(settings.panel_spec.driver_class, settings.resolved_busy_ceiling)
    display.init_driver()
    dashboards.size = display.size  # driver may report the real resolution
    library.size = display.size     # sizes archived originals against the real panel

    # Wire the callbacks now that everything exists.
    display.on_state_change = lambda _status: publish_state()
    prefs.on_change = _on_prefs_changed
    mqtt_bridge.on_command = _handle_command
    # Seed the bridge before connecting: _on_connect replays the last state/prefs it
    # was given, so a subscriber is never blank while waiting for the first change.
    mqtt_bridge.publish_prefs(prefs.as_dict())
    publish_state()
    mqtt_bridge.start()
    buttons.start()
    log.info("inky-frame ready on %s:%s (public %s)", settings.host, settings.port,
             PUBLIC_BASE_URL)
    try:
        yield
    finally:
        buttons.stop()
        mqtt_bridge.stop()


app = FastAPI(title="Inky Frame Dashboard", version="0.1.0", lifespan=lifespan)


async def _read_image_bytes(request: Request, file: UploadFile | None) -> bytes:
    if file is not None:
        return await file.read()
    body = await request.body()
    if not body:
        raise HTTPException(400, "No image. Send the raw image as the request body "
                                 "or as multipart/form-data field 'file'.")
    return body


def _resolve_wait(wait: bool | None) -> bool:
    return settings.display_wait_default if wait is None else wait


def _image_endpoint(request: Request) -> str:
    """Full POST URL baked into the shortcut, derived from how /setup was opened."""
    return f"{request.url.scheme}://{request.url.netloc}/display/image"


SHORTCUT_PATH = "/inky-frame.shortcut"


@app.get("/", response_class=HTMLResponse)
@app.get("/gallery", response_class=HTMLResponse)
async def gallery():
    """The frame's own picker: thumbnail grid, tap to show, drop/pick to upload.
    Static page, all URLs relative — see app/gallery.py for why this exists rather
    than a Home Assistant dashboard.

    Served at BOTH `/` (it is the only page anyone wants) and `/gallery` — the latter
    is what Home Assistant's Webpage panel points at, so it has to keep working."""
    return HTMLResponse(GALLERY_PAGE)


@app.get("/setup", response_class=HTMLResponse)
async def setup_page(request: Request):
    options = "".join(f"<li>{label}</li>" for label, _, _ in MENU_OPTIONS)
    image_url = _image_endpoint(request)
    return f"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Inky Frame — setup</title>
<style>
  body{{font:16px/1.5 -apple-system,system-ui,sans-serif;max-width:42rem;margin:2rem auto;padding:0 1rem;color:#111}}
  h1{{font-size:1.5rem}} code{{background:#f2f2f2;padding:.1em .3em;border-radius:4px}}
  .btn{{display:inline-block;background:#0a84ff;color:#fff;text-decoration:none;padding:.7rem 1.2rem;border-radius:12px;font-weight:600;margin:.5rem 0}}
  ol{{padding-left:1.2rem}} li{{margin:.3rem 0}} .muted{{color:#666;font-size:.9rem}}
  details{{margin-top:1.5rem}} summary{{cursor:pointer;font-weight:600}}
</style></head><body>
<h1>📷 Inky Frame — iOS Shortcut</h1>
<p><a href="/">← Back to the gallery</a>, where you can also just upload photos.</p>
<p>The Shortcut is optional: it puts the frame in the iOS share sheet. It downloads
automatically; if it doesn't, tap the button.</p>
<p><a class="btn" href="{SHORTCUT_PATH}">⬇︎ Download “Inky Frame” shortcut</a></p>

<ol>
  <li>On your iPhone, first enable untrusted shortcuts:
      <b>Settings → Shortcuts → Allow Untrusted Shortcuts</b>
      (run any shortcut once if the toggle is missing).</li>
  <li>Open the downloaded file → <b>Add Shortcut</b>.</li>
  <li>Run it (or share a photo to it). It asks how to place the image:
      <ul>{options}</ul></li>
</ol>
<p class="muted">The shortcut posts to <code>{image_url}</code>. Portrait rotates the
image 90° for a vertically-mounted frame; Cover fills &amp; crops, Fit centers with
white margins.</p>

<details>
  <summary>Manual setup (if the download won't import)</summary>
  <p>Create a shortcut with these actions:</p>
  <ol>
    <li><b>Select Photos</b> (or use “Receive Images from Share Sheet”).</li>
    <li><b>Get Contents of URL</b> →
      URL <code>{image_url}?fit=cover&amp;orientation=landscape</code>,
      Method <b>POST</b>, Request Body <b>File</b> = the photo.</li>
  </ol>
  <p class="muted">Change <code>fit</code> to <code>contain</code> and/or
  <code>orientation</code> to <code>portrait</code> as you like.</p>
</details>
<script>
  // Auto-start the download (the attachment response downloads without
  // navigating away). The button above is the manual fallback.
  window.addEventListener('load', function () {{
    var a = document.createElement('a');
    a.href = '{SHORTCUT_PATH}';
    document.body.appendChild(a);
    a.click();
    a.remove();
  }});
</script>
</body></html>"""


@app.get(SHORTCUT_PATH)
async def shortcut(request: Request):
    plist = build_shortcut_plist(_image_endpoint(request))
    return Response(
        content=plist,
        media_type="application/octet-stream",
        headers={"Content-Disposition": 'attachment; filename="Inky Frame.shortcut"'},
    )


@app.get("/healthz")
async def healthz():
    return {"status": "ok"}


@app.get("/status")
async def status():
    return {
        # Identity, so a Home Assistant integration can discover which device to
        # attach to instead of hardcoding it (CLAUDE.md §6).
        "device": {"id": settings.device_id, "name": settings.device_name,
                   "base_url": PUBLIC_BASE_URL,
                   "model": settings.panel_spec.ha_model,
                   # So the integration can subscribe for push state instead of
                   # polling, without being told the topic separately.
                   "mqtt_base_topic": settings.mqtt_base_topic},
        "display": display.status(),
        "dither": {"default": settings.dither_mode, "available": list(AVAILABLE_DITHER_MODES)},
        "dashboards": dashboards.available(),
        "prefs": prefs.as_dict(),
        "library": library.stats(),
        "mqtt_connected": mqtt_bridge.connected,
    }


# ---------------------------------------------------------------------------
# Shared plumbing for the "show a photo" endpoints
# ---------------------------------------------------------------------------
def _decode_image(data: bytes) -> Image.Image:
    try:
        img = Image.open(io.BytesIO(data))
        img.load()
    except (UnidentifiedImageError, OSError):
        raise HTTPException(400, "Could not decode image")
    return img


def _validate_dither(name: str | None) -> None:
    if not name:
        return
    try:
        resolve_dither_mode(name)
    except ValueError as exc:
        raise HTTPException(400, str(exc))


def _require_library() -> None:
    if not library.enabled:
        raise HTTPException(503, "Photo library is disabled (LIBRARY_ENABLED=0)")


def _require_entry(photo_id: str) -> PhotoEntry:
    _require_library()
    entry = library.get(photo_id)
    if entry is None:
        raise HTTPException(404, f"No photo {photo_id!r} in the library")
    return entry


def _fetch_url(url: str) -> bytes:
    """GET an image for POST /display/url, in a worker thread.

    The service is LAN-only and trusted (CLAUDE.md §1), so this stays deliberately
    small: an http(s) scheme allow-list, a timeout, and a hard size cap so a wrong
    URL can't stream gigabytes into memory. stdlib urllib keeps the arm64 image
    free of another dependency for one GET.
    """
    if urllib.parse.urlparse(url).scheme not in ("http", "https"):
        raise ValueError("Only http:// and https:// URLs are supported")
    cap = settings.url_fetch_max_bytes
    request = urllib.request.Request(url, headers={"User-Agent": "inky-frame"})
    with urllib.request.urlopen(request, timeout=settings.url_fetch_timeout) as response:
        declared = response.headers.get("Content-Length")
        if declared and declared.isdigit() and int(declared) > cap:
            raise ValueError(f"Image is larger than the {cap} byte URL_FETCH_MAX_BYTES limit")
        data = response.read(cap + 1)  # one byte past the cap detects an overrun
    if len(data) > cap:
        raise ValueError(f"Image is larger than the {cap} byte URL_FETCH_MAX_BYTES limit")
    return data


def _resolve_placement(
    image_size: tuple[int, int],
    fit: str | None,
    orientation: str | None,
    rotate: int = 0,
) -> tuple[str, str, str]:
    """Turn the request's (possibly absent) fit/orientation into concrete values.

    Both fall back to the DEVICE settings (app/prefs.py), not to a hardcoded default:
    orientation describes how the frame is mounted, and `fit=auto` picks cover only
    while cover wouldn't crop too much of *this* photo — measured on the photo AFTER
    rotation, since turning it 90° swaps its aspect ratio. Returns
    (fit, orientation, fit_mode) where fit_mode is what was asked for."""
    orientation = orientation or prefs.orientation
    fit_mode = (fit or prefs.fit).lower()
    canvas = working_canvas(display.size, orientation)
    resolved = prefs.resolve_fit(fit, rotated_size(image_size, rotate), canvas)
    return resolved, orientation, fit_mode


def _parse_crop(raw: str | None) -> list[float] | None:
    """`x,y,w,h` in pixels of the ROTATED photo. It may sit partly outside the photo —
    that is how "contain" and everything the user drags to are expressed."""
    if not raw:
        return None
    try:
        box = [float(v) for v in raw.split(",")]
    except ValueError:
        raise HTTPException(400, "crop must be four numbers: x,y,w,h")
    if len(box) != 4:
        raise HTTPException(400, "crop must be four numbers: x,y,w,h")
    if box[2] <= 0 or box[3] <= 0:
        raise HTTPException(400, "crop width and height must be positive")
    return box


def _validate_rotate(rotate: float) -> float:
    """Degrees clockwise, any angle — the crop editor levels horizons by hand. Folded
    into [0, 360) and rounded to a tenth so the render key stays stable."""
    if not math.isfinite(rotate):
        raise HTTPException(400, "rotate must be a finite number of degrees")
    return normalise_angle(rotate)


async def _render_and_store(
    data: bytes,
    image: Image.Image,
    *,
    fit: str | None,
    orientation: str | None,
    dither: str | None,
    rotate: float = 0.0,
    crop: list[float] | None = None,
    show: bool,
    wait: bool,
    store: bool,
    source: str,
    name: str | None = None,
    url: str | None = None,
    coalesce: bool = False,
) -> dict:
    """Shared tail of every "here is a NEW photo" endpoint: render it, optionally push
    it to the panel, optionally file it (and its render) in the library."""
    fit, orientation, fit_mode = _resolve_placement(image.size, fit, orientation, rotate)
    # prefs.dither, not settings.dither_mode: the stored pref is what Home Assistant
    # edits, and the render must use exactly what we report back.
    effective_dither = (dither or prefs.dither).upper()
    canvas = working_canvas(display.size, orientation)
    crop = crop or default_crop(rotated_size(image.size, rotate), canvas, fit)
    if show:
        rendered = await display.show_image(
            image, fit=fit, orientation=orientation, wait=wait, mode=effective_dither,
            rotate=rotate, crop=crop, coalesce=coalesce)
    else:
        rendered = await display.render_preview(
            image, fit=fit, orientation=orientation, mode=effective_dither,
            rotate=rotate, crop=crop)

    body = {
        "rendered": True,
        "shown": show,
        "fit": fit,
        "fit_mode": fit_mode,
        "orientation": orientation,
        "rotate": rotate,
        "dither": effective_dither,
        "preview": "/display/preview",
    }
    if show:
        body["waited"] = wait
        body["status"] = display.status()
    if store and library.enabled:
        entry = await asyncio.to_thread(
            library.add_photo,
            data,
            source=source,
            name=name,
            url=url,
            image=image,  # already decoded; saves the library a second decode
        )
        if entry is not None:
            render = await asyncio.to_thread(
                library.record_render, entry.id, rendered,
                fit=fit, crop=crop, rotate=rotate, orientation=orientation,
                dither=effective_dither, shown=show,
            )
            body["photo"] = _photo_public(entry)
            body["render"] = render.public(entry.id) if render else None
    publish_state()
    return body


def _photo_public(entry: PhotoEntry) -> dict:
    return entry.public(library.collections_of(entry.id))


async def _render_stored(
    entry: PhotoEntry,
    *,
    fit: str | None = None,
    orientation: str | None = None,
    dither: str | None = None,
    rotate: float | None = None,
    crop: list[float] | None = None,
    show: bool = True,
    wait: bool = False,
    coalesce: bool = False,
) -> dict:
    """Render a photo the library already holds, from its archived original.

    Never pushes a stored render: the point of re-rendering is that the CURRENT
    dithering settings, mount and panel size apply, which is what makes an old photo
    survive a panel swap. Where the unspecified parameters come from is a deliberate
    split — **geometry follows the frame, look follows the photo.** fit and orientation
    come from the device prefs, because they describe how the frame hangs *now*;
    dither and rotation come from the photo's most recent render, because those were
    explicit choices about this picture."""
    data = await asyncio.to_thread(library.read_original, entry.id)
    if data is None:
        raise HTTPException(410, f"The original for {entry.id!r} is gone from disk")
    image = _decode_image(data)

    last = entry.renders[-1] if entry.renders else None
    if rotate is None:
        rotate = last.rotate if last else 0
    effective_dither = (dither or (last.dither if last else None) or prefs.dither).upper()
    try:  # a stored mode can go stale if epaper-dithering drops one
        resolve_dither_mode(effective_dither)
    except ValueError:
        log.warning("stored dither %r for %s is unknown; using %s",
                    effective_dither, entry.id, prefs.dither)
        effective_dither = prefs.dither
    fit, orientation, fit_mode = _resolve_placement(image.size, fit, orientation, rotate)
    canvas = working_canvas(display.size, orientation)
    source = rotated_size(image.size, rotate)
    if crop is None:
        # No rectangle given: reuse the one this photo was last placed with, as long as
        # it was for the same rotation and canvas — otherwise it means nothing here.
        if last and last.crop and last.rotate == rotate and last.orientation == orientation:
            crop, fit_mode = list(last.crop), last.fit
        else:
            crop = default_crop(source, canvas, fit)
    else:
        fit_mode = "custom"

    if show:
        rendered = await display.show_image(
            image, fit=fit, orientation=orientation, wait=wait, mode=effective_dither,
            rotate=rotate, crop=crop, coalesce=coalesce)
    else:
        rendered = await display.render_preview(
            image, fit=fit, orientation=orientation, mode=effective_dither,
            rotate=rotate, crop=crop)

    render = await asyncio.to_thread(
        library.record_render, entry.id, rendered, fit=fit_mode, crop=crop,
        rotate=rotate, orientation=orientation, dither=effective_dither, shown=show,
    )
    body = {
        "rendered": True,
        "shown": show,
        "fit": fit,
        "fit_mode": fit_mode,
        "orientation": orientation,
        "rotate": rotate,
        "crop": crop,
        "source": list(source),
        "canvas": list(canvas),
        "dither": effective_dither,
        "preview": "/display/preview",
        "photo": _photo_public(entry),
        "render": render.public(entry.id) if render else None,
    }
    if show:
        body["waited"] = wait
        body["status"] = display.status()
    publish_state()
    return body


async def _show_existing_render(
    entry: PhotoEntry, key: str, *, wait: bool = False, coalesce: bool = False
) -> dict:
    """Put a render the library already has straight back on the panel.

    The stored PNG *is* the panel buffer, so nothing is recomputed and the result is
    byte-for-byte the look that was picked. The one thing that can invalidate it is a
    panel swap, so a size mismatch falls back to re-rendering from the original."""
    render = library.get_render(entry.id, key)
    found = library.file_path(entry.id, "render", key)
    if render is None or found is None:
        raise HTTPException(404, f"No render {key!r} for photo {entry.id!r}")
    p_image = await asyncio.to_thread(_load_render, found[0])
    if tuple(p_image.size) != tuple(display.size):
        log.info("render %s is %s but the panel is %s; re-rendering",
                 key, p_image.size, display.size)
        return await _render_stored(
            entry, fit=render.fit, orientation=render.orientation,
            dither=render.dither, rotate=render.rotate, show=True,
            wait=wait, coalesce=coalesce,
        )
    await display.show_rendered(p_image, wait=wait, coalesce=coalesce)
    await asyncio.to_thread(library.set_current, entry.id, key)
    publish_state()
    return {
        "shown": True,
        "reused": True,
        "waited": wait,
        "fit": render.fit,
        "orientation": render.orientation,
        "rotate": render.rotate,
        "dither": render.dither,
        "preview": "/display/preview",
        "photo": _photo_public(entry),
        "render": render.public(entry.id),
        "status": display.status(),
    }


def _load_render(path) -> Image.Image:
    """Load a stored render, keeping mode "P" — the palette indices are what inky
    consumes, so converting here would undo the whole point (CLAUDE.md §3)."""
    with Image.open(path) as opened:
        opened.load()
        return opened.copy()


# ---------------------------------------------------------------------------
# MQTT: state out, commands in  (app/mqtt.py owns the topics, this owns the meaning)
# ---------------------------------------------------------------------------
def _current_photo() -> PhotoEntry | None:
    if not library.enabled or not library.current_id:
        return None
    return library.get(library.current_id)


def _state_payload() -> dict:
    """Everything Home Assistant needs in one retained JSON message. `status` is the
    sensor's state; the rest rides along as its attributes."""
    d = display.status()
    photo = _current_photo()
    shown_at = d["last_shown_at"]
    # last_shown_at is per-process, but e-paper keeps its image across a restart, so
    # after one it is None while the panel still shows something. Fall back to when
    # the preview was written, so the cache-busted URL stays meaningful.
    version = shown_at or d["preview_updated_at"]
    return {
        "status": "refreshing" if d["busy"] else ("error" if d["last_error"] else "idle"),
        "busy": d["busy"],
        "panel": d["panel"],
        "driver": d["driver"],
        "resolution": d["resolution"],
        "mock": d["mock"],
        "last_shown_at": shown_at,
        "last_shown_iso": (
            datetime.fromtimestamp(shown_at).astimezone().isoformat() if shown_at else None
        ),
        "last_error": d["last_error"],
        "photo_id": photo.id if photo else None,
        "photo_name": (photo.name or photo.id) if photo else None,
        "render_key": library.current_render,
        "library_count": library.stats()["count"],
        # The `image` entity fetches this over HTTP — no image bytes go through the
        # broker. The ?v= is a cache-buster; without it HA keeps the first frame.
        "image_url": f"{PUBLIC_BASE_URL}/display/preview?view=true&v={int(version or 0)}",
        **prefs.as_dict(),
    }


def publish_state() -> None:
    """Safe to call from any thread — DisplayManager fires it from its refresh
    worker on both edges of a refresh."""
    mqtt_bridge.publish_state(_state_payload())


def _on_prefs_changed(current: dict) -> None:
    mqtt_bridge.publish_prefs(current)
    publish_state()


async def _apply_prefs(changes: dict) -> dict:
    """Patch the device settings. Changing the MOUNTING also re-lays-out whatever is
    on the panel — otherwise the frame keeps showing a sideways image until the next
    photo arrives. The other settings are defaults for future photos, so they don't
    spend a 30 s refresh."""
    was = prefs.orientation
    updated = prefs.patch(changes)
    if updated["orientation"] != was:
        photo = _current_photo()
        if photo is not None:
            log.info("mounting %s -> %s; re-showing %s", was, updated["orientation"], photo.id)
            await _render_stored(photo, coalesce=True)
    return updated


def _handle_command(verb: str, payload: str) -> None:
    """paho calls this on its NETWORK thread; everything below touches the panel and
    the event loop, so hand it over rather than doing it here."""
    if APP_LOOP is None:
        log.warning("MQTT command %r arrived before startup finished; dropped", verb)
        return
    asyncio.run_coroutine_threadsafe(_run_command(verb, payload), APP_LOOP)


def _command_json(payload: str) -> dict:
    """Command payloads are JSON, but stay friendly to a hand-typed mosquitto_pub:
    an empty payload means {}, and `key=value` is accepted for a single setting."""
    text = (payload or "").strip()
    if not text:
        return {}
    if text.startswith("{"):
        return json.loads(text)
    key, sep, value = text.partition("=")
    if not sep:
        raise ValueError(f"expected JSON or key=value, got {text!r}")
    return {key.strip(): value.strip()}


async def _run_command(verb: str, payload: str) -> None:
    """One MQTT command. Panel-touching commands are COALESCED (see
    DisplayManager._queue_latest): five quick taps on "Next photo" must land on the
    fifth photo, not crawl through all five at 30 s apiece."""
    try:
        if verb == "nav":
            direction = (payload or "next").lower()
            if direction not in ("next", "prev", "random"):
                raise ValueError(f"nav takes next|prev|random, got {payload!r}")
            _require_library()
            photo = library.neighbour(direction)
            if photo is None:
                log.warning("nav %s: the library is empty", direction)
                return
            await _render_stored(photo, coalesce=True)
        elif verb == "show":
            photo = _require_entry(payload)
            await _render_stored(photo, coalesce=True)
        elif verb == "url":
            data = await asyncio.to_thread(_fetch_url, payload)
            await _render_and_store(
                data, _decode_image(data), fit=None, orientation=None, dither=None,
                show=True, wait=False, store=True, source="url", url=payload,
                coalesce=True,
            )
        elif verb == "dashboard":
            await _show_dashboard(payload or "guest_wifi", dither="NONE", wait=False,
                                  show=True, coalesce=True)
        elif verb == "clear":
            options = _command_json(payload)
            await display.clear(
                wait=False,
                cycles=int(options.get("cycles", 1)),
                delay=float(options.get("delay", 1.5)),
            )
            publish_state()
        elif verb == "prefs":
            await _apply_prefs(_command_json(payload))
        else:
            log.warning("unknown MQTT command %r (payload %r)", verb, payload)
    except HTTPException as exc:
        log.warning("MQTT command %s rejected: %s", verb, exc.detail)
    except (ValueError, TypeError) as exc:
        # A bad payload is a user error, not a bug — no traceback needed.
        log.warning("MQTT command %s rejected: %s", verb, exc)
    except Exception:  # noqa: BLE001 - a bad command must never kill the loop
        log.exception("MQTT command %s failed (payload %r)", verb, payload)


@app.post("/display/image")
async def display_image(
    request: Request,
    file: UploadFile | None = File(default=None),
    fit: str | None = Query(
        None,
        pattern="^(auto|cover|contain)$",
        description="cover fills and crops, contain pads with white, auto picks cover "
        "only while it would crop less than the frame's auto-fit threshold. "
        "Omit to use the device setting (GET /prefs).",
    ),
    orientation: str | None = Query(
        None,
        pattern="^(landscape|portrait)$",
        description="How to lay the photo out. Omit to use how the frame is mounted "
        "(GET /prefs).",
    ),
    rotate: float = Query(0.0, description="Turn the photo this many degrees clockwise "
                          "before placing it — any angle, not just quarter turns. Off "
                          "the quarter turns the corners it exposes become white. "
                          "Turns the picture; `orientation` turns the frame."),
    dither: str | None = Query(
        None,
        description="Dithering algorithm for this request (case-insensitive); "
        "defaults to the device setting. GET /status lists the available names.",
    ),
    wait: bool | None = Query(None, description="Block until the ~30s refresh finishes"),
    show: bool = Query(True, description="Set false to only render a preview, no refresh"),
    store: bool | None = Query(
        None,
        description="File the photo in the library. Defaults to `show`, so tuning "
        "dithering with show=false doesn't fill the library with near-duplicates.",
    ),
    name: str | None = Query(None, description="Optional label for the library entry"),
):
    _validate_dither(dither)
    data = await _read_image_bytes(request, file)
    img = _decode_image(data)
    return await _render_and_store(
        data,
        img,
        fit=fit,
        orientation=orientation,
        dither=dither,
        rotate=_validate_rotate(rotate),
        show=show,
        wait=_resolve_wait(wait),
        store=show if store is None else store,
        source="upload",
        name=name,
    )


@app.post("/display/url")
async def display_url(
    url: str = Query(..., description="http(s) URL of an image to fetch and show"),
    fit: str | None = Query(None, pattern="^(auto|cover|contain)$"),
    orientation: str | None = Query(None, pattern="^(landscape|portrait)$"),
    dither: str | None = Query(None),
    wait: bool | None = Query(None),
    show: bool = Query(True),
    store: bool | None = Query(None),
    name: str | None = Query(None),
):
    """Fetch an image and show it. Lets anything that can produce a URL — Home
    Assistant media sources, Grafana's render API, a NAS — drive the frame without
    proxying the bytes through the caller."""
    _validate_dither(dither)
    try:
        data = await asyncio.to_thread(_fetch_url, url)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    except Exception as exc:  # noqa: BLE001 - network/HTTP failure of any shape
        raise HTTPException(502, f"Could not fetch {url}: {exc!r}")
    img = _decode_image(data)
    return await _render_and_store(
        data,
        img,
        fit=fit,
        orientation=orientation,
        dither=dither,
        show=show,
        wait=_resolve_wait(wait),
        store=show if store is None else store,
        source="url",
        name=name,
        url=url,
    )

@app.post("/display/library/{photo_id}")
async def display_library_photo(
    photo_id: str,
    render: str | None = Query(
        None,
        description="Key of an existing render to put back on the panel verbatim — no "
        "re-rendering, so you get exactly the look you picked. Omit to render afresh.",
    ),
    fit: str | None = Query(None, pattern="^(auto|cover|contain)$"),
    orientation: str | None = Query(None, pattern="^(landscape|portrait)$"),
    rotate: float | None = Query(None, description="Turn the photo this many degrees "
                                 "clockwise — any angle, not just quarter turns"),
    crop: str | None = Query(None, description="x,y,w,h of the rotated photo to place"),
    dither: str | None = Query(
        None, description="Re-dither this photo with a different algorithm"),
    wait: bool | None = Query(None),
    show: bool = Query(True, description="Set false to render a preview only"),
):
    """Show a stored photo: either an existing render as-is, or a fresh one."""
    _validate_dither(dither)
    entry = _require_entry(photo_id)
    if render:
        return await _show_existing_render(entry, render, wait=_resolve_wait(wait))
    return await _render_stored(
        entry, fit=fit, orientation=orientation, dither=dither,
        rotate=None if rotate is None else _validate_rotate(rotate),
        crop=_parse_crop(crop),
        show=show, wait=_resolve_wait(wait),
    )


@app.post("/library/{photo_id}/render")
async def library_render(
    photo_id: str,
    fit: str | None = Query(None, pattern="^(auto|cover|contain)$"),
    orientation: str | None = Query(None, pattern="^(landscape|portrait)$"),
    rotate: float | None = Query(None, description="Degrees clockwise, any angle"),
    crop: str | None = Query(
        None, description="x,y,w,h of the ROTATED photo to place on the canvas, in its "
        "pixels. It may extend past the edges — that is how 'contain' and every "
        "position between the presets are expressed. Omit it and `fit` picks one."),
    dither: str | None = Query(None),
    show: bool = Query(False, description="Also put it on the panel"),
    wait: bool | None = Query(None),
):
    """Make (or reuse) a render of this photo. The render's key is a hash of the crop
    rectangle and the rest of the parameters, so asking twice for the same look returns
    the same render instead of filling the library with near-duplicates."""
    _validate_dither(dither)
    entry = _require_entry(photo_id)
    return await _render_stored(
        entry, fit=fit, orientation=orientation, dither=dither,
        rotate=None if rotate is None else _validate_rotate(rotate),
        crop=_parse_crop(crop),
        show=show, wait=_resolve_wait(wait),
    )


@app.delete("/library/{photo_id}/renders/{key}")
async def library_delete_render(photo_id: str, key: str):
    _require_entry(photo_id)
    if not await asyncio.to_thread(library.delete_render, photo_id, key):
        raise HTTPException(404, f"No render {key!r} for photo {photo_id!r}")
    publish_state()
    return {"deleted": key, "photo": _photo_public(_require_entry(photo_id))}


@app.post("/display/nav")
async def display_nav(
    direction: str = Query("next", pattern="^(next|prev|random)$",
                           description="next/prev move later/earlier in time and wrap "
                                       "around; random avoids repeating the current photo"),
    wait: bool | None = Query(None),
):
    """Step through the library relative to whatever is on the panel now. Reuses the
    photo's most recent render when it still fits the panel, so stepping is fast."""
    _require_library()
    entry = library.neighbour(direction)
    if entry is None:
        raise HTTPException(404, "The photo library is empty")
    if entry.renders:
        try:
            return await _show_existing_render(
                entry, entry.renders[-1].key, wait=_resolve_wait(wait))
        except HTTPException:
            pass  # the file went missing — fall through and render it again
    return await _render_stored(entry, show=True, wait=_resolve_wait(wait))


@app.post("/display/clear")
async def display_clear(
    wait: bool | None = Query(None),
    cycles: int = Query(1, ge=1, le=6, description="1 = single white flush; >1 runs a deep de-ghost (black/white flashes + colour pass, blocks a few min)"),
    delay: float = Query(1.5, ge=0, le=10, description="Seconds of rest between flushes when cycles>1"),
):
    return await display.clear(wait=_resolve_wait(wait), cycles=cycles, delay=delay)


async def _show_dashboard(
    name: str, *, dither: str, wait: bool, show: bool, coalesce: bool = False
) -> dict:
    """Render a dashboard and put it on the panel. Dashboards are composed at the
    panel's own resolution and dither their own fills, so they are always placed with
    fit=contain and (by default) dither=NONE — see app/dashboards.py."""
    try:
        img = dashboards.render(name)
    except NotImplementedError as exc:
        raise HTTPException(501, str(exc))
    if show:
        await display.show_image(img, fit="contain", wait=wait, mode=dither,
                                 coalesce=coalesce)
    else:
        await display.render_preview(img, fit="contain", mode=dither)
    publish_state()
    body = {
        "dashboard": name,
        "shown": show,
        "dither": dither.upper(),
        "preview": "/display/preview",
        "available": dashboards.available(),
    }
    if show:
        body["waited"] = wait
    return body


@app.post("/display/dashboard")
async def display_dashboard(
    name: str = Query("guest_wifi"),
    dither: str = Query("NONE", description="Dithering algorithm; dashboards are pre-composed "
                        "(they dither their own fills), so the default is NONE to keep text/QR crisp"),
    wait: bool | None = Query(None),
    show: bool = Query(True, description="Set false to only render a preview, no refresh"),
):
    _validate_dither(dither)
    return await _show_dashboard(name, dither=dither, wait=_resolve_wait(wait), show=show)


def _preview_as_viewed() -> bytes:
    """The preview rotated the way you'd see it on the wall.

    `latest.png` is the PANEL BUFFER — always the panel's native landscape W×H, with a
    portrait layout transposed into it by render_for_inky(). Anything showing the frame
    to a human (the gallery header, Home Assistant's `image` entity) wants it turned
    back, or a vertically mounted frame reads sideways. ROTATE_270 undoes the
    ROTATE_90 applied during rendering."""
    with Image.open(display.preview_path) as img:
        img.load()
        out = io.BytesIO()
        # No optimize=: this runs per request and the gain on a photo is not worth
        # the extra second of CPU on a Pi.
        img.transpose(Image.ROTATE_270).save(out, "PNG")
    return out.getvalue()


@app.get("/display/preview")
async def display_preview(
    view: bool = Query(
        False,
        description="Rotate to viewing orientation when the frame is mounted portrait. "
        "Default false returns the raw panel buffer, which is what you want for "
        "inspecting a render or re-posting it verbatim.",
    ),
):
    if not os.path.exists(display.preview_path):
        raise HTTPException(404, "No preview rendered yet")
    if not view or prefs.orientation != "portrait":
        return FileResponse(display.preview_path, media_type="image/png")
    return Response(await asyncio.to_thread(_preview_as_viewed), media_type="image/png")


# ---------------------------------------------------------------------------
# Photo library (app/library.py)
# ---------------------------------------------------------------------------
@app.get("/library")
async def library_index(
    limit: int = Query(60, ge=1, le=500),
    offset: int = Query(0, ge=0),
    order: str = Query("newest", pattern="^(newest|oldest)$"),
    collection: str | None = Query(None, description="Only photos in this collection"),
):
    """A page of photos. Each entry carries its file URLs and every render made from
    it, so a gallery needs this one call."""
    _require_library()
    photos, total = library.list(
        limit=limit, offset=offset, order=order, collection_id=collection)
    return {
        "total": total,
        "limit": limit,
        "offset": offset,
        "order": order,
        "collection": collection,
        "current": {
            "photo_id": library.current_id,
            "render_key": library.current_render,
            "shown_at": library.current_shown_at,
        },
        "photos": [_photo_public(p) for p in photos],
    }


@app.get("/library/{photo_id}")
async def library_photo(photo_id: str):
    return _photo_public(_require_entry(photo_id))


@app.patch("/library/{photo_id}")
async def library_update(photo_id: str, changes: dict = Body(...)):
    """Rename a photo and/or replace which collections it is in. Membership is sent as
    the full set, because that is what a row of checkboxes produces."""
    _require_entry(photo_id)
    if "name" in changes:
        await asyncio.to_thread(library.rename, photo_id, changes.get("name"))
    if "collections" in changes:
        value = changes["collections"]
        if not isinstance(value, list):
            raise HTTPException(400, "collections must be a list of collection ids")
        try:
            await asyncio.to_thread(library.set_collections, photo_id, value)
        except ValueError as exc:
            raise HTTPException(400, str(exc))
    return _photo_public(_require_entry(photo_id))


@app.get("/library/{photo_id}/original")
async def library_photo_original(photo_id: str):
    return _library_file(photo_id, "original")


@app.get("/library/{photo_id}/thumb")
async def library_photo_thumb(photo_id: str):
    """Thumbnail of the ORIGINAL — a photo may have no render at all, and the grid
    still has to show the picture."""
    return _library_file(photo_id, "thumb")


@app.get("/library/{photo_id}/renders/{key}")
async def library_render_file(photo_id: str, key: str):
    """The 6-colour image exactly as it went to the panel."""
    return _library_file(photo_id, "render", key)


@app.get("/library/{photo_id}/renders/{key}/thumb")
async def library_render_thumb(photo_id: str, key: str):
    return _library_file(photo_id, "thumb", key)


def _library_file(photo_id: str, kind: str, key: str | None = None) -> FileResponse:
    _require_entry(photo_id)
    found = library.file_path(photo_id, kind, key)
    if found is None:
        what = f"{kind} {key!r}" if key else kind
        raise HTTPException(404, f"No {what} stored for {photo_id!r}")
    path, media_type = found
    return FileResponse(path, media_type=media_type)


@app.post("/library")
async def library_upload(
    request: Request,
    file: UploadFile | None = File(default=None),
    name: str | None = Query(None, description="Optional label for the entry"),
    collection: str | None = Query(None, description="File it in this collection too"),
):
    """Store a photo WITHOUT rendering or showing it.

    Uploading is a bulk action and rendering is not free (~1 s each, plus a ~30 s panel
    refresh if shown), so an upload only archives the original and makes a thumbnail.
    Renders appear when someone opens the photo and chooses a look."""
    _require_library()
    data = await _read_image_bytes(request, file)
    image = _decode_image(data)
    entry = await asyncio.to_thread(
        library.add_photo, data, source="upload", name=name, image=image,
        collection_id=collection,
    )
    if entry is None:
        raise HTTPException(503, "Photo library is disabled (LIBRARY_ENABLED=0)")
    publish_state()
    return _photo_public(entry)


@app.delete("/library/{photo_id}")
async def library_delete(photo_id: str):
    """Forget a photo: index entry, original, thumbnail and every render of it."""
    _require_entry(photo_id)
    await asyncio.to_thread(library.delete, photo_id)
    publish_state()
    return {"deleted": photo_id, "library": library.stats()}


# ---------------------------------------------------------------------------
# Collections — folders that a photo can be in several of at once
# ---------------------------------------------------------------------------
@app.get("/collections")
async def collections_index():
    _require_library()
    return {"collections": [c.public() for c in library.collections()]}


@app.post("/collections")
async def collections_create(body: dict = Body(..., examples=[{"name": "Guests"}])):
    _require_library()
    try:
        return (await asyncio.to_thread(library.create_collection, body.get("name", ""))).public()
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@app.patch("/collections/{collection_id}")
async def collections_rename(collection_id: str, body: dict = Body(...)):
    _require_library()
    try:
        collection = await asyncio.to_thread(
            library.rename_collection, collection_id, body.get("name", ""))
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    if collection is None:
        raise HTTPException(404, f"No collection {collection_id!r}")
    return collection.public()


@app.delete("/collections/{collection_id}")
async def collections_delete(collection_id: str):
    """Removes the collection only. The photos stay in the library — a photo can be in
    several collections, so it does not belong to any one of them."""
    _require_library()
    if not await asyncio.to_thread(library.delete_collection, collection_id):
        raise HTTPException(404, f"No collection {collection_id!r}")
    return {"deleted": collection_id}


@app.get("/prefs")
async def get_prefs():
    return {
        "prefs": prefs.as_dict(),
        "options": {
            "orientation": list(ORIENTATIONS),
            "fit": list(FIT_MODES),
            "dither": list(AVAILABLE_DITHER_MODES),
            "auto_fit_max_crop": {"min": 0.0, "max": 1.0},
        },
        "path": str(prefs.path),
    }


@app.patch("/prefs")
async def patch_prefs(changes: dict = Body(..., examples=[{"orientation": "portrait"}])):
    """Partial update; unknown keys and bad values are rejected without applying any
    of the change. Changing `orientation` also re-shows the current photo."""
    try:
        return {"prefs": await _apply_prefs(changes)}
    except ValueError as exc:
        raise HTTPException(400, str(exc))


def main() -> None:
    import uvicorn

    uvicorn.run("app.main:app", host=settings.host, port=settings.port)


if __name__ == "__main__":
    main()
