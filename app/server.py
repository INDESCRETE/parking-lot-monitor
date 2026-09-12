from __future__ import annotations

import cgi
import errno
import json
import mimetypes
import os
import re
import shutil
import sqlite3
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse


ROOT = Path(__file__).resolve().parents[1]
STATIC_DIR = ROOT / "static"
DATA_DIR = ROOT / "data"
IMAGES_DIR = DATA_DIR / "images"
DB_PATH = DATA_DIR / "db" / "parking_lot.sqlite"
DEFAULT_CAMERA_ID = "camera_1"
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def safe_segment(value: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9_.-]+", "_", value.strip())
    return cleaned.strip("._") or "item"


def parse_timestamp_from_filename(path: Path) -> str:
    match = re.search(
        r"(\d{4})[-_]?(\d{2})[-_]?(\d{2})[T _-]?(\d{2})[-_]?(\d{2})[-_]?(\d{2})",
        path.stem,
    )
    if match:
        year, month, day, hour, minute, second = match.groups()
        return f"{year}-{month}-{day}T{hour}:{minute}:{second}+00:00"
    return datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat()


class Database:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.init()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn

    def init(self) -> None:
        with self.connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS cameras (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS images (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    camera_id TEXT NOT NULL,
                    filename TEXT NOT NULL,
                    captured_at TEXT NOT NULL,
                    width INTEGER,
                    height INTEGER,
                    created_at TEXT NOT NULL,
                    UNIQUE(camera_id, filename),
                    FOREIGN KEY(camera_id) REFERENCES cameras(id)
                );

                CREATE TABLE IF NOT EXISTS parking_spaces (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    camera_id TEXT NOT NULL,
                    label TEXT NOT NULL,
                    polygon_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(camera_id) REFERENCES cameras(id)
                );

                CREATE TABLE IF NOT EXISTS detections (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    image_id INTEGER NOT NULL,
                    model_name TEXT NOT NULL,
                    class_id INTEGER NOT NULL,
                    class_name TEXT NOT NULL,
                    confidence REAL NOT NULL,
                    bbox_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(image_id) REFERENCES images(id)
                );

                CREATE TABLE IF NOT EXISTS occupancy_observations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    image_id INTEGER NOT NULL,
                    parking_space_id INTEGER NOT NULL,
                    occupied INTEGER NOT NULL,
                    score REAL NOT NULL,
                    detection_id INTEGER,
                    created_at TEXT NOT NULL,
                    UNIQUE(image_id, parking_space_id),
                    FOREIGN KEY(image_id) REFERENCES images(id),
                    FOREIGN KEY(parking_space_id) REFERENCES parking_spaces(id),
                    FOREIGN KEY(detection_id) REFERENCES detections(id)
                );
                """
            )
            conn.execute(
                "INSERT OR IGNORE INTO cameras (id, name, created_at) VALUES (?, ?, ?)",
                (DEFAULT_CAMERA_ID, "Camera 1", utc_now()),
            )

    def sync_images(self) -> None:
        with self.connect() as conn:
            for camera_dir in IMAGES_DIR.iterdir() if IMAGES_DIR.exists() else []:
                if not camera_dir.is_dir():
                    continue
                camera_id = safe_segment(camera_dir.name)
                filenames = {
                    path.name
                    for path in camera_dir.iterdir()
                    if path.suffix.lower() in IMAGE_EXTENSIONS
                }
                conn.execute(
                    "INSERT OR IGNORE INTO cameras (id, name, created_at) VALUES (?, ?, ?)",
                    (camera_id, camera_id.replace("_", " ").title(), utc_now()),
                )
                if filenames:
                    placeholders = ",".join("?" for _ in filenames)
                    conn.execute(
                        f"""
                        DELETE FROM images
                        WHERE camera_id = ?
                          AND filename NOT IN ({placeholders})
                        """,
                        (camera_id, *filenames),
                    )
                else:
                    conn.execute("DELETE FROM images WHERE camera_id = ?", (camera_id,))
                for path in sorted(camera_dir.iterdir()):
                    if path.suffix.lower() not in IMAGE_EXTENSIONS:
                        continue
                    conn.execute(
                        """
                        INSERT OR IGNORE INTO images
                            (camera_id, filename, captured_at, created_at)
                        VALUES (?, ?, ?, ?)
                        """,
                        (camera_id, path.name, parse_timestamp_from_filename(path), utc_now()),
                    )

    def list_cameras(self) -> list[dict[str, Any]]:
        with self.connect() as conn:
            return [dict(row) for row in conn.execute("SELECT * FROM cameras ORDER BY id")]

    def list_images(self, camera_id: str) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT id, camera_id, filename, captured_at, width, height
                FROM images
                WHERE camera_id = ?
                ORDER BY captured_at, filename
                """,
                (camera_id,),
            )
            images = []
            for row in rows:
                item = dict(row)
                item["url"] = f"/media/{camera_id}/{item['filename']}"
                images.append(item)
            return images

    def list_spaces(self, camera_id: str) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT id, camera_id, label, polygon_json, created_at, updated_at
                FROM parking_spaces
                WHERE camera_id = ?
                ORDER BY id
                """,
                (camera_id,),
            )
            return [space_from_row(row) for row in rows]

    def get_image_observations(self, image_id: int) -> dict[str, Any]:
        with self.connect() as conn:
            detections = [
                detection_from_row(row)
                for row in conn.execute(
                    """
                    SELECT id, image_id, model_name, class_id, class_name, confidence, bbox_json
                    FROM detections
                    WHERE image_id = ?
                    ORDER BY confidence DESC
                    """,
                    (image_id,),
                )
            ]
            occupancy = [
                {
                    "space_id": row["parking_space_id"],
                    "occupied": bool(row["occupied"]),
                    "score": row["score"],
                    "detection_id": row["detection_id"],
                }
                for row in conn.execute(
                    """
                    SELECT parking_space_id, occupied, score, detection_id
                    FROM occupancy_observations
                    WHERE image_id = ?
                    ORDER BY parking_space_id
                    """,
                    (image_id,),
                )
            ]
            return {"image_id": image_id, "detections": detections, "occupancy": occupancy}

    def create_space(self, camera_id: str, label: str, polygon: list[dict[str, float]]) -> dict[str, Any]:
        now = utc_now()
        with self.connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO parking_spaces
                    (camera_id, label, polygon_json, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (camera_id, label, json.dumps(polygon), now, now),
            )
            row = conn.execute(
                "SELECT * FROM parking_spaces WHERE id = ?",
                (cursor.lastrowid,),
            ).fetchone()
            return space_from_row(row)

    def delete_space(self, space_id: int) -> bool:
        with self.connect() as conn:
            cursor = conn.execute("DELETE FROM parking_spaces WHERE id = ?", (space_id,))
            return cursor.rowcount > 0

    def add_uploaded_image(self, camera_id: str, filename: str, source: Any) -> dict[str, Any]:
        camera_id = safe_segment(camera_id)
        image_dir = IMAGES_DIR / camera_id
        image_dir.mkdir(parents=True, exist_ok=True)
        safe_name = safe_segment(Path(filename).stem) + Path(filename).suffix.lower()
        if Path(safe_name).suffix.lower() not in IMAGE_EXTENSIONS:
            raise ValueError("Only jpg, jpeg, png, and webp images are supported.")

        target = image_dir / safe_name
        counter = 1
        while target.exists():
            target = image_dir / f"{Path(safe_name).stem}_{counter}{Path(safe_name).suffix}"
            counter += 1

        with target.open("wb") as handle:
            shutil.copyfileobj(source, handle)

        with self.connect() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO cameras (id, name, created_at) VALUES (?, ?, ?)",
                (camera_id, camera_id.replace("_", " ").title(), utc_now()),
            )
            cursor = conn.execute(
                """
                INSERT INTO images (camera_id, filename, captured_at, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (camera_id, target.name, parse_timestamp_from_filename(target), utc_now()),
            )
            row = conn.execute("SELECT * FROM images WHERE id = ?", (cursor.lastrowid,)).fetchone()
            item = dict(row)
            item["url"] = f"/media/{camera_id}/{item['filename']}"
            return item


