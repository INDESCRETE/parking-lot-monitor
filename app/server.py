from __future__ import annotations

import cgi
import errno
import json
import mimetypes
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import threading
from datetime import datetime, timedelta, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from app import analytics, reports
from app.video_extract import FfmpegUnavailable, extract_frames


ROOT = Path(__file__).resolve().parents[1]
STATIC_DIR = ROOT / "static"
DATA_DIR = ROOT / "data"
IMAGES_DIR = DATA_DIR / "images"
SOURCE_VIDEOS_DIR = DATA_DIR / "source_videos"
DB_PATH = DATA_DIR / "db" / "parking_lot.sqlite"
DEFAULT_CAMERA_ID = "camera_1"
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}
VIDEO_EXTENSIONS = {".mp4", ".mov", ".m4v", ".webm", ".avi", ".mkv"}
MAX_VIDEO_UPLOAD_BYTES = 2 * 1024 * 1024 * 1024  # 2 GiB safety cap
# Sentinel distinguishing "this optional argument wasn't passed at all" from
# an explicit `None`, used where `None` is itself a meaningful value (e.g.
# Database.update_camera's min_occupied_seconds, where None means "clear the
# per-camera override").
_UNSET = object()

DETECT_SCRIPT_PATH = ROOT / "scripts" / "detect_occupancy.py"
DETECTION_TIMEOUT_SECONDS = 30 * 60  # generous ceiling for a large batch on CPU


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def safe_segment(value: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9_.-]+", "_", value.strip())
    return cleaned.strip("._") or "item"


def parse_range_bound(value: str, *, inclusive_end: bool) -> datetime:
    """Parse a `start`/`end` query param. A bare date ("2026-01-07") used as
    `end` is treated as inclusive of that whole day (i.e. bumped to the next
    day's midnight, the exclusive upper bound); a full timestamp is used as-is."""
    value = value.strip()
    is_bare_date = len(value) == 10 and value.count("-") == 2 and "T" not in value
    # datetime.fromisoformat() only accepts a trailing "Z" (as produced by
    # JS's Date.toISOString(), which the dashboard sends) on Python 3.11+;
    # normalize it to an explicit offset so this works on older Pythons too.
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    if is_bare_date and inclusive_end:
        dt = dt + timedelta(days=1)
    return dt


def parse_report_range(query: dict[str, list[str]]) -> tuple[datetime, datetime]:
    """Defaults to the trailing 7 days ending now when start/end are omitted."""
    now = datetime.now(timezone.utc)
    end_raw = query.get("end", [None])[0]
    start_raw = query.get("start", [None])[0]
    end = parse_range_bound(end_raw, inclusive_end=True) if end_raw else now
    start = parse_range_bound(start_raw, inclusive_end=False) if start_raw else end - timedelta(days=7)
    if end <= start:
        raise ValueError("end must be after start")
    return start, end


# --- Background detection ---
#
# scripts/detect_occupancy.py is a slow, synchronous batch job (it loads an
# RF-DETR model and runs inference over every image for one camera), so it
# can't run inline inside an HTTP request handler without blocking uploads
# for minutes. Instead, a video/image upload kicks it off on a background
# thread and returns immediately; the frontend polls a status endpoint and
# refreshes itself once the run finishes. `_detection_state` is in-memory
# only (not persisted) — it describes "is a job running right now", which
# is inherently about this process's lifetime, not durable data.
_detection_lock = threading.Lock()
_detection_state: dict[str, dict[str, Any]] = {}
# Matches the "PROGRESS <done> <total>" line scripts/detect_occupancy.py
# prints (flushed) after each image, so this worker can turn it into a live
# progress indicator instead of only knowing "running" vs "not running".
_PROGRESS_RE = re.compile(r"^PROGRESS (\d+) (\d+)$")
# Matches the "PHASE <name>" line the script prints once it moves from
# per-image detection (where PROGRESS applies) into rebuilding occupancy
# timelines from the results -- a step with no per-item progress of its own,
# but one that can take a real few seconds for a lot with many spaces. Without
# this, the UI has nothing to show once PROGRESS hits done==total except a
# stale "100%" for however long that rebuild takes, which looks stuck/finished
# when it isn't -- this is what Rob saw and asked about.
_PHASE_RE = re.compile(r"^PHASE (\w+)$")


