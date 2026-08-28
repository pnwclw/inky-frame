# CLAUDE.md — Inky Frame Dashboard

Knowledge base for this project. Read this before changing anything; the hardware
and the dithering pipeline have non-obvious constraints that are easy to break.

---

## 1. What this is

A small FastAPI service running on a **Raspberry Pi 5** that drives a **Pimoroni
Inky Impression** e-paper panel — either the **7.3"** (800×480, the current
default) or the **13.3"** (1600×1200), chosen at runtime by one setting (see
"Panel selection" below).

```
 iOS Shortcut ───HTTP POST image──▶┌───────────────────────────────┐
                                   │  FastAPI (app/main.py)        │
 HA automation ◀──MQTT button──────│   ├─ DisplayManager (panel)   │──SPI/GPIO──▶ Inky 7.3"/13.3"
                                   │   ├─ ButtonWatcher (gpiod)    │◀─GPIO──────  4 buttons
 HA ◀──state + discovery───────────│   ├─ MqttBridge (paho)        │
 HA ──▶cmd/nav|show|prefs|url──────│   ├─ DashboardRenderer (stub) │
                                   │   ├─ PhotoLibrary (data/)     │
                                   │   └─ Prefs (mounting, fit)    │
                                   └───────────────────────────────┘
                              packaged with uv, run via docker compose
```

### Panel selection (7.3" vs 13.3") — the one switch

Everything that differs between the two panels lives on a `PanelSpec` in
**`app/panels.py`**: the inky driver class, the native resolution, which GPIO
button C is on, the busy-wait ceiling, and the Home-Assistant identity. One setting
picks the panel and derives all of that:

```
PANEL=impression_73   # 7.3", 800×480,   InkyE673,     button C=GPIO16, ceiling 40s  (default)
PANEL=impression_13   # 13.3", 1600×1200, InkyEL133UF1, button C=GPIO25, ceiling 50s
```

