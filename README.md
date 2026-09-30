# Parking Lot Monitor POC

Local proof-of-concept harness for annotating parking lot spaces from static camera images.

## Run

```bash
python3 -m app.server
```

Then open:

```text
http://127.0.0.1:8000
```

## First POC Flow

1. Add images to `data/images/camera_1/`, upload them from the UI, or import a video (below).
2. Select an image in the browser.
3. Click **Mark Space**.
4. Click the four corners of one parking space.
5. Name and save the space.

Saved spaces are persisted in SQLite at `data/db/parking_lot.sqlite` and exported as JSON through `/api/config/camera_1`.

## Import a Video

The sidebar has an **Import Video** panel that turns a video clip into a set of still frames for the selected camera, right from the browser:

1. Choose a video file (`.mp4`, `.mov`, `.m4v`, `.webm`, `.avi`, `.mkv`).
2. Set **Every N sec** for how often to grab a frame.
3. Optionally set **Stop at** (`mm:ss` or plain seconds) to only extract up to that point in the clip; leave it blank to use the whole video.
4. Click **Extract Frames**. The server runs `ffmpeg` on the upload and adds the resulting frames straight into `data/images/<camera_id>/`.

This needs `ffmpeg` available to the server process, via the `imageio-ffmpeg` package:

```bash
pip install imageio-ffmpeg
```

The core server still runs with zero dependencies otherwise — this is only needed if you want video import. If it's missing, the **Extract Frames** button returns a clear error instead of crashing the server.

The uploaded source video itself is kept at `data/source_videos/<camera_id>/` (gitignored) in case you want to re-run extraction at a different interval later.

Only use footage you own or otherwise have the rights to process — this feature works on a video file you already have, it does not fetch or download video from anywhere.

## Run Occupancy Detection

Detection runs on [RF-DETR](https://github.com/roboflow/rf-detr) (Apache-2.0 licensed, so it's safe to ship in a paid product — see the licensing note below). Install dependencies in a local virtualenv, then run detection for a camera:

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python scripts/detect_occupancy.py --camera-id camera_1
```

Pass `--model-size {nano,small,medium,large}` to trade off speed vs. accuracy (default: `medium`). The first run downloads pretrained COCO weights, plus PyTorch/torchvision/transformers if they aren't already installed — expect a larger download than a typical pip install.

The detector writes raw vehicle boxes to `detections` and per-space occupied/empty decisions to `occupancy_observations`. The UI will show vehicle boxes and tint marked spaces after detection results exist for the selected image.

Occupancy is assigned using a vehicle ground-anchor point near the bottom-center of each detection box. Each detection can occupy at most one marked space, which avoids marking a neighboring space occupied just because a car visually overlaps it in the 2D image.

### Why RF-DETR and not YOLOv8

`ultralytics` (YOLOv8/YOLO11/etc.) is licensed **AGPL-3.0**, which applies even to internal or SaaS use — not just redistribution. Selling this as a product, or running it as a service for a customer, would legally require either open-sourcing the whole application or paying Ultralytics for an Enterprise license. RF-DETR is **Apache-2.0**: free to use in a closed-source, paid product with no revenue threshold and no source-disclosure obligation. (This isn't legal advice — verify current license terms directly before shipping commercially.)

## Live Traffic Counting

Each camera is either a **Parking** camera (mark spaces, see which are occupied) or a **Traffic** camera (draw lines, count vehicles crossing them). Pick the type in **+ Add Camera**. Traffic cameras live on their own page, `/traffic`.

1. Give the camera a video-stream entry in `data/camera_sources.json` with `"type": "rtsp"`, `"rtsp_decode": "all"` and `"interval_seconds": 0.2` (5 pictures a second). See `reolink_traffic_counting` in `camera_sources.example.json`. One physical camera can feed both a parking camera and a traffic camera.
2. On `/traffic`, press **Draw Line (L)** and click two points across the road or a lot's entrance/exit. Name the line and its two directions (e.g. Northbound / Southbound). Drag a line's end dots to move it.
3. Each tracked vehicle is counted once per line when the bottom-middle of its box crosses it. Counts appear live, are saved in `line_crossings`, and download as a CSV of 5/15/30/60-minute intervals per direction.

The tracker is `app/tracking.py` (shared with `scripts/count_traffic.py` for recorded video); the live worker is `app/live_traffic.py`; tables and count summaries are in `app/traffic_store.py`.

## Project Shape

```text
app/
  server.py        # stdlib HTTP + SQLite backend
static/
  index.html       # annotation UI
  styles.css
  app.js
data/
  images/
    camera_1/      # static image set for the POC
  db/
    parking_lot.sqlite
```
