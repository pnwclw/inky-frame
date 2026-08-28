# Inky Frame Dashboard

Small API service for a **Pimoroni Inky Impression** e-paper frame on a Raspberry
Pi 5 — supports both the **7.3"** (800×480, the default) and the **13.3"**
(1600×1200); pick one with `PANEL` in `.env`:

- accepts an image (from an **iOS Shortcut** or anything that can POST),
- dithers it with [`epaper-dithering`](https://github.com/OpenDisplay-org/epaper-dithering),
  and shows it on the panel,
- keeps every photo in an on-disk **library** so it can be shown again
  (`GET /library`, `POST /display/library/<id>`, `POST /display/nav`),
- serves a **gallery** at `/` shaped like a phone photo app — a tile grid, collections
  a photo can be in several of, and a sheet where you pick the fit, rotation and
  dithering before putting it on the frame; uploads are filed, not rendered,
- ships a **Home Assistant integration** (`homeassistant/custom_components/inky_frame`)
  that is the whole device in HA: status, the current frame, the settings below, the
  panel buttons, next/previous/random — plus a `media_player` that browses the library
  with thumbnails and puts "Play on Inky Frame" on every HA media source,
- uses **MQTT as transport only** (push state + physical button presses); entities come
  from the integration, so there is one place describing the device,
- has **device settings** (how the frame is mounted, how to place a photo) that are
  edited from HA and persist: `fit=auto` uses `cover` only while it wouldn't crop
  too much, so a portrait photo on a landscape frame is padded, not butchered,
- leaves a **dashboard** seam for later.

Runs via `docker compose` (with `uv` inside the image).

## Quick start (on the Pi)

```bash
cp .env.example .env        # set MQTT_HOST / MQTT_USERNAME / MQTT_PASSWORD
docker compose up -d --build
curl -F file=@photo.jpg http://localhost:8080/display/image
```

Then on your iPhone open `http://<pi-host>:8080/` — it serves a generated iOS
Shortcut that sends photos to the frame (asks landscape/portrait + cover/fit).
See [CLAUDE.md §5](CLAUDE.md) for the one-time "Allow Untrusted Shortcuts" step.

## Local dev (no hardware, e.g. macOS)

```bash
uv sync                     # core deps only (no inky/gpiod)
INKY_MOCK=1 uv run uvicorn app.main:app --reload --port 8080
# renders to data/latest.png instead of the panel
```

📖 **All the project knowledge — hardware facts, the dithering pipeline, MQTT /
Home Assistant wiring, the Pi host setup, the iOS Shortcut, and how to build the
dashboards — lives in [CLAUDE.md](CLAUDE.md). Read it first.**