def trigger_detection(camera_id: str) -> None:
    """Starts a background detection run for one camera, or — if a run for
    that camera is already in progress — marks that it needs to run again
    once the current pass finishes, rather than starting a second
    overlapping pass. Two RF-DETR runs for the same camera at once would
    both rebuild the same space_state_intervals rows from scratch and race
    each other, so this coalesces bursts (e.g. importing several videos back
    to back) into one rerun instead of many concurrent ones.
    """
    with _detection_lock:
        state = _detection_state.setdefault(camera_id, {"status": "idle"})
        if state.get("status") == "running":
            state["pending_rerun"] = True
            return
        state.update(
            {
                "status": "running",
                "started_at": utc_now(),
                "finished_at": None,
                "error": None,
                "pending_rerun": False,
                "progress": None,
                "phase": None,
            }
        )
    thread = threading.Thread(target=_run_detection_worker, args=(camera_id,), daemon=True)
    thread.start()


def _set_detection_progress(camera_id: str, done: int, total: int) -> None:
    with _detection_lock:
        state = _detection_state.setdefault(camera_id, {})
        # A rerun coalesced while this line was in flight (see
        # trigger_detection) has already reset status away from "running";
        # don't resurrect a progress reading from the run that's finishing.
        if state.get("status") == "running":
            state["progress"] = {"done": done, "total": total}


def _set_detection_phase(camera_id: str, phase: str) -> None:
    with _detection_lock:
        state = _detection_state.setdefault(camera_id, {})
        if state.get("status") == "running":
            state["phase"] = phase


def _run_detection_worker(camera_id: str) -> None:
    while True:
        error: str | None = None
        try:
            # sys.executable: the exact interpreter already running this
            # server, so this automatically uses whatever venv (with
            # RF-DETR installed) the server itself was started with —
            # no separate environment to keep in sync.
            command = [sys.executable, str(DETECT_SCRIPT_PATH), "--camera-id", camera_id]
            # Re-read the camera's own noise-filter override fresh on every
            # run (rather than once outside the loop) so a value Rob just
            # saved from the UI takes effect on the very next run, including
            # an immediate coalesced rerun. None means "no override" -- the
            # script falls back to its own DEFAULT_MIN_OCCUPIED_SECONDS.
            camera = db.get_camera(camera_id)
            if camera is not None and camera.get("min_occupied_seconds") is not None:
                command += ["--min-occupied-seconds", str(camera["min_occupied_seconds"])]

            # Popen + a streamed read loop (rather than subprocess.run's
            # capture_output, which only hands back output once the process
            # has already exited) so progress lines can update the UI while
            # detection is still running, not just after it finishes.
            env = dict(os.environ, PYTHONUNBUFFERED="1")
            proc = subprocess.Popen(
                command,
                cwd=ROOT,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                env=env,
            )
            timed_out = False

            def _on_timeout() -> None:
                nonlocal timed_out
                timed_out = True
                proc.kill()

            timer = threading.Timer(DETECTION_TIMEOUT_SECONDS, _on_timeout)
            timer.start()
            output_lines: list[str] = []
            try:
                assert proc.stdout is not None
                for line in proc.stdout:
                    line = line.rstrip("\n")
                    output_lines.append(line)
                    stripped = line.strip()
                    progress_match = _PROGRESS_RE.match(stripped)
                    phase_match = _PHASE_RE.match(stripped)
                    if progress_match:
                        _set_detection_progress(camera_id, int(progress_match.group(1)), int(progress_match.group(2)))
                    elif phase_match:
                        _set_detection_phase(camera_id, phase_match.group(1))
            finally:
                proc.stdout.close()
                returncode = proc.wait()
                timer.cancel()

            if timed_out:
                error = f"Detection timed out after {DETECTION_TIMEOUT_SECONDS // 60} minutes."
            elif returncode != 0:
                error = "\n".join(output_lines).strip()[-2000:] or "Detection failed."
        except OSError as exc:
            error = str(exc)

        with _detection_lock:
            state = _detection_state.setdefault(camera_id, {})
            state["status"] = "error" if error else "idle"
            state["error"] = error
            state["finished_at"] = utc_now()
            state["progress"] = None
            state["phase"] = None
            rerun = state.pop("pending_rerun", False)
            if rerun:
                state.update(
                    {
                        "status": "running",
                        "started_at": utc_now(),
                        "finished_at": None,
                        "progress": None,
                        "phase": None,
                    }
                )
        if not rerun:
            return


