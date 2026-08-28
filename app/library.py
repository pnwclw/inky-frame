"""On-disk photo library: photos, the renders made from them, and collections.

Layout under ``OUTPUT_DIR`` (the ``./data`` volume on the Pi):

    photos/<pid>.<ext>              the photo as received (downscaled only if huge —
                                    see ``library_original_max_scale``). Source of
                                    truth: every render is produced from here, so
                                    changing DITHER_* or swapping panels still gives a
                                    correct result for an old photo.
    thumbs/<pid>.jpg                thumbnail of the ORIGINAL, for the grid.
    renders/<pid>-<key>.png         one dithered panel buffer, mode "P" (a 6-colour
                                    palette PNG: pixel-identical to RGB at ~half the
                                    bytes). This is what the panel actually shows.
    render-thumbs/<pid>-<key>.jpg   thumbnail of that render, rotated to VIEWING
                                    orientation so a portrait mount isn't sideways.
    library.json                    the index: photos, collections, and what is on
                                    the panel right now.

Three things shape this design:

* **A photo has MANY renders.** fit, rotation, dithering algorithm and the panel's own
  resolution all change the result, and the point of the picker is choosing between
  them. A render's key is a hash of exactly those parameters, so asking for the same
  look twice reuses the file instead of making a near-duplicate.
* **Uploading does not render.** Photos arrive in bulk and rendering is expensive
  (~1 s each, and a 30 s panel refresh if shown), so an upload only stores and
  thumbnails. Renders appear when someone actually looks at a photo.
* **What is on the panel is stored.** ``current`` names a photo *and* a render, so
  after any restart the service knows what the e-paper is still showing.

Ordering is chronological, **oldest first**, internally: appending is O(1), pruning
the oldest is a slice, and next/prev mean "later/earlier in time". The HTTP layer
reverses it for listing, where newest-first is what a gallery wants.

Dedup is by SHA-256 of the received bytes: re-sending a photo the library already
holds does not make a second copy — the existing entry is refreshed and moved to the
end, so a re-send behaves like a fresh arrival and sorts to the top.

Everything here is synchronous and guarded by a threading lock; callers on the event
loop reach it through ``asyncio.to_thread``.
"""

from __future__ import annotations

import dataclasses
import hashlib
import io
import json
import logging
import os
import random
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from PIL import Image

from .config import Settings
from .dithering import default_crop, rotated_size, working_canvas

log = logging.getLogger(__name__)

INDEX_VERSION = 3

# Formats we keep byte-for-byte when the photo is already small enough. Anything else
# (TIFF, BMP, HEIC via a plugin, …) is re-encoded to JPEG on the way in.
VERBATIM_FORMATS = {"JPEG": ".jpg", "PNG": ".png", "WEBP": ".webp"}
MEDIA_TYPES = {".jpg": "image/jpeg", ".png": "image/png", ".webp": "image/webp"}

PHOTO_KINDS = ("original", "thumb")
RENDER_KINDS = ("render", "thumb")
ROTATIONS = (0, 90, 180, 270)


@dataclass
class RenderEntry:
    """One rendered version of a photo. `key` is derived from the parameters, so the
    same look never gets stored twice."""

    key: str
    created_at: float
    fit: str  # the preset the crop came from: cover | contain | custom
    rotate: int
    orientation: str
    dither: str
    panel: str
    resolution: list[int]
    # Rectangle of the ROTATED photo that lands on the canvas, in its pixels; it may
    # overhang the edges. Defaulted so an index written before crops existed still
    # loads — the v3 migration fills it in.
    crop: list[float] = field(default_factory=list)
    bytes_on_disk: int = 0
    shown_count: int = 0
    last_shown_at: float | None = None

    def public(self, photo_id: str) -> dict:
        d = asdict(self)
        d["photo_id"] = photo_id
        d["render"] = f"/library/{photo_id}/renders/{self.key}"
        d["thumb"] = f"/library/{photo_id}/renders/{self.key}/thumb"
        return d