`DEFAULT_PANEL` in `app/panels.py` is the fallback when `PANEL` is unset (currently
`impression_73`). `Settings` (`app/config.py`) exposes `settings.panel_spec` and a
`model_validator` that fills `button_gpio_c` / `device_id` / `device_name` from the
spec **unless** those env vars are set explicitly. To add a third panel: add one
`PanelSpec` entry — nothing else in the app hard-codes a resolution or driver (the
dashboards scale to `self.size` (§10); `DisplayManager` reads resolution + driver
from the spec / live driver; `gpio_compat` patches the spec's driver class).

Both panels are **Spectra 6 (E6)**: identical six colours and palette order, and —
handy — inky 2.4.0's 7.3" `InkyE673` ships the *same* `_busy_wait` code as the
13.3" `InkyEL133UF1`, so the refresh/ghosting fix in `gpio_compat` (§12) applies to
both; only the ceiling differs (smaller for the faster 7.3", still with margin).

Design choices that are already decided (don't relitigate without reason):

- **Single process / single worker.** The panel and its GPIO lines must be owned
  by exactly one process. `uvicorn` runs with one worker. Refreshes are
  serialized by an `asyncio.Lock` in `DisplayManager`.
- **LAN-only, no auth.** The API trusts the local network. Do **not** forward the
  port from the router. If you later expose it, put it behind Tailscale or a
  reverse proxy with auth — there is a natural place to add a dependency in
  `app/main.py`.
- **External MQTT broker.** We connect to the existing Mosquitto in Home
  Assistant; we do not run a broker.
- **Dashboards are a stub.** Only the seam exists (`app/dashboards.py`); see §10.

---

## 2. Hardware facts (Inky Impression, 2025 Spectra editions)

Rows that differ per panel show both; the rest are shared. `PANEL` (§1) selects which.

| Property | 13.3" (`impression_13`) | 7.3" (`impression_73`, default) |
|---|---|---|
| Panel / controller | **EL133UF1** (`inky.InkyEL133UF1`, `inky/inky_el133uf1.py`) | **E673** (`inky.InkyE673`, `inky/inky_e673.py`) |
| Resolution | **1600 × 1200**, landscape | **800 × 480**, landscape |
| Colours | **6-colour Spectra 6 (E6)**: black, white, red, green, blue, yellow | *same* |
| Full refresh | **~30 s** (stock `_busy_wait(32.0)`, no partial refresh); our BUSY-poll ceiling **50 s** | faster (fewer pixels); our ceiling **40 s** |
| GPIO library | **gpiod v2** (`gpiod` + `gpiodevice`), *not* RPi.GPIO | *same* |
| Display control pins | CS0=GPIO26, **CS1=GPIO16**, DC=GPIO22, RESET=GPIO27, BUSY=GPIO17 | single chip-select (no CS1); DC/RESET/BUSY same |
| Button C GPIO | **25** (GPIO16 is taken by CS1) | **16** (single CS leaves GPIO16 free) |
| Buttons A/B/D | 4 tactile buttons A/B/C/D — A=GPIO5, B=GPIO6, D=GPIO24 | *same* |
| EEPROM | on-board, over I2C (`/dev/i2c-1`); `inky.auto.auto()` reads it to identify the board | *same* |

### Buttons → GPIO (the one gotcha)

| Button | BCM GPIO | Pi 5 header pin |
|---|---|---|
| A | 5 | 29 |
| B | 6 | 31 |
| **C** | **16** (7.3") / **25** (13.3") | **36** / **22** |
| D | 24 | 18 |

Button C is the one that moves. On the **7.3"** (and the other small Impressions)
it is GPIO **16**; on the **13.3"** it is moved to GPIO **25**, because there
**GPIO 16 is the display's second chip-select (CS1)**. The value is derived from
`PANEL` (§1) — `app/panels.py` sets `button_gpio_c` (16 for `impression_73`, 25 for
`impression_13`), overridable with `BUTTON_GPIO_C`. If you ever see button C "not
working" or fighting the display, this mismatch is why.

The buttons are wired pull-up, active-low → we watch for the **falling edge**.

### DESATURATED_PALETTE index order (used by the dithering handoff, §3)

`inky`'s Spectra drivers (**EL133UF1** and **E673**) both expect mode-"P" images
with this identical index order:

```
0 = black    (0,0,0)
1 = white    (255,255,255)
2 = yellow   (255,255,0)
3 = red      (255,0,0)
4 = blue     (0,0,255)
5 = green    (0,255,0)
```

`DisplayManager.init_driver()` reads `driver.DESATURATED_PALETTE` at runtime so we
stay correct if Pimoroni reorders it; the same list is hardcoded as a fallback in
`app/dithering.py` (`INKY_PALETTE_RGB`).

---

## 3. The dithering pipeline (most important section)

File: `app/dithering.py`.

### The trap: double dithering

Both libraries can quantize an image to the palette:

- `epaper-dithering` does a high-quality, perceptually-correct (OKLab) error
  diffusion pass.
- `inky.set_image()` will *also* dither any non-"P" image, via PIL's
  `image.im.convert("P", True, palette)` (the `True` = dithering on). There is no
  flag to turn that off.

If you feed inky an RGB image, **you dither twice** and the second pass adds noise
to an already-posterized image.

### The fix: hand inky a "P" image

`inky.set_image()` passes mode-"P" images through **unchanged** (it just reads the
palette indices). So the pipeline is:

```
photo (any size/mode)
  └─ cover/contain to the panel size     (app/dithering.py: _cover / _contain;
                                          800×480 on the 7.3", 1600×1200 on the 13.3")
  └─ dither_image(..., ColorScheme/palette, mode=FLOYD_STEINBERG, ...)   -> RGB, 6 colours
  └─ .quantize(palette=INKY_PALETTE, dither=Image.Dither.NONE)           -> "P" image
        (lossless 6→6 relabel into inky's index order — NO second dither)
  └─ inky.set_image(p_image); inky.show()
```

`dither=Image.Dither.NONE` is essential — the dithered image already contains only
the 6 palette colours, so this step is a pure nearest-colour relabel.

### Tuning knobs (env / `app/config.py`)

`epaper-dithering`'s `dither_image(image, palette, *, mode, serpentine, exposure,
saturation, shadows, highlights, tone, gamut)`:

- `DITHER_MODE` — `FLOYD_STEINBERG` (default), `ATKINSON`, `STUCKI`, `SIERRA`,
  `JARVIS_JUDICE_NINKE`, `BURKES`, `SIERRA_LITE`, `ORDERED`, `NONE`.
- `DITHER_COLOR_SCHEME` — `BWGBRY` is the 6-colour Spectra scheme (default).
  Others: `MONO`, `BWR`, `BWY`, `BWRY`, `GRAYSCALE_4/8/16`.
- `DITHER_PALETTE` — a *measured* palette constant (e.g. `SPECTRA_7_3_6COLOR_V2`).
  When set it **overrides** the scheme and usually gives more accurate photo
  tones. On the **7.3"**, `SPECTRA_7_3_6COLOR_V2` is a direct match. There is no
  13.3"-specific measured palette shipped, but the 7.3" one is a good starting point
  there too (both are Spectra 6 (E6)). The §3 handoff still produces correct output
  with a measured palette (the 6 measured colours each map to a distinct pure colour).
- `DITHER_EXPOSURE`, `DITHER_SATURATION`, `DITHER_SHADOWS`, `DITHER_HIGHLIGHTS`,
  `DITHER_TONE` (`auto`/...), `DITHER_GAMUT` (`auto`/...) — tone/colour mapping
  before dithering. e-paper can't reproduce full dynamic range, so the library
  compresses tones; tweak these if photos look washed out or muddy.
- `INKY_SATURATION` (0–1, passed to `inky.set_image`) blends inky's
  desaturated↔saturated palette at display time. Independent from the dithering
  saturation above.

`render_for_inky` also takes **`rotate`** (any angle, clockwise) and **`crop`**.
Two rotations are in play and they are not the same thing: `rotate` turns the
*picture*, `orientation` turns the *canvas* because the frame hangs that way.
`rotated_size()` exists because `fit=auto` compares aspect ratios, and turning a photo
90° swaps its own.

**Rotation is not limited to quarter turns** — the crop editor straightens horizons by
hand. Three things follow from that and all three are load-bearing:

- `apply_rotation()` keeps the transpose fast path for 0/90/180/270 (pure pixel moves,
  no resampling) and falls back to `Image.rotate(-angle, BICUBIC, expand=True)`. PIL
  counts counter-clockwise, hence the minus.
- The photo then occupies its **bounding box**, and `crop` is expressed in *that box's*
  pixels — so `rotated_size()` must reproduce PIL's `expand=True` maths exactly
  (`ceil(max) - floor(min)` about the centre), because the browser computes the
  rectangle in the same space. `app/gallery.py`'s `rotatedSize()` is the same formula;
  they are checked against each other, don't change one alone.
- The corners the rotation exposes are filled **white**, the colour `_crop_to()` pads
  with, so both kinds of empty space dither identically.

`normalise_angle()` folds any angle into [0, 360) and rounds to a **tenth of a degree**
— finer than the panel can show, and coarse enough that a drag doesn't mint a render
per hundredth. `render_key()` formats it with `:g`, which prints whole degrees without
a decimal point, so every key minted back when only quarter turns existed still hashes
the same and the renders already on disk stay usable.

**`crop` is the real placement**; `fit` only names one of two rectangles. It is
`[x, y, w, h]` in pixels of the *rotated* photo and **may extend past its edges** —
what falls outside becomes white. That is what makes one code path cover `cover`
(the biggest canvas-shaped rectangle inside the photo), `contain` (the smallest one
containing it) and every position a person drags to in between. `default_crop()`
computes the two presets; `_crop_to()` maps any rectangle onto the canvas.

### How to iterate on look without burning 30s per try

`POST /display/image?show=false` renders + writes `data/latest.png` **without**
refreshing the panel. Pull it from `GET /display/preview`, eyeball it, adjust env,
repeat. Only refresh the panel when you're happy.

---

## 4. HTTP API

| Method & path | Purpose |
|---|---|
| `GET /` | **the gallery** — thumbnail grid, tap to show, upload (§4 below) |
| `GET /gallery` | the same page; this is the URL the HA Webpage panel uses |
| `GET /setup` | iOS Shortcut download + instructions (§5) |
| `GET /inky-frame.shortcut` | the generated shortcut (`.shortcut` plist), URL baked from the request host |
| `GET /healthz` | liveness |
| `GET /status` | `{display:{driver,resolution,busy,last_shown_at,last_error,mock}, mqtt_connected}` |
| `POST /display/image` | accept an image, dither, show |
| `POST /display/clear` | blank panel to white |
| `POST /display/dashboard?name=…` | render + show a dashboard (stub → 501 for unknown names) |
| `GET /display/preview` | last rendered image as PNG (`?view=true` rotates it upright for a portrait-mounted frame) |
| `POST /display/url?url=…` | fetch an image over http(s), dither, show |
| `POST /display/library/{id}` | show a stored photo: `?render=<key>` puts a saved render back verbatim, otherwise it renders afresh |
| `POST /display/nav?direction=…` | show the `next`/`prev`/`random` stored photo |
| `POST /library` | **upload a photo without rendering or showing it** |
| `GET /library` | paged index (`?collection=<id>` filters) |
| `GET /library/{id}` | one photo, with every render made from it |
| `PATCH /library/{id}` | rename, or replace which collections it is in |
| `GET /library/{id}/{original\|thumb}` | the photo's own files |
| `POST /library/{id}/render` | make (or reuse) a render with given `crop`/`rotate`/`dither` |
| `GET /library/{id}/renders/{key}[/thumb]` | one render |
| `DELETE /library/{id}/renders/{key}` | drop one render |
| `DELETE /library/{id}` | forget a photo (index entry + original, thumb and every render) |
| `GET`/`POST /collections`, `PATCH`/`DELETE /collections/{id}` | collections |
| `GET /prefs` | device settings + their valid values |
| `PATCH /prefs` | change device settings (JSON body; same values HA edits) |

`POST /display/image` accepts the image **either** as the raw request body
(`Content-Type: image/*`) **or** as multipart field `file`. Query params:

- `fit=cover` (scale + centre-crop to fill), `fit=contain` (pad white) or
  `fit=auto`. **Omit it** and the device setting applies — see "Device settings"
  below; the shipped default is `auto`.
- `orientation=landscape` or `orientation=portrait` — for a frame mounted
  vertically. Portrait lays the photo out on a rotated canvas (H×W), then rotates
  the finished image 90° back to the panel's native landscape W×H — e.g. 480×800 →
  800×480 on the 7.3", 1200×1600 → 1600×1200 on the 13.3" (see `render_for_inky` in
  `app/dithering.py`; it derives the canvas from the panel size). **Omit it** and
  how the frame is mounted (a device setting) applies.
- `dither=<algorithm>` — override the dithering algorithm for this one request
  (case-insensitive; default `DITHER_MODE` = `JARVIS_JUDICE_NINKE`). Valid names:
  `ATKINSON`, `BURKES`, `FLOYD_STEINBERG`, `JARVIS_JUDICE_NINKE`, `NONE`, `ORDERED`,
  `SIERRA`, `SIERRA_LITE`, `STUCKI` (also listed under `dither` at `GET /status`).
  Unknown name → 400. Independent from the `DITHER_MODE` env default.
- `wait=true|false` — block until the ~30 s refresh finishes. Default from
  `DISPLAY_WAIT_DEFAULT` (false → returns immediately, refreshes in background).
- `show=false` — render a preview only, don't touch the panel.
- `store=true|false` — file the photo in the library (see below). Defaults to
  `show`, so tuning dithering with `show=false` doesn't fill the library with
  near-duplicates.
- `name=…` — optional label carried on the library entry.

### Device settings ("prefs")

`app/prefs.py`, persisted to `<OUTPUT_DIR>/prefs.json`. Deliberately *not* env vars:

| | |
|---|---|
| `Settings` (`app/config.py`) | deploy-time, read-only, from `.env`. Which panel, which GPIOs, broker credentials. |
| `Prefs` (`app/prefs.py`) | runtime, edited from Home Assistant or `PATCH /prefs`, persisted, survives a restart. |

The env keys `DEFAULT_ORIENTATION` / `DEFAULT_FIT` / `AUTO_FIT_MAX_CROP` /
`DITHER_MODE` only **seed** `prefs.json` on first run. After that the file wins —
otherwise a deploy would silently revert a choice made from HA. Delete `prefs.json`
to re-seed.

| pref | values | meaning |
|---|---|---|
| `orientation` | `landscape` / `portrait` | how the frame is physically **mounted** |
| `fit` | `auto` / `cover` / `contain` | default placement for an incoming photo |
| `auto_fit_max_crop` | 0.0–1.0 | the threshold `auto` uses |
| `dither` | any algorithm name | default dithering for new photos |

**`fit=auto` is the point of this file.** `cover` looks best — photo edge to edge —
but only while the aspect ratios are close; forcing a portrait photo onto a
landscape frame with `cover` throws away 44% of it. `auto` measures what `cover`
would crop (`crop_loss` in `app/dithering.py`: `1 - min(a,c)/max(a,c)` on the
aspect ratios) and picks `cover` while that stays under `auto_fit_max_crop`,
`contain` otherwise. On a landscape-mounted 4:3 panel with the default 0.30:

| photo | crop if cover | `auto` picks |
|---|---|---|
| 4:3 | 0% | cover |
| 3:2 | 11% | cover |
| 16:9 | 25% | cover |
| 2:1 panorama | 33% | **contain** |
| 3:4 portrait | 44% | **contain** |

Mount the frame vertically and the table inverts — the decision is made against the
*working canvas* (`working_canvas()`), i.e. the panel rotated for the mount.

Two behaviours worth knowing:

- **Explicit query params always win.** The iOS Shortcut bakes
  `?orientation=…&fit=…` into its four menu leaves, so it keeps working exactly as
  before and ignores these settings. A bare `POST /display/image` with no params is
  what follows the device settings.
- **Changing `orientation` re-shows the current photo** (one refresh). The other
  settings are defaults for *future* photos and don't touch the panel — otherwise
  fiddling with the dither dropdown would cost 30 s a click.

Re-showing a stored photo (`POST /display/library/{id}`, `POST /display/nav`, the HA
picker) resolves these the same way, with one split: **geometry follows the frame,
look follows the photo.** fit and orientation come from prefs, because they describe
how the frame hangs *now*; the dither comes from the library entry, because
`?dither=` on a specific photo is an explicit choice worth remembering.

### The photo library

Code: `app/library.py`; settings are the `LIBRARY_*` block in `.env.example`.

```
photos/<pid>.<ext>             the photo as received. Downscaled only if its long edge
                               exceeds LIBRARY_ORIGINAL_MAX_SCALE x the panel's (2.0 ->
                               3200 px on the 13.3"); otherwise kept BYTE-FOR-BYTE for
                               jpeg/png/webp.
thumbs/<pid>.jpg               thumbnail of the ORIGINAL, for the grid.
renders/<pid>-<key>.png        one dithered panel buffer, mode "P" — a 6-colour palette
                               PNG, pixel-identical to RGB at ~half the bytes.
render-thumbs/<pid>-<key>.jpg  thumbnail of that render, rotated to VIEWING orientation.
library.json                   photos, collections, and what is on the panel now.
```

Four things about it are load-bearing:

- **A photo has MANY renders.** The crop rectangle, rotation, dithering algorithm and
  the panel's resolution all change the result, and choosing between them is the point
  of the picker. A render's `key` is a hash of exactly those (`render_key()`) — of the
  *rectangle*, not the preset that produced it, because two presets can land on the
  same rectangle — so asking for the same look twice reuses the file instead of piling
  up near-duplicates. Rounded to whole pixels, or a drag ending a hundredth of a pixel
  away would make a second render. Resolution is in the hash on purpose: after a panel
  swap the old render is the wrong size and must not be reused —
  `_show_existing_render` re-renders instead.
- **Uploading does not render.** `POST /library` only archives the original and makes
  a thumbnail. A selection can be dozens of photos, rendering is ~1 s each and showing
  is a ~30 s refresh, so what to show stays a separate, deliberate act.
- **The original is the source of truth.** Every render is produced from `photos/`, so
  changing `DITHER_*` or swapping panels still gives a correct result for an old photo.
- **Dedup is by SHA-256 of the received bytes.** Re-sending a photo the library already
  holds refreshes the entry and moves it to the end (so it sorts to the top of the
  newest-first listing) instead of making a second copy. Note this hashes the *bytes*,
  so the same picture re-encoded by a different app is a different entry.

**Collections are many-to-many.** A photo can be in several at once, so a collection
holds an ordered `photo_ids` list and membership is derived from that; deleting a
collection never deletes photos.

**`current` names a photo *and* a render**, so after any restart the service knows
exactly what the e-paper is still showing. The library lives in the frame service's
`./data` volume — Home Assistant restarts have nothing to do with it.

Order is chronological, oldest first, internally: appending is O(1), pruning past
`LIBRARY_MAX_ITEMS` is a slice off the front, and `next`/`prev` mean later/earlier in
time (they wrap). `GET /library` reverses it, because newest-first is what a gallery
wants; inside a collection the collection's own order wins.

**The index is versioned, and migrations move files.** Dry-run one on a copy of the
Pi's `data/` before deploying.

- **v1 → v2**: v1 gave each photo exactly one render at `renders/<id>.png` and used
  `thumbs/` for a thumbnail of THAT RENDER. The render moves to
  `renders/<id>-<key>.png`, its thumbnail to `render-thumbs/`, and `thumbs/` is
  regenerated from the original (a photo can have zero renders now, and the grid still
  has to show something). `_migrate_locked` is handed the RAW json, because v1 kept the
  render's parameters on the photo and the v2 dataclass drops them.
- **v2 → v3**: v2 renders predate the crop rectangle. `_migrate_v3_locked` fills it in
  from the `fit` preset and then **re-keys** them — the key *is* the parameters, so a
  render whose key no longer describes it would be served for the wrong look — which
  means renaming its two files and re-pointing `current_render`. `crop` has a dataclass
  default for exactly this: without one, loading a pre-crop index would drop every
  render as malformed.

### The gallery page

`GET /` (and `/gallery`, which is what Home Assistant's panel points at) — `app/gallery.py`.
Shaped like a phone photo app: a square-tile grid, a tab bar at the bottom for
**All / Collections / Add**, and a full-screen sheet when you open a photo. Tiles carry
a badge with the render count and an ON FRAME pin for whatever is on the panel.

The sheet is where a photo becomes a frame, and it is built like a photo editor:
every control is an icon, and the toolbar is down to **crop · algorithm · Show** —
placement is a gesture, so the only geometry control outside the editor is the door
into it. Above the toolbar, a strip of every render already made from this photo, each
with its own delete button so trying things out doesn't leave rubbish behind. The
header carries rename and delete.

- **Crop is a drag, not a dropdown.** The crop button swaps the stage for the photo
  behind a frame in the panel's exact shape: drag to move, scroll or pinch to zoom,
  twist or work the dial to straighten, everything outside dimmed. Leaving crop mode
  renders the rectangle you chose.
- **The presets became detents.** There are no cover/contain buttons; the *zoom* snaps
  as it passes the scale that exactly fills the frame and the one that shows the whole
  photo, so both are still one gesture away and land exactly. Angles snap every 45°,
  and the photo's edges and centre snap to the frame's. `coverScale()` derives the
  fill scale by turning the *frame* by -angle and requiring its bounding box to fit the
  photo — conservative when tilted, which is the safe direction: it never leaves white.
- **Rotation keeps the frame's centre still.** Changing the angle resizes the photo's
  bounding box, which moves every coordinate, so `setAngle()` reads the point of the
  unrotated photo currently under the frame's centre (`photoPointAt`) and puts it back
  afterwards (`placePhotoPointAt`). Without that the picture slides away as you
  straighten it.
- **Undo is per gesture, not per event.** One entry is pushed when a drag, pinch, dial
  drag or wheel burst *starts*; a drag across the whole stage is one thing you did.
- **Press and hold the preview to compare with the original.** The dithered result is
  what you are judging, and holding shows what it came from.
- **The algorithm picker explains itself** — a sheet of cards with what each one does
  to a photo (`DITHER_INFO`), because `JARVIS_JUDICE_NINKE` in a dropdown tells nobody
  anything.
- **Prompts are drawn in the page** (`ask()`), not `window.prompt`. Chrome ignores
  `prompt`/`confirm` in a **cross-origin iframe**, which is exactly how Home Assistant
  embeds this page in the sidebar — renaming and deleting silently did nothing there.
- Changing anything calls `POST /library/{id}/render` (`show=false`) and swaps the
  preview; the panel is only ever touched by **Show**. Opening a photo with no renders
  makes one, so you see the e-paper version rather than the photo. A veil with a
  spinner covers the stage while a render is in flight, and a stale response is
  discarded (`renderSeq`) so a fast series of adjustments lands on the last one.

Showing an existing render goes through `POST /display/library/{id}?render=<key>`,
which pushes the stored "P" PNG straight to the panel (`DisplayManager.show_rendered`).
Nothing is recomputed, so you get byte-for-byte the look you picked — and `/display/nav`
uses the same path, which is why stepping through the library is fast.

Uploads (＋ or drag-and-drop) go to `POST /library`: filed, not rendered, not shown.
The filename becomes the label unless it is a placeholder like the `image.jpg` iOS
hands out from the photo picker. Inside a collection, uploads land in it.

**Every URL in the page is relative**, so it behaves the same on `http://<ip>:8080` and
behind the Tailscale HTTPS proxy — which is what keeps it out of mixed-content trouble
inside an HTTPS Home Assistant (§6). No build step and no CDN: the Pi has no business
fetching a framework to draw a grid, and the frame should keep working when the
internet doesn't.

**Four traps, all learned the hard way.**

*Dragging an image inside the page uploaded it.* The browser hands a dragged `<img>`
to the drop target as a FILE, so the page's own drop handler treated moving the crop
as an upload and quietly added a copy of the photo to the library. Every image is
`draggable="false"` with `-webkit-user-drag: none`, and a drop while the sheet is open
is ignored outright.

*Loading the page must not touch the panel.* A browser restoring form state across a
navigation fires `change` by itself — that once put two photos on the panel with no
click at all. Every `change` handler checks `event.isTrusted` and the value's validity.
Regression check: load the page twice and confirm zero non-GET requests.

*The crop frame needs a laid-out stage.* Sizing it against a zero-width element sets
the scale to 0 and the photo vanishes; a later drag then clamps the scale to 0
permanently. A `ResizeObserver` on the stage does the layout, and `clampView()` returns
early until the frame has a size.

*The render strip only scrolls while it is a grid item.* An `overflow-x: auto` box has
an automatic minimum size of 0 **as a flex/grid item**; wrapped in a plain block it
hands its full min-content width upward instead, and eight thumbnails pushed the Show
button off the screen. `#foot-view` / `#foot-crop` are grids with `min-width: 0` for
exactly that reason — and they need an explicit `[hidden] { display: none }`, because
an id selector outranks the UA sheet's.

*A render is the panel BUFFER* — always the panel's native landscape W×H, with a
portrait layout transposed into it. Anything showing it to a human turns it back with
`ROTATE_270`: `PhotoLibrary._write_render_files` does it for the render thumbnail (never
for the render itself, which stays panel-accurate), and `GET /display/preview?view=true`
does it on the fly. `inky-frame/state.image_url` uses `?view=true` too.

## 5. iOS Shortcut (auto-generated)

The shortcut is **generated and served by the app** — there's nothing to author by
hand. Code: `app/shortcut.py`; served from `GET /` (landing page) and
`GET /inky-frame.shortcut`.

User flow:

1. On the iPhone, open **`http://<pi-host>:8080/`** in Safari. The page
   auto-downloads the shortcut (with a manual button as backup).
2. Install it (**Add Shortcut**), then run it or share a photo to it.
3. It pops a menu — pick one of: Landscape/Portrait × Cover/Fit — and posts the
   photo to the matching endpoint.

How it's built (`app/shortcut.py`): a `Select Photos` action + one
`Choose from Menu` with four leaves; **each leaf is a `Get Contents of URL` with a
literal URL** carrying the chosen `orientation` + `fit` (e.g.
`…/display/image?orientation=portrait&fit=cover`). Four literal URLs avoid
variable/text-token plumbing in the plist. The endpoint host is taken from the
request, so whatever address the user opened the page on is baked into the
shortcut. Body is the selected photo as a `File` (raw-body path of the API).

### Important: unsigned-shortcut caveat

Apple-signed shortcuts are AEA archives; we can't sign offline, so the served file
is a **plain XML plist**. Importing it requires the user to enable
**Settings → Shortcuts → Allow Untrusted Shortcuts** once (the toggle only appears
after at least one shortcut has been run). The landing page spells this out, and
also lists the **manual two-action build** (Select Photos → Get Contents of URL
POST to `/display/image`) as a fallback if a given iOS version refuses the import.
This is the one part that can't be verified from the repo — test it on a real
device.

No auth header is needed (LAN-only). If you later add a token, add it both as a
header in `app/shortcut.py`'s `_post_image_action` (`WFHTTPHeaders`) and as a check
in `app/main.py`.

---

## 6. MQTT & Home Assistant

File: `app/mqtt.py`. Connects to your existing broker (`MQTT_*` env).

### Topics

| Topic | Direction | Payload |
|---|---|---|
| `inky-frame/button/A` … `/D` | publish | `PRESS` (on each debounced press) |
| `inky-frame/availability` | publish (retained, LWT) | `online` / `offline` |
| `inky-frame/state` | publish (retained) | JSON: what the panel is doing (below) |
| `inky-frame/prefs` | publish (retained) | JSON: the current device settings (§4) |
| `inky-frame/cmd/nav` | subscribe | `next` \| `prev` \| `random` |
| `inky-frame/cmd/show` | subscribe | a photo id (or a picker label, see below) |
| `inky-frame/cmd/url` | subscribe | an http(s) image URL |
| `inky-frame/cmd/dashboard` | subscribe | a dashboard name |
| `inky-frame/cmd/clear` | subscribe | `{}` or `{"cycles":3,"delay":1.5}` |
| `inky-frame/cmd/prefs` | subscribe | `{"orientation":"portrait"}` |
| `inky-frame/command` | subscribe | legacy alias: `verb:payload`, e.g. `nav:random` |

(`inky-frame` = `MQTT_BASE_TOPIC`.) Command payloads are JSON but stay friendly to a
hand-typed `mosquitto_pub`: empty means `{}`, and `key=value` works for a single
setting.

**MQTT is the control plane; HTTP stays the data plane.** No image bytes ever go
through the broker — a photo is megabytes, a retained image topic would be re-sent
to every subscriber on connect, there is no back-pressure for a 30 s refresh, and
iOS Shortcuts can't speak MQTT anyway. The `image` entity publishes a *URL* that
Home Assistant fetches over HTTP.

`inky-frame/state` carries the sensor's state plus every attribute:

```json
{"status":"idle","busy":false,"panel":"impression_13","resolution":[1600,1200],
 "driver":"Inky","mock":false,"last_shown_at":1787927725.8,
 "last_shown_iso":"2026-08-28T16:35:25+02:00","last_error":null,
 "photo_id":"20260828-163525-5c763b15","photo_name":"GuestBG","library_count":3,
 "image_url":"http://192.168.68.27:8080/display/preview?v=1787927725",
 "orientation":"landscape","fit":"auto","auto_fit_max_crop":0.3,"dither":"..."}
```

`image_url` is built from `PUBLIC_BASE_URL`, which defaults to the primary LAN
address (`http://<ip>:<port>` — a connected-but-silent UDP socket asks the kernel
which source address the default route uses). Set it explicitly if HA should reach
the frame over Tailscale or a reverse proxy instead. The `?v=` is a cache-buster;
without it HA keeps showing the first frame it fetched.

### Entities live in the custom integration, not in discovery

The frame publishes **no** Home Assistant discovery configs. Every entity comes from
`homeassistant/custom_components/inky_frame` (below), so one place describes the
device instead of two, and adding an entity is a Python class rather than
hand-written JSON plus retained-topic bookkeeping.

`MqttBridge` keeps one duty from the old scheme: `_retire_legacy_discovery()` publishes
an empty payload, **on every connect**, to each of the 18 config topics this service
used to publish. Those configs are retained, so a broker that still holds one would
resurrect that entity on the next connect — clearing them is a standing duty, not a
one-off migration. `LEGACY_ENTITIES` is that list; leave it in place.

What MQTT still carries, and why:

| | |
|---|---|
| `state` / `prefs` (retained) | a "something changed, re-read" signal — the integration re-polls `/status` instead of waiting up to 30 s |
| `button/A`…`/D` | the **only** thing that needs MQTT: a physical press has no HTTP equivalent |
| `cmd/*` | so automations and `mosquitto_pub` can drive the frame without the integration |

The integration is not a hard MQTT dependency (`after_dependencies`, not
`dependencies`): with MQTT absent it polls every 30 s and the four button entities
simply never fire.

Automations use the `event` entities now, not the old MQTT device triggers:

```yaml
automation:
  - alias: "Inky button A → toggle desk lamp"
    trigger:
      - platform: state
        entity_id: event.inky_frame_13_3_button_a
    action:
      - service: light.toggle
        target: { entity_id: light.desk_lamp }
```

Or stay off HA entirely and trigger on the raw topic:

```yaml
    trigger:
      - platform: mqtt
        topic: inky-frame/button/B
        payload: PRESS
```

### The `/media/inky` mount: removed, and why (history)

`data/renders/` was once bind-mounted into Home Assistant as `/media/inky:ro`, so the
library showed up under **Media → Local Media**. It is **gone** — the custom
integration below replaced it and the two interacted badly:

- **No thumbnails.** HA's built-in `media_source/local_source.py` never sets
  `thumbnail` (grep it: the word does not appear), so it browsed as a list of
  filenames. The integration sets it, so its browser has previews.
- **It duplicated photos.** Playing a file from Local Media handed the frame a URL to
  its own `renders/` folder; the frame fetched its own render back and filed it as a
  NEW library photo, because dedup hashes the bytes received and a render's bytes are
  not the original's. Every play grew the library by one and re-dithered an
  already-dithered image.

If it is ever wanted back, it was one line on the `homeassistant` service in the Pi's
**shared** `~/services/docker-compose.yaml` (root-owned, not this repo — HA's volumes
belong to HA's service definition):

```yaml
      - ~/services/inky-frame-dashboard/data/renders:/media/inky:ro
```

HA in Docker with no `media_dirs` key in `configuration.yaml` registers `/media` as
the `local` media source by itself (`core_config.py`), so no HA config is needed —
but browse the library through the integration instead.

### The custom integration (`media_player`)

`homeassistant/custom_components/inky_frame/` — every entity for the frame:

| Entity | Platform | What it does |
|---|---|---|
| Status | sensor | `idle` / `refreshing` / `error`; panel, resolution, current photo, library size and all four prefs as attributes |
| Refreshing | binary_sensor | the panel is mid-redraw |
| Current frame | image | `GET /display/preview?view=true`, fetched by HA (never by the browser) |
| Mounting / Fit / Dithering | select | the prefs, written with `PATCH /prefs` |
| Auto-fit max crop | number | the `fit=auto` threshold, as a % slider |
| Next / Previous / Random photo | button | `POST /display/nav` |
| Clear panel / Deep de-ghost | button | `POST /display/clear` (de-ghost holds the panel for minutes) |
| Dashboard: … | button | one per name in `GET /status.dashboards` |
| Button A–D | event | the physical buttons, fed by MQTT |
| (the frame itself) | media_player | browse + play, below |

**Control is HTTP, always.** Every action calls the frame's own API, so the
integration works with the MQTT integration absent. `GET /status` is the single source
for identity, prefs, dither list and dashboards — nothing is configured twice.

The `media_player` adds the two things Home Assistant cannot do on its own:

- **Browse with thumbnails.** It sets `thumbnail` on each `BrowseMedia`, using
  `get_browse_image_url()` so the image is proxied by Home Assistant's own
  `media_player_proxy` and fetched back through `async_get_browse_image()`. The
  frame's URL never reaches the frontend, so it works over HTTPS, from outside the
  LAN, under Home Assistant's auth — no signed paths or custom HTTP view needed.
- **Play.** `async_play_media` handles three shapes: `library/<id>` (a photo the frame
  already holds → `POST /display/library/<id>`), a `media-source://…` id (resolved,
  then `async_process_play_media_url` → `POST /display/url`, so the bytes go straight
  from HA to the frame), and a bare http(s) URL. That is what puts **"Play on Inky
  Frame"** on every other Home Assistant media source.

Plus next/previous (→ `POST /display/nav`) and the current frame as the media image
(`?view=true`, fetched by HA server-side since `media_image_remotely_accessible` is
False).

**Two device identifiers, on purpose** (`entity.py`). `("inky_frame", <id>)` says who
owns the device now. `("mqtt", <id>)` is what the MQTT integration created back when
this service published discovery configs — MQTT namespaces a payload's
`device.identifiers` under its own domain, so the registry entry was
`[["mqtt", "inky_frame_13"]]`. Keeping it makes the registry match the **existing**
device record, so the area, any renamed entities and the config entry already added
survive the move; the live entry now reads `[["inky_frame", …], ["mqtt", …]]` with
both config entries attached. `MQTT_DOMAIN` is a literal in `const.py`, not an import,
so the integration loads without MQTT.

State is a 30 s `DataUpdateCoordinator` poll of `GET /status`, with the MQTT `state`
topic collapsing that to "re-read now" when it fires. The MQTT payload is deliberately
*not* parsed into coordinator data — its shape differs from `/status`, and one shape
is worth more than saving one local HTTP call.

### The sidebar panel

The integration registers a **panel_custom** sidebar entry ("Inky Frame") that embeds
the frame's own `/gallery`. A custom panel, not a generated Lovelace dashboard:
creating a dashboard from an integration means reaching into `lovelace`'s private
`DashboardsCollection`, while `panel_custom.async_register_panel` is a supported API —
and the page stays ours. `inky-frame-panel.js` is a small web component served from a
static path; it only re-renders when `panel.config.url` or the title changes, since
Home Assistant reassigns `hass` on every state update and re-rendering there would
reload the iframe constantly.

**The panel draws its own toolbar, and that is not decoration.** A custom panel is
handed the whole viewport and gets *no* Home Assistant chrome — unlike a Webpage
dashboard, which is drawn inside one. Without a menu button there is no way back to
the sidebar on a phone, where the sidebar is collapsed. The button fires
`hass-toggle-menu` (the same event the frontend's own toolbar uses), which reaches
Home Assistant because the panel is registered with `embed_iframe=False`: our element
lives in the HA document, so a `composed` event bubbles out of the shadow root to it.

**The module URL is versioned by the file's mtime** (`?v=…`, `panel.py:_module_version`).
Browsers cache ES modules hard and Home Assistant ships a service worker that caches
static assets, so without it an updated panel keeps running the old code until someone
clears their cache — which is what happens after *every* HACS update. `customElements.define`
is also guarded, because a page that outlives an update can import two copies and
defining the same name twice throws.

**The URL is a config-entry option, not derived from the host.** The two are answered
by different machines: Home Assistant reaches the frame on `127.0.0.1:8080`, while the
*browser* rendering the panel needs a routable address — and an `https://` one
whenever Home Assistant is served over HTTPS, or the browser blocks the iframe as
mixed content. Settings → Devices & services → Inky Frame → **Configure**. It defaults
to `GET /status.device.base_url` + `/gallery`, which is right on a LAN and wrong
behind a proxy.

The panel is global (one sidebar entry), so the first config entry registers it and
removes it on unload.

### Installing it: HACS, or by hand

The repo is laid out for **HACS**: `custom_components/inky_frame/` at the root plus
`hacs.json`, so the same repository is both the service and the integration (HACS
downloads only the `custom_components/` subtree). Add it as a custom repository of
category *Integration*, then install and restart. `.github/workflows/validate.yml`
runs hassfest and the HACS action on every push.

By hand — mutagen syncs the repo to the Pi, but Home Assistant reads
`~/services/homeassistant/config/custom_components`, which is root-owned, so copy it
in through the container:

```
tar -C ~/services/inky-frame-dashboard/custom_components -cf - inky_frame \
  | docker exec -i homeassistant tar -C /config/custom_components -xf -
docker exec homeassistant chown -R root:root /config/custom_components/inky_frame
cd ~/services && docker compose restart homeassistant
```

Then add it once from the UI: Settings → Devices & services → Add integration →
**Inky Frame** (the defaults, `127.0.0.1:8080`, are right when Home Assistant and the
frame share the Pi). Re-running the copy after a code change needs the restart again.

### Commands: how they run

`MqttBridge` calls `on_command(verb, payload)` **on paho's network thread**, so
`main.py`'s handler does nothing but `asyncio.run_coroutine_threadsafe` onto the
loop captured in the lifespan. Touching the panel from paho's thread would be a bug.

Every panel-touching command is **coalesced** (latest-wins — see §11): five quick
taps on "Next photo" must land on the fifth photo, not crawl through all five at
30 s apiece. HTTP posts are *not* coalesced; every photo you send is shown.

Bad payloads are user errors, not bugs: an unknown verb, a bad direction or an
invalid pref logs a one-line warning and is dropped. Nothing a broker publishes can
kill the loop.

---

## 7. Raspberry Pi host setup (do this once, before `docker compose up`)

The container talks to real device nodes, so the **host** kernel must expose them.

1. **Enable SPI and I2C.** `sudo raspi-config` → Interface Options, or in
   `/boot/firmware/config.txt`:
   ```
   dtparam=spi=on
   dtparam=i2c_arm=on
   ```
1b. **Free the SPI chip-select GPIO(s) — REQUIRED, or the panel can't refresh.**
   inky 2.4.0's Spectra drivers (both **E673** and **EL133UF1**) drive the
   chip-select themselves via gpiod, so the kernel must NOT hold GPIO8 (and GPIO7)
   as hardware CS. With plain `dtparam=spi=on` a refresh dies immediately with
   `SystemExit: Woah there, some pins we need are in use! ⚠️ Chip Select: (line 8,
   GPIO8) currently claimed by spi0 CS0`. Add to `/boot/firmware/config.txt`:
   ```
   dtoverlay=spi0-0cs
   ```
   This keeps `/dev/spidev0.0` (data) but removes the kernel CS pins; `/dev/spidev0.1`
   (CS1) disappears, so drop it from the compose `devices:` list (neither panel needs
   it under `spi0-0cs` — both manage CS via gpiod). **This line is easy to lose on an
   OS re-image**, and its absence is the #1 cause of "renders a preview but the panel
   never updates". Reboot after adding it.
2. **Enlarge the SPI buffer.** The **13.3"** (1600×1200) pushes large SPI transfers
   and the default 4 KB buffer is too small; the **7.3"** (800×480) is fine without
   it, but setting it is harmless. Append to the single line in
   `/boot/firmware/cmdline.txt`:
   ```
   spidev.bufsiz=65536
   ```
   (This is exactly what Pimoroni's `install.sh` does for the big Impressions.)
3. **Reboot**, then confirm the device nodes exist:
   ```
   ls -l /dev/spidev0.* /dev/i2c-1 /dev/gpiochip*
   ```
   On **Pi 5 / Bookworm** the 40-pin header is **`/dev/gpiochip0`** (older kernels
   named it `gpiochip4`). `docker-compose.yml` maps `gpiochip0`; if yours is
   `gpiochip4`, swap the commented line. `gpiodevice.find_chip_by_platform()`
   inside the container picks the right one as long as it's mapped in.
4. (debug) `i2cdetect -y 1` should show the EEPROM; `gpioinfo` lists the lines.

### What the container needs (already in `docker-compose.yml`)

- `devices:` → `/dev/spidev0.0`, `/dev/spidev0.1`, `/dev/i2c-1`, `/dev/gpiochip0`
- runs as root, so no `group_add` is required to access them.
- Blunt fallback if you still get `Permission denied` on a device:
  `privileged: true` (works, but grants the container all host devices).

---

## 8. Docker & uv

- **uv** manages deps. Core deps install everywhere; the **`hardware`** dependency
  group (`inky`, which pulls `gpiod`, `gpiodevice`, `spidev`, `numpy`, `smbus2`)
  is Linux/Pi-only and is installed **only in the image** via
  `uv sync --no-dev --group hardware` (see `Dockerfile`).
- The project is `package = false` (an app, not a library) — `uv sync` just builds
  the `.venv`, it doesn't try to build/install the repo as a wheel.
- **Adding a dependency:** `uv add <pkg>` (core) or `uv add --group hardware <pkg>`.
  Commit the updated `uv.lock` for reproducible image builds.
- **Build/run on the Pi:**
  ```
  docker compose up -d --build
  docker compose logs -f
  ```
- **arm64 wheels.** `epaper-dithering` is Rust-built; if there's no arm64 wheel for
  your Python, the first build will be slow or fail. The `Dockerfile` keeps
  `build-essential` as a safety net and has a commented Rust-toolchain block to
  enable if needed. Same idea for `gpiod` if its wheel is missing.

---

## 9. Local dev (no hardware)

`gpiod` won't build on macOS and there's no panel, so use **mock mode**:

```bash
uv sync                       # core deps only (hardware group skipped)
INKY_MOCK=1 MQTT_ENABLED=0 OUTPUT_DIR=./data \
  uv run uvicorn app.main:app --reload --port 8080

curl -F file=@some.jpg "http://localhost:8080/display/image?show=true"
open data/latest.png          # what would have been shown
```

`INKY_MOCK=1` skips the `inky`/`gpiod` imports entirely and the button watcher,
and renders to `data/latest.png` instead of the panel. The full dithering pipeline
still runs, so it's the right way to tune look-and-feel.

---

## 10. Dashboards (the stub, and how to fill it in)

File: `app/dashboards.py`. There is one real dashboard (`guest_wifi`); unknown
names raise `NotImplementedError` → HTTP 501. The HTTP seam (`POST
/display/dashboard`), the dither→show pipeline, and the schedule story are already
in place — a renderer just has to **return an RGB `PIL.Image` at `self.size`** (the
active panel's resolution: 800×480 on the 7.3", 1600×1200 on the 13.3") and
everything downstream is shared with photos.

**Make renderers resolution-relative**, since `self.size` now varies by panel. The
`guest_wifi` renderer shows the pattern: author the layout at a 1600×1200 reference
and scale every coordinate/stroke/font uniformly to `self.size` (drawing at the
scaled sizes, so text/QR stay crisp — nothing is resampled). Don't hard-code pixel
positions against one panel.

Pick one strategy:

**A. Headless Chromium screenshot (most flexible).**
Add Playwright, render an HTML page / a Home Assistant Lovelace view / a Grafana
panel to a `self.size` PNG (e.g. 800×480), return it.
- Pros: full HTML/CSS, reuse existing HA/Grafana dashboards, easy to design.
- Cons: heaviest on a Pi (Chromium is big; add `playwright install chromium` +
  system libs to the Dockerfile, or run the renderer on another host).
- Sketch:
  ```python
  # async; call from an executor or make the endpoint await it
  page.set_viewport_size({"width": self.size[0], "height": self.size[1]})
  await page.goto(url, wait_until="networkidle")
  png = await page.screenshot()
  return Image.open(io.BytesIO(png)).convert("RGB")
  ```

**B. Server-side Pillow widgets (lightest).**
Draw weather/calendar/metric boxes directly with `PIL.ImageDraw` + bundled fonts,
pulling data from HA's REST API or MQTT.
- Pros: tiny footprint, total control, fast.
- Cons: you hand-build every layout; no HTML.
- Good fit for e-paper anyway: flat colour blocks dither cleanly.

**C. Pull a ready PNG from HA / Grafana (least logic here).**
Let HA (camera/screenshot integrations) or Grafana's render API produce the PNG;
this service just fetches, dithers, and shows it. Closest to the photo path —
basically `requests.get(url)` then the same pipeline.

**Triggering a dashboard refresh** (any strategy):
- a cron/systemd timer or a loop that calls `POST /display/dashboard`,
- a panel **button** (extend the button → MQTT → HA → `POST` loop, or call the
  renderer directly from `ButtonWatcher`'s callback),
- an HA automation publishing to `inky-frame/command` (§6).

e-paper is reflective and has no backlight, so dashboards should be **high
contrast, few colours, large type**. Avoid gradients and thin grey text — they
dither poorly. Refresh sparingly (~30 s each, and e-paper has finite refresh
cycles); once an hour is plenty for weather/calendar.

---

## 11. Concurrency model & gotchas

- **One refresh at a time.** `DisplayManager._lock` serializes refreshes; a refresh
  runs in a worker thread (`asyncio.to_thread`) so the event loop stays responsive.
  A second image sent during a refresh queues behind the lock.
- **The library is thread-safe, not async.** `PhotoLibrary` guards its index with a
  plain `threading.Lock` and every caller reaches it through `asyncio.to_thread`.
  Its `_write_render` reads the same mode-"P" image a background refresh may be
  handing to `inky.set_image()` — both are reads of an already-loaded image, which
  is safe; don't add anything there that mutates it.
- **Buttons are independent.** `ButtonWatcher` runs in its own daemon thread and
  uses different GPIO lines than the display, so it never blocks or conflicts with
  a refresh. gpiod allows multiple line requests on the same chip.
- **Background vs blocking.** With `wait=false` the endpoint returns before the
  refresh finishes; failures surface via logs and `GET /status.last_error`, not in
  the HTTP response. Use `wait=true` if the caller must know the outcome.
- **Two scheduling policies, on purpose.** `show_image(coalesce=False)` — every HTTP
  post — queues behind whatever is running, so every photo you send is eventually
  shown. `coalesce=True` — every MQTT command — is latest-wins
  (`DisplayManager._queue_latest`): a new submission *replaces* the one still
  waiting. Five taps on "Next photo" in HA would otherwise be two and a half minutes
  of refreshes crawling through five images; instead the panel lands on the fifth.
  The preview is still written for each submission, so `latest.png` matches the last
  one — which is also the one that reaches the panel.
- **MQTT callbacks arrive on paho's network thread.** `main.py`'s `_handle_command`
  only does `asyncio.run_coroutine_threadsafe` onto the loop captured in the
  lifespan; the panel is never touched from that thread. `DisplayManager` fires
  `on_state_change` from its *refresh worker* thread, so the MQTT publish it triggers
  must stay thread-safe (paho's `publish` is).
- **Don't add uvicorn workers.** Multiple workers = multiple processes fighting
  over SPI/GPIO. Keep it at one.

---

## 12. Troubleshooting

| Symptom | Likely cause / fix |
|---|---|
| `RuntimeError: No EEPROM detected` at startup | I2C not enabled or `/dev/i2c-1` not mapped. Enable I2C (§7); the code also falls back to constructing the `PANEL` spec's driver (`InkyE673` / `InkyEL133UF1`) directly. |
| Image shows but looks noisy/banded | Double dithering — confirm the §3 "P" handoff is intact (don't pass RGB to `inky.set_image`). |
| Faint traces of the previous image remain (even after clear) | **Ghosting / image retention** — normal on 6-colour Spectra, worse when cold or after a long-static image. A plain `POST /display/clear` is a single white flush; for a deep de-ghost run `POST /display/clear?cycles=N&delay=S` — flashes black↔white `N` times then passes through all 6 colours (`S`s rest between flushes), ending white (`DisplayManager.clear`; blocks a few min). Repeat if a faint ghost persists (edge/perimeter retention is the hardest to fully clear). |
| Renders a preview but the panel never updates; `GET /status.last_error` shows `SystemExit: Woah there … Chip Select: (line 8, GPIO8) currently claimed by spi0 CS0` | Host missing `dtoverlay=spi0-0cs` — inky needs the CS GPIO(s) freed from the kernel to drive them via gpiod. Add it to `/boot/firmware/config.txt`, drop `/dev/spidev0.1` from the compose `devices:` list, reboot (§7 step 1b). Common after an OS re-image. |
| Display refresh hangs or errors mid-transfer | `spidev.bufsiz=65536` missing from `cmdline.txt` (§7). |
| Button C does nothing / interferes with display | Wrong GPIO for the panel: C is GPIO **16** on the 7.3" but GPIO **25** on the 13.3" (GPIO 16 = CS1 there). It's derived from `PANEL`; check `PANEL` matches the wired panel, or set `BUTTON_GPIO_C` (§2). |
| A long press on the photo pops iOS's "Save Image" sheet instead of comparing | `-webkit-touch-callout: none` missing from the `img` rule in `app/gallery.py` (and the sheet's `contextmenu` handler for desktop/Android). |
| Photo comes out sideways / squashed after re-hanging the frame | The **Mounting** setting still says the old orientation. Change it in HA (or `PATCH /prefs {"orientation":"portrait"}`) — that also re-shows the current photo (§4). |
| Portrait photos arrive hard-cropped | `fit` is pinned to `cover`. Set it to `auto` so cover is only used when it wouldn't crop more than `auto_fit_max_crop` (§4). Note the iOS Shortcut sends an explicit `fit=` and overrides the device setting. |
| Thumbnails sideways / the frame reads rotated in HA | Renders are the panel buffer; the *viewing* rotation lives in the thumbnail and in `GET /display/preview?view=true` (§4). If something shows a portrait mount sideways, it is fetching the raw preview. |
| "Inky Frame" makes a SECOND device in HA | The integration's `DeviceInfo` identifier must be `("mqtt", <device id>)`, not `("inky_frame", …)` — that is how the MQTT integration namespaces it (§6). |
| The sidebar panel is blank / the gallery won't load in it | Mixed content: an `http://` gallery URL inside an HTTPS Home Assistant. Set the HTTPS address in Settings → Devices & services → Inky Frame → **Configure** (§6). |
| HA shows the frame device but the entities are `unavailable` | The service isn't publishing `online` to `inky-frame/availability` — check `GET /status.mqtt_connected` and the broker creds. |
| HA's "Current frame" image is stale or broken | `PUBLIC_BASE_URL` autodetected an address HA can't reach (or the frame moved behind a proxy). Set it explicitly; `GET /status.device.base_url` shows what it resolved to. |
| `data/` grows without bound / SD card filling up | The photo library keeps every photo sent (§4). Lower `LIBRARY_MAX_ITEMS` (default 300, ~400 MB on the 13.3") or `LIBRARY_ORIGINAL_MAX_SCALE`, or set `LIBRARY_ENABLED=0`. `GET /status.library.bytes_on_disk` reports the current total. |
| `gpiod unavailable — buttons disabled` in logs | `hardware` group not installed (running core-only image) or not on a Pi. Expected in mock/dev. |
| `Permission denied` on `/dev/gpiochip0` etc. | Device not mapped in compose, wrong gpiochip name, or use `privileged: true` (§7). |
| Buttons never reach HA | Check `GET /status.mqtt_connected`, broker creds in `.env`, and that the broker host is reachable from the container. |
| Colours off | Try a measured `DITHER_PALETTE`, adjust `DITHER_*` tone knobs and `INKY_SATURATION` (§3). |

---

## 13. Project layout

```
app/
  main.py        FastAPI app, routes, lifespan wiring
  config.py      Settings (pydantic-settings) <- env / .env; panel-derived defaults
  panels.py      PanelSpec registry + PANEL selection (7.3" / 13.3"), §1
  prefs.py       Prefs: runtime device settings + the fit=auto rule (§4)
  display.py     DisplayManager: driver init, serialized refresh, preview
  dithering.py   photo -> 6-colour "P" image (the §3 pipeline)
  gallery.py     the gallery at GET / and /gallery: tiles, collections,
                 the photo sheet, upload (§4)
  library.py     PhotoLibrary: photos, their renders, collections (§4)
  gpio_compat.py in-container gpiochip fallback + the BUSY-poll refresh fix (§12)
  buttons.py     ButtonWatcher: gpiod edge loop -> on_press callback
  mqtt.py        MqttBridge: state/prefs publish, commands in, HA discovery (§6)
  shortcut.py    generates the unsigned iOS .shortcut plist (§5)
custom_components/inky_frame/   the Home Assistant integration — every entity for the
                 frame, the media_player, and the sidebar panel (§6). At the repo ROOT
                 because that is where HACS looks. NOT deployed by mutagen; see §6.
  panel/inky-frame-panel.js   the web component the sidebar panel loads
hacs.json         HACS metadata for this repo
.github/workflows/validate.yml   hassfest + HACS validation
  dashboards.py  DashboardRenderer: guest_wifi (resolution-relative; see §10)
Dockerfile        python:3.12-slim + uv, installs the hardware group
docker-compose.yml Pi 5 device mappings + env
.env.example      all settings, documented
test_main.http    scratch API requests
```

---

## 14. Sources

- Inky library + Spectra drivers: <https://github.com/pimoroni/inky>
  (`inky/inky_el133uf1.py` for the 13.3", `inky/inky_e673.py` for the 7.3",
  `examples/spectra6/buttons.py`). Both drivers share the same `_busy_wait` and
  DESATURATED_PALETTE order.
- Getting started: <https://learn.pimoroni.com/article/getting-started-with-inky-impression>
- Product (13.3", 1600×1200, 4 buttons): <https://shop.pimoroni.com/products/inky-impression-13-3>
- Product (7.3", 800×480, 4 buttons): <https://shop.pimoroni.com/products/inky-impression-7-3>
- `epaper-dithering`: <https://github.com/OpenDisplay-org/epaper-dithering> ·
  <https://pypi.org/project/epaper-dithering/>
- Pi 5 GPIO in Docker: <https://github.com/gpiozero/gpiozero/discussions/1117>
- Home Assistant MQTT device triggers:
  <https://www.home-assistant.io/integrations/device_trigger.mqtt/>