def get_detection_status(camera_id: str) -> dict[str, Any]:
    with _detection_lock:
        state = _detection_state.get(camera_id, {"status": "idle"})
        return {"camera_id": camera_id, **state}


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
                    min_occupied_seconds REAL,
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

                CREATE TABLE IF NOT EXISTS space_state_intervals (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    parking_space_id INTEGER NOT NULL,
                    occupied INTEGER NOT NULL,
                    start_image_id INTEGER NOT NULL,
                    start_captured_at TEXT NOT NULL,
                    end_image_id INTEGER NOT NULL,
                    end_captured_at TEXT NOT NULL,
                    duration_seconds REAL NOT NULL,
                    is_current INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(parking_space_id) REFERENCES parking_spaces(id),
                    FOREIGN KEY(start_image_id) REFERENCES images(id),
                    FOREIGN KEY(end_image_id) REFERENCES images(id)
                );

                CREATE INDEX IF NOT EXISTS idx_intervals_space_current
                    ON space_state_intervals(parking_space_id, is_current);
                CREATE INDEX IF NOT EXISTS idx_intervals_space_start
                    ON space_state_intervals(parking_space_id, start_captured_at);

                CREATE TABLE IF NOT EXISTS clients (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS lots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    client_id INTEGER NOT NULL,
                    name TEXT NOT NULL,
                    address TEXT,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(client_id) REFERENCES clients(id)
                );

                CREATE INDEX IF NOT EXISTS idx_lots_client ON lots(client_id);
                """
            )
            self._migrate_camera_lot_column(conn)
            self._migrate_camera_min_occupied_seconds_column(conn)
            conn.execute(
                "INSERT OR IGNORE INTO cameras (id, name, created_at) VALUES (?, ?, ?)",
                (DEFAULT_CAMERA_ID, "Camera 1", utc_now()),
            )
            self._backfill_unassigned_lot(conn)

    def _migrate_camera_lot_column(self, conn: sqlite3.Connection) -> None:
        """Add cameras.lot_id if this DB predates the clients/lots feature.

        SQLite's CREATE TABLE IF NOT EXISTS doesn't add columns to an
        already-existing table, so an explicit, idempotent migration is
        needed here for anyone upgrading an existing database in place.
        """
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(cameras)")}
        if "lot_id" not in columns:
            conn.execute("ALTER TABLE cameras ADD COLUMN lot_id INTEGER REFERENCES lots(id)")

    def _migrate_camera_min_occupied_seconds_column(self, conn: sqlite3.Connection) -> None:
        """Add cameras.min_occupied_seconds if this DB predates the
        pass-through/noise-filtering feature. NULL means "use
        scripts/detect_occupancy.py's own default (DEFAULT_MIN_OCCUPIED_SECONDS)"
        rather than a fixed number, so existing cameras aren't forced onto a
        stored value that then drifts out of sync with the script's default."""
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(cameras)")}
        if "min_occupied_seconds" not in columns:
            conn.execute("ALTER TABLE cameras ADD COLUMN min_occupied_seconds REAL")

    def _ensure_unassigned_lot(self, conn: sqlite3.Connection) -> int:
        """Returns the id of the well-known "Unassigned" client/lot,
        creating it on first use. Looked up by name, not "first row in the
        table", so this never misattributes a camera to a real client that
        happens to already exist. Shared by the startup backfill,
        sync_images, and any camera created without an explicit lot, so
        every camera always has a lot to report under from the moment it
        exists rather than only after the next server restart.
        """
        now = utc_now()
        client_row = conn.execute(
            "SELECT id FROM clients WHERE name = ?", ("Unassigned",)
        ).fetchone()
        if client_row is None:
            client_id = conn.execute(
                "INSERT INTO clients (name, created_at) VALUES (?, ?)",
                ("Unassigned", now),
            ).lastrowid
        else:
            client_id = client_row["id"]
        lot_row = conn.execute(
            "SELECT id FROM lots WHERE client_id = ? AND name = ?",
            (client_id, "Unassigned Lot"),
        ).fetchone()
        if lot_row is None:
            lot_id = conn.execute(
                "INSERT INTO lots (client_id, name, created_at) VALUES (?, ?, ?)",
                (client_id, "Unassigned Lot", now),
            ).lastrowid
        else:
            lot_id = lot_row["id"]
        return lot_id

    def _backfill_unassigned_lot(self, conn: sqlite3.Connection) -> None:
        """Any camera with no lot yet (pre-existing data, or a brand-new camera
        folder discovered by sync_images) gets parked under the "Unassigned"
        client/lot rather than left NULL, so every camera is always
        reportable under some lot.
        """
        if not conn.execute("SELECT 1 FROM cameras WHERE lot_id IS NULL").fetchone():
            return
        lot_id = self._ensure_unassigned_lot(conn)
        conn.execute("UPDATE cameras SET lot_id = ? WHERE lot_id IS NULL", (lot_id,))

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
            # Any camera folder discovered just now (or from before this
            # feature existed) starts with lot_id NULL; park it under the
            # "Unassigned" client/lot immediately rather than waiting for the
            # next server restart.
            self._backfill_unassigned_lot(conn)

    def list_cameras(self, lot_id: int | None = None) -> list[dict[str, Any]]:
        query = """
            SELECT cameras.*, lots.name AS lot_name, lots.client_id AS client_id,
                   clients.name AS client_name
            FROM cameras
            LEFT JOIN lots ON lots.id = cameras.lot_id
            LEFT JOIN clients ON clients.id = lots.client_id
        """
        params: list[Any] = []
        if lot_id is not None:
            query += " WHERE cameras.lot_id = ?"
            params.append(lot_id)
        query += " ORDER BY cameras.id"
        with self.connect() as conn:
            return [dict(row) for row in conn.execute(query, params)]

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

    def list_clients(self) -> list[dict[str, Any]]:
        with self.connect() as conn:
            return [dict(row) for row in conn.execute("SELECT * FROM clients ORDER BY name")]

    def create_client(self, name: str) -> dict[str, Any]:
        now = utc_now()
        with self.connect() as conn:
            cursor = conn.execute(
                "INSERT INTO clients (name, created_at) VALUES (?, ?)", (name, now)
            )
            row = conn.execute(
                "SELECT * FROM clients WHERE id = ?", (cursor.lastrowid,)
            ).fetchone()
            return dict(row)

    def list_lots(self, client_id: int | None = None) -> list[dict[str, Any]]:
        with self.connect() as conn:
            if client_id is not None:
                rows = conn.execute(
                    """
                    SELECT lots.*, clients.name AS client_name
                    FROM lots JOIN clients ON clients.id = lots.client_id
                    WHERE lots.client_id = ?
                    ORDER BY lots.name
                    """,
                    (client_id,),
                )
            else:
                rows = conn.execute(
                    """
                    SELECT lots.*, clients.name AS client_name
                    FROM lots JOIN clients ON clients.id = lots.client_id
                    ORDER BY clients.name, lots.name
                    """
                )
            return [dict(row) for row in rows]

    def create_lot(self, client_id: int, name: str, address: str | None) -> dict[str, Any]:
        now = utc_now()
        with self.connect() as conn:
            if not conn.execute("SELECT 1 FROM clients WHERE id = ?", (client_id,)).fetchone():
                raise ValueError(f"Client {client_id} does not exist.")
            cursor = conn.execute(
                "INSERT INTO lots (client_id, name, address, created_at) VALUES (?, ?, ?, ?)",
                (client_id, name, address, now),
            )
            row = conn.execute(
                """
                SELECT lots.*, clients.name AS client_name
                FROM lots JOIN clients ON clients.id = lots.client_id
                WHERE lots.id = ?
                """,
                (cursor.lastrowid,),
            ).fetchone()
            return dict(row)

    def update_camera(
        self,
        camera_id: str,
        *,
        lot_id: int | None = None,
        min_occupied_seconds: Any = _UNSET,
    ) -> dict[str, Any] | None:
        """Updates whichever of a camera's settings were actually passed.

        `lot_id`, if given, reassigns the camera to a different lot (must
        exist). `min_occupied_seconds` is tri-state via the `_UNSET`
        sentinel so a caller can distinguish "don't touch this setting"
        (omitted) from "clear it back to the script's own default"
        (explicit `None`) from "set an override" (a number) -- a plain
        default of `None` couldn't tell the second and third cases apart.
        Returns None if the camera doesn't exist.
        """
        with self.connect() as conn:
            if not conn.execute("SELECT 1 FROM cameras WHERE id = ?", (camera_id,)).fetchone():
                return None
            if lot_id is not None:
                if not conn.execute("SELECT 1 FROM lots WHERE id = ?", (lot_id,)).fetchone():
                    raise ValueError(f"Lot {lot_id} does not exist.")
                conn.execute("UPDATE cameras SET lot_id = ? WHERE id = ?", (lot_id, camera_id))
            if min_occupied_seconds is not _UNSET:
                if min_occupied_seconds is not None and min_occupied_seconds < 0:
                    raise ValueError("min_occupied_seconds cannot be negative.")
                conn.execute(
                    "UPDATE cameras SET min_occupied_seconds = ? WHERE id = ?",
                    (min_occupied_seconds, camera_id),
                )
            row = conn.execute(
                """
                SELECT cameras.*, lots.name AS lot_name, lots.client_id AS client_id,
                       clients.name AS client_name
                FROM cameras
                LEFT JOIN lots ON lots.id = cameras.lot_id
                LEFT JOIN clients ON clients.id = lots.client_id
                WHERE cameras.id = ?
                """,
                (camera_id,),
            ).fetchone()
            return dict(row)

    def get_lot(self, lot_id: int) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT lots.*, clients.name AS client_name
                FROM lots JOIN clients ON clients.id = lots.client_id
                WHERE lots.id = ?
                """,
                (lot_id,),
            ).fetchone()
            return dict(row) if row else None

    def get_camera(self, camera_id: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM cameras WHERE id = ?", (camera_id,)).fetchone()
            return dict(row) if row else None

    def list_spaces(self, camera_id: str) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT ps.id, ps.camera_id, ps.label, ps.polygon_json, ps.created_at, ps.updated_at,
                       si.occupied AS current_occupied,
                       si.start_captured_at AS current_since,
                       si.duration_seconds AS current_duration_seconds
                FROM parking_spaces ps
                LEFT JOIN space_state_intervals si
                    ON si.parking_space_id = ps.id AND si.is_current = 1
                WHERE ps.camera_id = ?
                ORDER BY ps.id
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
            occupancy = []
            for row in conn.execute(
                """
                SELECT oo.parking_space_id, oo.occupied, oo.score, oo.detection_id,
                       im.captured_at AS image_captured_at,
                       si.start_captured_at AS since
                FROM occupancy_observations oo
                JOIN images im ON im.id = oo.image_id
                LEFT JOIN space_state_intervals si
                    ON si.parking_space_id = oo.parking_space_id
                   AND si.start_captured_at <= im.captured_at
                   AND si.end_captured_at >= im.captured_at
                WHERE oo.image_id = ?
                ORDER BY oo.parking_space_id
                """,
                (image_id,),
            ):
                duration_seconds = None
                if row["since"]:
                    try:
                        duration_seconds = max(
                            0.0,
                            (
                                datetime.fromisoformat(row["image_captured_at"])
                                - datetime.fromisoformat(row["since"])
                            ).total_seconds(),
                        )
                    except ValueError:
                        duration_seconds = None
                occupancy.append(
                    {
                        "space_id": row["parking_space_id"],
                        "occupied": bool(row["occupied"]),
                        "score": row["score"],
                        "detection_id": row["detection_id"],
                        "since": row["since"],
                        "duration_seconds": duration_seconds,
                    }
                )
            return {"image_id": image_id, "detections": detections, "occupancy": occupancy}

    def get_space_intervals(self, space_id: int, limit: int = 50) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT id, occupied, start_captured_at, end_captured_at, duration_seconds, is_current
                FROM space_state_intervals
                WHERE parking_space_id = ?
                ORDER BY start_captured_at DESC, id DESC
                LIMIT ?
                """,
                (space_id, max(1, min(limit, 500))),
            )
            return [
                {
                    "id": row["id"],
                    "occupied": bool(row["occupied"]),
                    "start_captured_at": row["start_captured_at"],
                    "end_captured_at": row["end_captured_at"],
                    "duration_seconds": row["duration_seconds"],
                    "is_current": bool(row["is_current"]),
                }
                for row in rows
            ]

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

    def update_space_polygon(self, space_id: int, polygon: list[dict[str, float]]) -> dict[str, Any] | None:
        now = utc_now()
        with self.connect() as conn:
            cursor = conn.execute(
                """
                UPDATE parking_spaces
                SET polygon_json = ?, updated_at = ?
                WHERE id = ?
                """,
                (json.dumps(polygon), now, space_id),
            )
            if cursor.rowcount == 0:
                return None
            row = conn.execute(
                "SELECT * FROM parking_spaces WHERE id = ?",
                (space_id,),
            ).fetchone()
            return space_from_row(row)

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
            # INSERT OR IGNORE: if this camera already exists, this is a
            # no-op and its existing lot_id is left untouched. If it's
            # brand new, it needs a lot_id immediately (not just at the
            # next restart's backfill) or it would be silently invisible
            # to every report until then.
            lot_id = self._ensure_unassigned_lot(conn)
            conn.execute(
                "INSERT OR IGNORE INTO cameras (id, name, lot_id, created_at) VALUES (?, ?, ?, ?)",
                (camera_id, camera_id.replace("_", " ").title(), lot_id, utc_now()),
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

    def create_camera(
        self, name: str, camera_id: str | None = None, lot_id: int | None = None
    ) -> dict[str, Any]:
        """Creates a new, independent camera. Its images, marked spaces, and
        detection history are scoped entirely by camera_id (see list_images/
        list_spaces/etc.), so this never touches or overwrites any other
        camera's data — that isolation is the whole point of this method
        existing as a first-class action instead of the old implicit
        "type a new folder name and hope" flow.
        """
        name = name.strip()
        if not name:
            raise ValueError("name is required")
        with self.connect() as conn:
            # Reject a duplicate display name outright (case-insensitive):
            # two cameras that both show up as "Camera 2" in the picker are
            # indistinguishable to look at, even once their underlying ids
            # are made safely unique below.
            existing = conn.execute(
                "SELECT id FROM cameras WHERE LOWER(name) = ?", (name.lower(),)
            ).fetchone()
            if existing is not None:
                raise ValueError(
                    f'A camera named "{name}" already exists (id "{existing["id"]}"). '
                    "Pick a different name, or select that camera from the dropdown instead."
                )
            # Lowercased and checked case-insensitively: without this, typing
            # "Camera 2" when a lowercase "camera_2" already exists would
            # silently create a second, visually-identical camera ("Camera_2")
            # instead of either reusing it or clearly disambiguating it.
            base_slug = (safe_segment(camera_id) if camera_id else safe_segment(name)).lower()
            candidate = base_slug
            counter = 2
            while conn.execute(
                "SELECT 1 FROM cameras WHERE LOWER(id) = ?", (candidate,)
            ).fetchone():
                candidate = f"{base_slug}_{counter}"
                counter += 1
            resolved_camera_id = candidate

            if lot_id is not None:
                if not conn.execute("SELECT 1 FROM lots WHERE id = ?", (lot_id,)).fetchone():
                    raise ValueError(f"Lot {lot_id} does not exist.")
            else:
                lot_id = self._ensure_unassigned_lot(conn)

            conn.execute(
                "INSERT INTO cameras (id, name, lot_id, created_at) VALUES (?, ?, ?, ?)",
                (resolved_camera_id, name, lot_id, utc_now()),
            )
            row = conn.execute(
                """
                SELECT cameras.*, lots.name AS lot_name, lots.client_id AS client_id,
                       clients.name AS client_name
                FROM cameras
                LEFT JOIN lots ON lots.id = cameras.lot_id
                LEFT JOIN clients ON clients.id = lots.client_id
                WHERE cameras.id = ?
                """,
                (resolved_camera_id,),
            ).fetchone()
        (IMAGES_DIR / resolved_camera_id).mkdir(parents=True, exist_ok=True)
        return dict(row)


def space_from_row(row: sqlite3.Row) -> dict[str, Any]:
    item = dict(row)
    item["polygon"] = json.loads(item.pop("polygon_json"))
    if "current_occupied" in item:
        raw = item["current_occupied"]
        item["current_occupied"] = bool(raw) if raw is not None else None
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
        elif path == "/dashboard":
            self.serve_file(STATIC_DIR / "dashboard.html")
        elif path.startswith("/static/"):
            self.serve_file(STATIC_DIR / path.removeprefix("/static/"))
        elif path.startswith("/media/"):
            self.serve_media(path)
        elif path == "/api/health":
            self.send_json({"ok": True})
        elif path == "/api/cameras":
            lot_id_raw = query.get("lot_id", [None])[0]
            if lot_id_raw is None:
                self.send_json({"cameras": db.list_cameras()})
            else:
                try:
                    lot_id = int(lot_id_raw)
                except ValueError:
                    self.send_json({"error": "lot_id must be an integer"}, HTTPStatus.BAD_REQUEST)
                    return
                self.send_json({"cameras": db.list_cameras(lot_id)})
        elif path == "/api/clients":
            self.send_json({"clients": db.list_clients()})
        elif path == "/api/lots":
            client_id_raw = query.get("client_id", [None])[0]
            try:
                client_id = int(client_id_raw) if client_id_raw is not None else None
            except ValueError:
                self.send_json({"error": "client_id must be an integer"}, HTTPStatus.BAD_REQUEST)
                return
            self.send_json({"lots": db.list_lots(client_id)})
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
        elif re.fullmatch(r"/api/spaces/(\d+)/intervals", path):
            match = re.fullmatch(r"/api/spaces/(\d+)/intervals", path)
            space_id = int(match.group(1))
            try:
                limit = int(query.get("limit", ["50"])[0])
            except ValueError:
                limit = 50
            self.send_json({"space_id": space_id, "intervals": db.get_space_intervals(space_id, limit)})
        elif path.startswith("/api/config/"):
            camera_id = safe_segment(unquote(path.removeprefix("/api/config/")))
            self.send_json({"camera_id": camera_id, "spaces": db.list_spaces(camera_id)})
        elif re.fullmatch(r"/api/cameras/([A-Za-z0-9_.-]+)/detection-status", path):
            match = re.fullmatch(r"/api/cameras/([A-Za-z0-9_.-]+)/detection-status", path)
            self.send_json(get_detection_status(match.group(1)))
        elif path == "/api/reports/metrics":
            self.handle_report_metrics(query)
        elif path == "/api/reports/export":
            self.handle_report_export(query)
        else:
            self.send_error(HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        detect_match = re.fullmatch(r"/api/cameras/([A-Za-z0-9_.-]+)/detect", parsed.path)
        if detect_match:
            camera_id = detect_match.group(1)
            if db.get_camera(camera_id) is None:
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            trigger_detection(camera_id)
            self.send_json(get_detection_status(camera_id), HTTPStatus.ACCEPTED)
        elif parsed.path == "/api/spaces":
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
        elif parsed.path == "/api/videos":
            self.handle_video_upload()
        elif parsed.path == "/api/cameras":
            payload = self.read_json()
            name = (payload.get("name") or "").strip()
            if not name:
                self.send_json({"error": "name is required"}, HTTPStatus.BAD_REQUEST)
                return
            lot_id_raw = payload.get("lot_id")
            lot_id = None
            if lot_id_raw is not None:
                try:
                    lot_id = int(lot_id_raw)
                except (TypeError, ValueError):
                    self.send_json({"error": "lot_id must be an integer"}, HTTPStatus.BAD_REQUEST)
                    return
            try:
                camera = db.create_camera(name, camera_id=payload.get("camera_id"), lot_id=lot_id)
            except ValueError as exc:
                self.send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
                return
            self.send_json({"camera": camera}, HTTPStatus.CREATED)
        elif parsed.path == "/api/clients":
            payload = self.read_json()
            name = (payload.get("name") or "").strip()
            if not name:
                self.send_json({"error": "name is required"}, HTTPStatus.BAD_REQUEST)
                return
            self.send_json({"client": db.create_client(name)}, HTTPStatus.CREATED)
        elif parsed.path == "/api/lots":
            payload = self.read_json()
            name = (payload.get("name") or "").strip()
            try:
                client_id = int(payload["client_id"])
            except (KeyError, TypeError, ValueError):
                self.send_json({"error": "client_id is required and must be an integer"}, HTTPStatus.BAD_REQUEST)
                return
            if not name:
                self.send_json({"error": "name is required"}, HTTPStatus.BAD_REQUEST)
                return
            try:
                lot = db.create_lot(client_id, name, payload.get("address") or None)
            except ValueError as exc:
                self.send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
                return
            self.send_json({"lot": lot}, HTTPStatus.CREATED)
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

    def do_PATCH(self) -> None:
        parsed = urlparse(self.path)
        space_match = re.fullmatch(r"/api/spaces/(\d+)", parsed.path)
        camera_match = re.fullmatch(r"/api/cameras/([A-Za-z0-9_.-]+)", parsed.path)
        if space_match:
            payload = self.read_json()
            try:
                polygon = payload["polygon"]
                if len(polygon) != 4:
                    raise ValueError("A parking space needs exactly 4 points.")
            except (KeyError, TypeError, ValueError) as exc:
                self.send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
                return
            space = db.update_space_polygon(int(space_match.group(1)), polygon)
            if space is None:
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            self.send_json({"space": space})
        elif camera_match:
            payload = self.read_json()
            lot_id: int | None = None
            if "lot_id" in payload:
                try:
                    lot_id = int(payload["lot_id"])
                except (TypeError, ValueError):
                    self.send_json({"error": "lot_id must be an integer"}, HTTPStatus.BAD_REQUEST)
                    return
            min_occupied_seconds: Any = _UNSET
            if "min_occupied_seconds" in payload:
                raw = payload["min_occupied_seconds"]
                if raw is None:
                    min_occupied_seconds = None  # explicit clear -> use script default
                else:
                    try:
                        min_occupied_seconds = float(raw)
                    except (TypeError, ValueError):
                        self.send_json(
                            {"error": "min_occupied_seconds must be a number or null"},
                            HTTPStatus.BAD_REQUEST,
                        )
                        return
            if lot_id is None and min_occupied_seconds is _UNSET:
                self.send_json(
                    {"error": "Provide lot_id and/or min_occupied_seconds to update."},
                    HTTPStatus.BAD_REQUEST,
                )
                return
            try:
                camera = db.update_camera(
                    camera_match.group(1), lot_id=lot_id, min_occupied_seconds=min_occupied_seconds
                )
            except ValueError as exc:
                self.send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
                return
            if camera is None:
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            self.send_json({"camera": camera})
        else:
            self.send_error(HTTPStatus.NOT_FOUND)

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
        trigger_detection(image["camera_id"])
        self.send_json({"image": image, "detection_triggered": True}, HTTPStatus.CREATED)

    def handle_video_upload(self) -> None:
        content_length = int(self.headers.get("Content-Length", "0"))
        if content_length > MAX_VIDEO_UPLOAD_BYTES:
            self.send_json(
                {"error": "Video is larger than the 2 GiB upload limit."},
                HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
            )
            return

        form = cgi.FieldStorage(
            fp=self.rfile,
            headers=self.headers,
            environ={
                "REQUEST_METHOD": "POST",
                "CONTENT_TYPE": self.headers.get("Content-Type", ""),
            },
        )
        video_field = form["video"] if "video" in form else None
        if video_field is None or not video_field.filename:
            self.send_json({"error": "Upload requires a video field."}, HTTPStatus.BAD_REQUEST)
            return

        camera_id = safe_segment(form.getfirst("camera_id", DEFAULT_CAMERA_ID))

        try:
            interval = float(form.getfirst("interval_seconds", "5"))
            if interval <= 0:
                raise ValueError
        except (TypeError, ValueError):
            self.send_json(
                {"error": "interval_seconds must be a positive number."}, HTTPStatus.BAD_REQUEST
            )
            return

        end_seconds: float | None = None
        end_raw = (form.getfirst("end_seconds", "") or "").strip()
        if end_raw:
            try:
                end_seconds = float(end_raw)
                if end_seconds <= 0:
                    raise ValueError
            except ValueError:
                self.send_json(
                    {"error": "end_seconds must be a positive number."}, HTTPStatus.BAD_REQUEST
                )
                return

        suffix = Path(video_field.filename).suffix.lower()
        if suffix not in VIDEO_EXTENSIONS:
            self.send_json(
                {"error": f"Unsupported video type: {suffix or 'unknown'}"}, HTTPStatus.BAD_REQUEST
            )
            return

        video_dir = SOURCE_VIDEOS_DIR / camera_id
        video_dir.mkdir(parents=True, exist_ok=True)
        safe_name = safe_segment(Path(video_field.filename).stem) + suffix
        video_path = video_dir / safe_name
        counter = 1
        while video_path.exists():
            video_path = video_dir / f"{Path(safe_name).stem}_{counter}{suffix}"
            counter += 1
        with video_path.open("wb") as handle:
            shutil.copyfileobj(video_field.file, handle)

        try:
            written = extract_frames(
                video_path,
                IMAGES_DIR / camera_id,
                interval=interval,
                start_time=datetime.now(timezone.utc),
                end_seconds=end_seconds,
            )
        except FfmpegUnavailable as exc:
            self.send_json({"error": str(exc)}, HTTPStatus.SERVICE_UNAVAILABLE)
            return
        except (RuntimeError, ValueError) as exc:
            self.send_json({"error": str(exc)}, HTTPStatus.INTERNAL_SERVER_ERROR)
            return

        db.sync_images()
        detection_triggered = False
        if written:
            trigger_detection(camera_id)
            detection_triggered = True
        self.send_json(
            {
                "camera_id": camera_id,
                "video": video_path.name,
                "frames_extracted": len(written),
                "detection_triggered": detection_triggered,
            },
            HTTPStatus.CREATED,
        )

    def resolve_report_request(
        self, query: dict[str, list[str]]
    ) -> tuple[int, datetime, datetime, str | None] | None:
        """Parses lot_id/camera_id/start/end from the query string; sends an
        error response and returns None if anything is wrong, so callers can
        just `if resolved is None: return`. camera_id is optional -- when
        given it must exist and belong to the requested lot, narrowing the
        report down to that one camera instead of the whole lot."""
        lot_id_raw = query.get("lot_id", [None])[0]
        if lot_id_raw is None:
            self.send_json({"error": "lot_id is required"}, HTTPStatus.BAD_REQUEST)
            return None
        try:
            lot_id = int(lot_id_raw)
        except ValueError:
            self.send_json({"error": "lot_id must be an integer"}, HTTPStatus.BAD_REQUEST)
            return None
        if db.get_lot(lot_id) is None:
            self.send_json({"error": f"Lot {lot_id} does not exist."}, HTTPStatus.NOT_FOUND)
            return None
        camera_id = query.get("camera_id", [None])[0] or None
        if camera_id is not None:
            camera = db.get_camera(camera_id)
            if camera is None:
                self.send_json({"error": f'Camera "{camera_id}" does not exist.'}, HTTPStatus.NOT_FOUND)
                return None
            if camera.get("lot_id") != lot_id:
                self.send_json(
                    {"error": f'Camera "{camera_id}" is not part of lot {lot_id}.'},
                    HTTPStatus.BAD_REQUEST,
                )
                return None
        try:
            start, end = parse_report_range(query)
        except ValueError as exc:
            self.send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
            return None
        return lot_id, start, end, camera_id

    def handle_report_metrics(self, query: dict[str, list[str]]) -> None:
        resolved = self.resolve_report_request(query)
        if resolved is None:
            return
        lot_id, start, end, camera_id = resolved
        with db.connect() as conn:
            report = analytics.compute_lot_report(conn, lot_id, start, end, camera_id)
        self.send_json(report)

    def handle_report_export(self, query: dict[str, list[str]]) -> None:
        resolved = self.resolve_report_request(query)
        if resolved is None:
            return
        lot_id, start, end, camera_id = resolved
        lot = db.get_lot(lot_id)
        camera = db.get_camera(camera_id) if camera_id else None
        with db.connect() as conn:
            report = analytics.compute_lot_report(conn, lot_id, start, end, camera_id)
        html = reports.render_report_html(lot, report, camera=camera)
        body = html.encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

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
    SOURCE_VIDEOS_DIR.mkdir(parents=True, exist_ok=True)
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