@dataclass
class PhotoEntry:
    id: str
    added_at: float
    sha256: str
    source: str  # "upload" (HTTP body/multipart) | "url"
    ext: str
    width: int  # of the stored original, after any downscale
    height: int
    bytes_on_disk: int = 0
    name: str | None = None
    url: str | None = None  # where it was fetched from, for source="url"
    renders: list[RenderEntry] = field(default_factory=list)

    def public(self, collections: list[str] | None = None) -> dict:
        d = asdict(self)
        d["renders"] = [r.public(self.id) for r in self.renders]
        d["thumb"] = f"/library/{self.id}/thumb"
        d["original"] = f"/library/{self.id}/original"
        d["collections"] = collections or []
        return d


@dataclass
class Collection:
    id: str
    name: str
    created_at: float
    photo_ids: list[str] = field(default_factory=list)

    def public(self) -> dict:
        d = asdict(self)
        d["count"] = len(self.photo_ids)
        return d


_PHOTO_FIELDS = {f.name for f in dataclasses.fields(PhotoEntry)}
_RENDER_FIELDS = {f.name for f in dataclasses.fields(RenderEntry)}
_COLLECTION_FIELDS = {f.name for f in dataclasses.fields(Collection)}


def _new_id(sha256: str, when: float) -> str:
    """Sortable, human-readable, unique: local timestamp + a slice of the hash.

    A collision needs two *different* photos with the same 32-bit hash prefix in the
    same second; the same photo twice is caught by the sha256 dedup first.
    """
    return time.strftime("%Y%m%d-%H%M%S", time.localtime(when)) + "-" + sha256[:8]


def render_key(
    *,
    crop: list[float],
    rotate: int,
    orientation: str,
    dither: str,
    resolution: list[int] | tuple,
) -> str:
    """Identity of a rendered look — the crop rectangle, not the preset that produced
    it, because two presets can land on the same rectangle. Rounded to whole pixels so
    a drag that ends a hundredth of a pixel away doesn't make a second render.
    Resolution is in there on purpose: after a panel swap the old render is the wrong
    size and must not be reused."""
    box = "x".join(str(round(v)) for v in (crop or [0, 0, 0, 0]))
    raw = f"{box}|{int(rotate) % 360}|{orientation}|{dither.upper()}|{tuple(resolution)}"
    return hashlib.sha256(raw.encode()).hexdigest()[:10]


