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

## Run YOLO Occupancy Detection

Install dependencies in a local virtualenv, then run detection for a camera:

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python scripts/detect_occupancy.py --camera-id camera_1
```

The detector writes raw YOLO vehicle boxes to `detections` and per-space occupied/empty decisions to `occupancy_observations`. The UI will show vehicle boxes and tint marked spaces after detection results exist for the selected image.

Occupancy is assigned using a vehicle ground-anchor point near the bottom-center of each YOLO box. Each detection can occupy at most one marked space, which avoids marking a neighboring space occupied just because a car visually overlaps it in the 2D image.

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
