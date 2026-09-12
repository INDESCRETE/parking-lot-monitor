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