class PhotoLibrary:
    def __init__(self, settings: Settings, size: tuple[int, int] | None = None):
        self.settings = settings
        self.root = Path(settings.output_dir)
        # The panel resolution originals are sized against. Set from the live driver in
        # the lifespan, like DashboardRenderer.size.
        self.size: tuple[int, int] = tuple(size or settings.panel_spec.resolution)
        self._lock = threading.RLock()
        self._entries: list[PhotoEntry] = []  # chronological, oldest first
        self._by_id: dict[str, PhotoEntry] = {}
        self._by_sha: dict[str, PhotoEntry] = {}
        self._collections: list[Collection] = []
        self.current_id: str | None = None
        self.current_render: str | None = None
        self.current_shown_at: float | None = None
        if self.enabled:
            for d in (self.photos_dir, self.thumbs_dir, self.renders_dir,
                      self.render_thumbs_dir):
                d.mkdir(parents=True, exist_ok=True)
            self._load()

    # -- paths ---------------------------------------------------------------
    @property
    def enabled(self) -> bool:
        return self.settings.library_enabled

    @property
    def photos_dir(self) -> Path:
        return self.root / "photos"

    @property
    def thumbs_dir(self) -> Path:
        return self.root / "thumbs"

    @property
    def renders_dir(self) -> Path:
        return self.root / "renders"

    @property
    def render_thumbs_dir(self) -> Path:
        return self.root / "render-thumbs"

    @property
    def index_path(self) -> Path:
        return self.root / "library.json"

    def _original_path(self, entry: PhotoEntry) -> Path:
        return self.photos_dir / f"{entry.id}{entry.ext}"

    def _thumb_path(self, photo_id: str) -> Path:
        return self.thumbs_dir / f"{photo_id}.jpg"

    def _render_path(self, photo_id: str, key: str) -> Path:
        return self.renders_dir / f"{photo_id}-{key}.png"

    def _render_thumb_path(self, photo_id: str, key: str) -> Path:
        return self.render_thumbs_dir / f"{photo_id}-{key}.jpg"

    def file_path(
        self, photo_id: str, kind: str, key: str | None = None
    ) -> tuple[Path, str] | None:
        """(path, media type) for one stored file, or None if it isn't there.

        `kind` is original|thumb for the photo, or render|thumb with a render `key`."""
        with self._lock:
            entry = self._by_id.get(photo_id)
        if entry is None:
            return None
        if key is not None:
            if kind == "render":
                path, ext = self._render_path(photo_id, key), ".png"
            elif kind == "thumb":
                path, ext = self._render_thumb_path(photo_id, key), ".jpg"
            else:
                raise ValueError(f"unknown render kind {kind!r}; expected {RENDER_KINDS}")
        elif kind == "original":
            path, ext = self._original_path(entry), entry.ext
        elif kind == "thumb":
            path, ext = self._thumb_path(photo_id), ".jpg"
        else:
            raise ValueError(f"unknown kind {kind!r}; expected one of {PHOTO_KINDS}")
        if not path.exists():
            return None
        return path, MEDIA_TYPES.get(ext, "application/octet-stream")

    def read_original(self, photo_id: str) -> bytes | None:
        found = self.file_path(photo_id, "original")
        return None if found is None else found[0].read_bytes()

    # -- index persistence ---------------------------------------------------
    def _load(self) -> None:
        if not self.index_path.exists():
            return
        try:
            raw = json.loads(self.index_path.read_text("utf-8"))
        except (OSError, ValueError):
            log.exception("library index unreadable, starting empty: %s", self.index_path)
            return

        version = raw.get("version", 1)
        photos = [self._photo_from_json(item) for item in raw.get("photos", [])]
        self._entries = [p for p in photos if p is not None]
        self._collections = [
            Collection(**{k: v for k, v in c.items() if k in _COLLECTION_FIELDS})
            for c in raw.get("collections", [])
        ]
        current = raw.get("current") or {}
        self.current_id = current.get("photo_id") or raw.get("current_id")
        self.current_render = current.get("render_key")
        self.current_shown_at = current.get("shown_at")
        self._reindex()
        if version < INDEX_VERSION:
            # The raw dicts, because v1 kept the render's parameters on the PHOTO and
            # _photo_from_json drops anything that is not a v2 field.
            with self._lock:
                self._migrate_locked(version, {p["id"]: p for p in raw.get("photos", [])
                                               if isinstance(p, dict) and "id" in p})
        log.info("photo library: %d photos, %d collections from %s",
                 len(self._entries), len(self._collections), self.index_path)

    def _photo_from_json(self, item: dict) -> PhotoEntry | None:
        try:  # tolerate fields added by a newer version
            entry = PhotoEntry(**{k: v for k, v in item.items()
                                  if k in _PHOTO_FIELDS and k != "renders"})
        except TypeError:
            log.warning("skipping malformed library entry: %r", item)
            return None
        for r in item.get("renders") or []:
            try:
                entry.renders.append(
                    RenderEntry(**{k: v for k, v in r.items() if k in _RENDER_FIELDS}))
            except TypeError:
                log.warning("skipping malformed render on %s: %r", entry.id, r)
        return entry

    def _save(self) -> None:
        payload = {
            "version": INDEX_VERSION,
            "current": {
                "photo_id": self.current_id,
                "render_key": self.current_render,
                "shown_at": self.current_shown_at,
            },
            "collections": [asdict(c) for c in self._collections],
            "photos": [asdict(p) for p in self._entries],
        }
        tmp = self.index_path.with_name(self.index_path.name + ".tmp")
        tmp.write_text(json.dumps(payload, indent=1), "utf-8")
        os.replace(tmp, self.index_path)  # atomic within the same directory

    def _reindex(self) -> None:
        self._by_id = {e.id: e for e in self._entries}
        # Last one wins, matching list order; duplicates shouldn't exist anyway.
        self._by_sha = {e.sha256: e for e in self._entries}

    def _migrate_v3_locked(self) -> None:
        """v2 renders predate the crop rectangle: they only recorded the `fit` preset.
        Fill the rectangle in and re-key them, because the key IS the parameters — a
        render whose key no longer describes it would be handed out for the wrong look.
        Recomputing the key means renaming its two files."""
        fixed = 0
        for entry in self._entries:
            for render in entry.renders:
                if render.crop:
                    continue
                work = working_canvas(tuple(render.resolution), render.orientation)
                source = rotated_size((entry.width, entry.height), render.rotate)
                render.crop = default_crop(source, work, render.fit)
                old_key, render.key = render.key, render_key(
                    crop=render.crop, rotate=render.rotate,
                    orientation=render.orientation, dither=render.dither,
                    resolution=render.resolution,
                )
                if old_key != render.key:
                    for src, dst in (
                        (self._render_path(entry.id, old_key),
                         self._render_path(entry.id, render.key)),
                        (self._render_thumb_path(entry.id, old_key),
                         self._render_thumb_path(entry.id, render.key)),
                    ):
                        if src.exists():
                            src.replace(dst)
                    if self.current_id == entry.id and self.current_render == old_key:
                        self.current_render = render.key
                fixed += 1
            entry.bytes_on_disk = self._disk_usage(entry)
        if fixed:
            log.info("library migrated v2 -> v3 (%d renders given a crop rectangle)", fixed)

    def _migrate_locked(self, from_version: int, legacy: dict[str, dict]) -> None:
        """v1 gave every photo exactly one render at `renders/<id>.png`, and
        `thumbs/<id>.jpg` was a thumbnail of THAT RENDER. v2 makes renders plural and
        reclaims `thumbs/` for the original, so the old files move and a fresh
        original-thumbnail is generated."""
        if from_version >= 2:
            self._migrate_v3_locked()
            self._save()
            return
        moved = 0
        for entry in self._entries:
            legacy_render = self.renders_dir / f"{entry.id}.png"
            legacy_thumb = self._thumb_path(entry.id)
            params = legacy.get(entry.id, {})
            fit = params.get("fit", self.settings.default_fit)
            orientation = params.get("orientation", self.settings.default_orientation)
            resolution = params.get("resolution", list(self.size))
            # v1 stored no crop. The rectangle is recoverable from the preset and the
            # photo's own size, which is exactly what default_crop() computes.
            work = working_canvas(tuple(resolution), orientation)
            crop = default_crop(rotated_size((entry.width, entry.height), 0), work, fit)
            key = render_key(
                crop=crop, rotate=0, orientation=orientation,
                dither=params.get("dither", self.settings.dither_mode),
                resolution=resolution,
            )
            if legacy_render.exists():
                if legacy_thumb.exists():
                    legacy_thumb.replace(self._render_thumb_path(entry.id, key))
                legacy_render.replace(self._render_path(entry.id, key))
                entry.renders = [RenderEntry(
                    key=key,
                    created_at=entry.added_at,
                    fit=fit,
                    crop=crop,
                    rotate=0,
                    orientation=orientation,
                    dither=params.get("dither", self.settings.dither_mode),
                    panel=params.get("panel", self.settings.panel),
                    resolution=resolution,
                )]
                if entry.id == self.current_id:
                    self.current_render = key
                moved += 1
            try:  # thumbs/ now means "the original", so rebuild it
                self._write_photo_thumb(entry)
            except (OSError, ValueError):
                log.exception("could not rebuild the thumbnail for %s", entry.id)
            entry.bytes_on_disk = self._disk_usage(entry)
        self._migrate_v3_locked()
        self._save()
        log.info("library migrated v%d -> v%d (%d renders carried over)",
                 from_version, INDEX_VERSION, moved)

    # -- writing files -------------------------------------------------------
    def _original_cap(self) -> int:
        """Longest edge we keep for an archived original."""
        return max(1, round(max(self.size) * self.settings.library_original_max_scale))

    def _store_original(
        self, data: bytes, photo_id: str, image: Image.Image | None
    ) -> tuple[str, int, int]:
        """Write the archive copy; return (ext, width, height)."""
        if image is None:
            image = Image.open(io.BytesIO(data))
            image.load()
        cap = self._original_cap()
        ext = VERBATIM_FORMATS.get((image.format or "").upper())
        if ext is not None and max(image.size) <= cap:
            # Already a sane size in a format we can serve — keep the exact bytes.
            (self.photos_dir / f"{photo_id}{ext}").write_bytes(data)
            return ext, image.size[0], image.size[1]
        out = image.convert("RGB")  # a copy; never mutates the caller's image
        if max(out.size) > cap:
            out.thumbnail((cap, cap), Image.LANCZOS)
        out.save(
            self.photos_dir / f"{photo_id}.jpg",
            "JPEG",
            quality=self.settings.library_jpeg_quality,
            optimize=True,
        )
        return ".jpg", out.size[0], out.size[1]

    def _write_photo_thumb(self, entry: PhotoEntry, image: Image.Image | None = None) -> None:
        """Grid thumbnail, made from the ORIGINAL — a photo may have no render at all,
        and the grid should still show the picture."""
        if image is None:
            path = self._original_path(entry)
            if not path.exists():
                return
            with Image.open(path) as opened:
                opened.load()
                image = opened.convert("RGB")
        else:
            image = image.convert("RGB")
        side = self.settings.library_thumb_size
        image.thumbnail((side, side), Image.LANCZOS)
        image.save(self._thumb_path(entry.id), "JPEG", quality=85, optimize=True)

    def _write_render_files(
        self, photo_id: str, key: str, rendered: Image.Image, orientation: str
    ) -> None:
        # `rendered` can be the very image a background (wait=false) refresh is feeding
        # to inky.set_image() on another thread. Both sides only READ an already-loaded
        # image, which is safe; keep it that way — do not mutate it here.
        # Saved as-is in mode "P": a 6-colour palette PNG is pixel-identical to the RGB
        # version at ~half the bytes.
        rendered.save(self._render_path(photo_id, key), optimize=True)

        thumb = rendered.convert("RGB")
        if orientation == "portrait":
            # The render is the PANEL BUFFER: render_for_inky() lays a portrait photo
            # out on a rotated canvas and transposes it back to the panel's landscape
            # W×H. Undo that here so a gallery shows it the way it hangs on the wall.
            thumb = thumb.transpose(Image.ROTATE_270)
        side = self.settings.library_thumb_size
        thumb.thumbnail((side, side), Image.LANCZOS)
        thumb.save(self._render_thumb_path(photo_id, key), "JPEG", quality=85, optimize=True)

    def _size_of(self, path: Path) -> int:
        try:
            return path.stat().st_size
        except OSError:
            return 0

    def _disk_usage(self, entry: PhotoEntry) -> int:
        total = self._size_of(self._original_path(entry)) + self._size_of(
            self._thumb_path(entry.id))
        for render in entry.renders:
            render.bytes_on_disk = self._size_of(
                self._render_path(entry.id, render.key)) + self._size_of(
                self._render_thumb_path(entry.id, render.key))
            total += render.bytes_on_disk
        return total

    # -- photos --------------------------------------------------------------
    def add_photo(
        self,
        data: bytes,
        *,
        source: str = "upload",
        name: str | None = None,
        url: str | None = None,
        image: Image.Image | None = None,
        collection_id: str | None = None,
    ) -> PhotoEntry | None:
        """Store a photo. Does NOT render it — that happens when someone looks at it.
        `image` is the already-decoded photo if the caller has one."""
        if not self.enabled:
            return None
        sha = hashlib.sha256(data).hexdigest()
        now = time.time()
        with self._lock:
            entry = self._by_sha.get(sha)
            if entry is not None:
                # Same bytes we already hold: keep the single original and its renders,
                # and move it to the end so a re-send reads as a fresh arrival.
                self._entries.remove(entry)
                self._entries.append(entry)
                entry.added_at = now
                if name:
                    entry.name = name
                log.info("library: %s re-sent (dedup by sha256)", entry.id)
            else:
                photo_id = _new_id(sha, now)
                ext, width, height = self._store_original(data, photo_id, image)
                entry = PhotoEntry(
                    id=photo_id, added_at=now, sha256=sha, source=source, ext=ext,
                    width=width, height=height, name=name, url=url,
                )
                self._write_photo_thumb(entry, image)
                self._entries.append(entry)
                self._by_id[entry.id] = entry
                self._by_sha[sha] = entry
                log.info("library: stored %s (%dx%d %s)", entry.id, width, height, ext)
            if collection_id:
                self._add_to_collection_locked(collection_id, entry.id)
            entry.bytes_on_disk = self._disk_usage(entry)
            self._prune_locked()
            self._save()
            return entry

    def record_render(
        self,
        photo_id: str,
        rendered: Image.Image,
        *,
        fit: str,
        crop: list[float],
        rotate: int,
        orientation: str,
        dither: str,
        shown: bool = False,
    ) -> RenderEntry | None:
        """File a rendered version. Rendering the same look again overwrites the same
        files rather than piling up near-duplicates, because the key is the params."""
        with self._lock:
            entry = self._by_id.get(photo_id)
            if entry is None:
                return None
            resolution = list(rendered.size)
            key = render_key(crop=crop, rotate=rotate, orientation=orientation,
                             dither=dither, resolution=resolution)
            render = next((r for r in entry.renders if r.key == key), None)
            if render is None:
                render = RenderEntry(
                    key=key, created_at=time.time(), fit=fit, crop=list(crop),
                    rotate=rotate, orientation=orientation, dither=dither,
                    panel=self.settings.panel, resolution=resolution,
                )
                entry.renders.append(render)
            self._write_render_files(photo_id, key, rendered, orientation)
            if shown:
                render.shown_count += 1
                render.last_shown_at = time.time()
                self.current_id = photo_id
                self.current_render = key
                self.current_shown_at = render.last_shown_at
            entry.bytes_on_disk = self._disk_usage(entry)
            self._save()
            return render

    def get(self, photo_id: str) -> PhotoEntry | None:
        with self._lock:
            return self._by_id.get(photo_id)

    def get_render(self, photo_id: str, key: str) -> RenderEntry | None:
        entry = self.get(photo_id)
        if entry is None:
            return None
        return next((r for r in entry.renders if r.key == key), None)

    def list(
        self,
        *,
        limit: int = 60,
        offset: int = 0,
        order: str = "newest",
        collection_id: str | None = None,
    ) -> tuple[list[PhotoEntry], int]:
        """A page of photos plus the total. Default order is newest-first; inside a
        collection the collection's own order is used."""
        with self._lock:
            if collection_id:
                collection = self._collection(collection_id)
                if collection is None:
                    return [], 0
                items = [self._by_id[p] for p in collection.photo_ids if p in self._by_id]
            else:
                items = list(self._entries)
                if order != "oldest":
                    items.reverse()
            return items[offset : offset + limit], len(items)

    def neighbour(self, direction: str) -> PhotoEntry | None:
        """The photo to show for next/prev/random, relative to the current one.
        next/prev move later/earlier in time and wrap around; random avoids repeating
        what is already on the panel."""
        with self._lock:
            if not self._entries:
                return None
            if direction == "random":
                pool = [e for e in self._entries if e.id != self.current_id] or self._entries
                return random.choice(pool)
            current = self._by_id.get(self.current_id or "")
            if current is None:  # nothing shown yet (or it was pruned) -> newest
                return self._entries[-1]
            index = self._entries.index(current)
            step = 1 if direction == "next" else -1
            return self._entries[(index + step) % len(self._entries)]

    def rename(self, photo_id: str, name: str | None) -> PhotoEntry | None:
        with self._lock:
            entry = self._by_id.get(photo_id)
            if entry is None:
                return None
            entry.name = (name or "").strip() or None
            self._save()
            return entry

    def delete(self, photo_id: str) -> bool:
        with self._lock:
            entry = self._by_id.get(photo_id)
            if entry is None:
                return False
            self._delete_locked(entry)
            self._save()
            return True

    def delete_render(self, photo_id: str, key: str) -> bool:
        with self._lock:
            entry = self._by_id.get(photo_id)
            if entry is None:
                return False
            render = next((r for r in entry.renders if r.key == key), None)
            if render is None:
                return False
            self._render_path(photo_id, key).unlink(missing_ok=True)
            self._render_thumb_path(photo_id, key).unlink(missing_ok=True)
            entry.renders.remove(render)
            if self.current_id == photo_id and self.current_render == key:
                self.current_render = None
            entry.bytes_on_disk = self._disk_usage(entry)
            self._save()
            return True

    # -- collections ---------------------------------------------------------
    def _collection(self, collection_id: str) -> Collection | None:
        return next((c for c in self._collections if c.id == collection_id), None)

    def collections(self) -> list[Collection]:
        with self._lock:
            return list(self._collections)

    def collections_of(self, photo_id: str) -> list[str]:
        with self._lock:
            return [c.id for c in self._collections if photo_id in c.photo_ids]

    def create_collection(self, name: str) -> Collection:
        name = (name or "").strip()
        if not name:
            raise ValueError("A collection needs a name")
        with self._lock:
            if any(c.name.lower() == name.lower() for c in self._collections):
                raise ValueError(f"There is already a collection called {name!r}")
            now = time.time()
            collection = Collection(
                id="c-" + hashlib.sha256(f"{name}{now}".encode()).hexdigest()[:8],
                name=name, created_at=now,
            )
            self._collections.append(collection)
            self._save()
            return collection

    def rename_collection(self, collection_id: str, name: str) -> Collection | None:
        name = (name or "").strip()
        if not name:
            raise ValueError("A collection needs a name")
        with self._lock:
            collection = self._collection(collection_id)
            if collection is None:
                return None
            collection.name = name
            self._save()
            return collection

    def delete_collection(self, collection_id: str) -> bool:
        """Removes the collection only — the photos in it are untouched, since a photo
        can be in several collections and belongs to the library, not to a folder."""
        with self._lock:
            collection = self._collection(collection_id)
            if collection is None:
                return False
            self._collections.remove(collection)
            self._save()
            return True

    def _add_to_collection_locked(self, collection_id: str, photo_id: str) -> bool:
        collection = self._collection(collection_id)
        if collection is None or photo_id not in self._by_id:
            return False
        if photo_id not in collection.photo_ids:
            collection.photo_ids.append(photo_id)
        return True

    def set_collections(self, photo_id: str, collection_ids: list[str]) -> list[str] | None:
        """Replace a photo's membership in one go — that is what a set of checkboxes in
        the UI produces."""
        wanted = set(collection_ids)
        with self._lock:
            if photo_id not in self._by_id:
                return None
            unknown = wanted - {c.id for c in self._collections}
            if unknown:
                raise ValueError(f"Unknown collections: {', '.join(sorted(unknown))}")
            for collection in self._collections:
                if collection.id in wanted and photo_id not in collection.photo_ids:
                    collection.photo_ids.append(photo_id)
                elif collection.id not in wanted and photo_id in collection.photo_ids:
                    collection.photo_ids.remove(photo_id)
            self._save()
            return [c.id for c in self._collections if photo_id in c.photo_ids]

    # -- state ---------------------------------------------------------------
    def set_current(self, photo_id: str, key: str | None) -> None:
        with self._lock:
            self.current_id = photo_id
            self.current_render = key
            self.current_shown_at = time.time()
            self._save()

    def stats(self) -> dict:
        with self._lock:
            current = self._by_id.get(self.current_id or "")
            return {
                "enabled": self.enabled,
                "count": len(self._entries),
                "max_items": self.settings.library_max_items,
                "bytes_on_disk": sum(e.bytes_on_disk for e in self._entries),
                "renders": sum(len(e.renders) for e in self._entries),
                "collections": len(self._collections),
                "current_id": self.current_id,
                "current_render": self.current_render,
                "current_shown_at": self.current_shown_at,
                "current_name": (current.name if current else None),
            }

    # -- internals -----------------------------------------------------------
    def _delete_locked(self, entry: PhotoEntry) -> None:
        self._original_path(entry).unlink(missing_ok=True)
        self._thumb_path(entry.id).unlink(missing_ok=True)
        for render in entry.renders:
            self._render_path(entry.id, render.key).unlink(missing_ok=True)
            self._render_thumb_path(entry.id, render.key).unlink(missing_ok=True)
        self._entries.remove(entry)
        self._by_id.pop(entry.id, None)
        if self._by_sha.get(entry.sha256) is entry:
            self._by_sha.pop(entry.sha256, None)
        for collection in self._collections:
            if entry.id in collection.photo_ids:
                collection.photo_ids.remove(entry.id)
        if self.current_id == entry.id:
            self.current_id = self.current_render = self.current_shown_at = None
        log.info("library: removed %s", entry.id)

    def _prune_locked(self) -> int:
        """Drop the oldest photos past the cap. Caller holds the lock and saves."""
        removed = 0
        while len(self._entries) > self.settings.library_max_items:
            self._delete_locked(self._entries[0])
            removed += 1
        if removed:
            log.info("library: pruned %d photos over the %d cap", removed,
                     self.settings.library_max_items)
        return removed
