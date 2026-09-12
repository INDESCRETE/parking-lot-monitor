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

1. Add images to `data/images/camera_1/`, or upload them from the UI.
2. Select an image in the browser.
3. Click **Mark Space**.
4. Click the four corners of one parking space.
5. Name and save the space.

Saved spaces are persisted in SQLite at `data/db/parking_lot.sqlite` and exported as JSON through `/api/config/camera_1`.

## Run YOLO Occupancy Detection

Install dependencies in a local virtualenv, then run detection for a camera:

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python scripts/detect_occupancy.py --camera-id camera_1
```

The detector writes raw YOLO vehicle boxes to `detections` and per-space occupied/empty decisions to `occupancy_observations`. The UI will show vehicle boxes and tint marked spaces after detection results exist for the selected image.

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