def space_from_row(row: sqlite3.Row) -> dict[str, Any]:
    item = dict(row)
    item["polygon"] = json.loads(item.pop("polygon_json"))
    return item


def detection_from_row(row: sqlite3.Row) -> dict[str, Any]:
    item = dict(row)
    item["bbox"] = json.loads(item.pop("bbox_json"))
    return item


db = Database(DB_PATH)


class Handler(BaseHTTPRequestHandler):
    server_version = "ParkingLotPOC/0.1"

    def do_GET(self) -> None:
        db.sync_images()
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)

        if path == "/":
            self.serve_file(STATIC_DIR / "index.html")
        elif path.startswith("/static/"):
            self.serve_file(STATIC_DIR / path.removeprefix("/static/"))
        elif path.startswith("/media/"):
            self.serve_media(path)
        elif path == "/api/health":
            self.send_json({"ok": True})
        elif path == "/api/cameras":
            self.send_json({"cameras": db.list_cameras()})
        elif path == "/api/images":
            camera_id = query.get("camera_id", [DEFAULT_CAMERA_ID])[0]
            self.send_json({"images": db.list_images(camera_id)})
        elif path == "/api/spaces":
            camera_id = query.get("camera_id", [DEFAULT_CAMERA_ID])[0]
            self.send_json({"spaces": db.list_spaces(camera_id)})
        elif path == "/api/observations":
            try:
                image_id = int(query.get("image_id", ["0"])[0])
            except ValueError:
                self.send_json({"error": "image_id must be an integer"}, HTTPStatus.BAD_REQUEST)
                return
            self.send_json(db.get_image_observations(image_id))
        elif path.startswith("/api/config/"):
            camera_id = safe_segment(unquote(path.removeprefix("/api/config/")))
            self.send_json({"camera_id": camera_id, "spaces": db.list_spaces(camera_id)})
        else:
            self.send_error(HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/api/spaces":
            payload = self.read_json()
            try:
                polygon = payload["polygon"]
                if len(polygon) != 4:
                    raise ValueError("A parking space needs exactly 4 points.")
                space = db.create_space(
                    safe_segment(payload.get("camera_id", DEFAULT_CAMERA_ID)),
                    payload.get("label") or "Space",
                    polygon,
                )
            except (KeyError, TypeError, ValueError) as exc:
                self.send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
                return
            self.send_json({"space": space}, HTTPStatus.CREATED)
        elif parsed.path == "/api/images":
            self.handle_upload()
        else:
            self.send_error(HTTPStatus.NOT_FOUND)

    def do_DELETE(self) -> None:
        parsed = urlparse(self.path)
        match = re.fullmatch(r"/api/spaces/(\d+)", parsed.path)
        if not match:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        deleted = db.delete_space(int(match.group(1)))
        if not deleted:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        self.send_json({"deleted": True})

    def handle_upload(self) -> None:
        form = cgi.FieldStorage(
            fp=self.rfile,
            headers=self.headers,
            environ={
                "REQUEST_METHOD": "POST",
                "CONTENT_TYPE": self.headers.get("Content-Type", ""),
            },
        )
        image_field = form["image"] if "image" in form else None
        if image_field is None or not image_field.filename:
            self.send_json({"error": "Upload requires an image field."}, HTTPStatus.BAD_REQUEST)
            return
        camera_id = form.getfirst("camera_id", DEFAULT_CAMERA_ID)
        try:
            image = db.add_uploaded_image(camera_id, image_field.filename, image_field.file)
        except ValueError as exc:
            self.send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
            return
        self.send_json({"image": image}, HTTPStatus.CREATED)

    def read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length)
        return json.loads(raw.decode("utf-8"))

    def send_json(self, payload: dict[str, Any], status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(payload, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def serve_file(self, path: Path) -> None:
        if not path.exists() or not path.is_file():
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        body = path.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def serve_media(self, path: str) -> None:
        parts = [safe_segment(unquote(part)) for part in path.removeprefix("/media/").split("/")]
        if len(parts) != 2:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        camera_id, filename = parts
        self.serve_file(IMAGES_DIR / camera_id / filename)


def main() -> None:
    DATA_DIR.mkdir(exist_ok=True)
    (DATA_DIR / "db").mkdir(parents=True, exist_ok=True)
    (IMAGES_DIR / DEFAULT_CAMERA_ID).mkdir(parents=True, exist_ok=True)
    db.sync_images()
    host = "127.0.0.1"
    starting_port = int(os.environ.get("PORT", "8000"))
    server = None
    for port in range(starting_port, starting_port + 20):
        try:
            server = ThreadingHTTPServer((host, port), Handler)
            break
        except OSError as exc:
            if exc.errno != errno.EADDRINUSE:
                raise
    if server is None:
        raise RuntimeError(f"No open port found from {starting_port} to {starting_port + 19}")

    address = server.server_address
    print(f"Parking Lot Monitor POC running at http://{address[0]}:{address[1]}")
    server.serve_forever()


if __name__ == "__main__":
    main()
