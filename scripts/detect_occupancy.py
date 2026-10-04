from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from rfdetr import RFDETRLarge, RFDETRMedium, RFDETRNano, RFDETRSmall


ROOT = Path(__file__).resolve().parents[1]

# This file is run directly (python scripts/detect_occupancy.py), which puts only
# scripts/ on sys.path -- so add the project root to import the shared app package.
sys.path.insert(0, str(ROOT))
from app.detection_core import (  # noqa: E402  (must come after the sys.path line above)
    Detection,
    assign_detections_to_spaces,
    box_to_json,
    detect_vehicles,
    load_rgb_image,
    scale_polygon,
)
from app.intervals import (  # noqa: E402
    DEFAULT_MIN_OCCUPIED_SECONDS,
    _run_duration_seconds,
    recompute_space_intervals,
)
DB_PATH = ROOT / "data" / "db" / "parking_lot.sqlite"
IMAGES_DIR = ROOT / "data" / "images"


MODEL_SIZES = {
    "nano": RFDETRNano,
    "small": RFDETRSmall,
    "medium": RFDETRMedium,
    "large": RFDETRLarge,
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run RF-DETR vehicle detection and infer per-space occupancy."
    )
    parser.add_argument("--camera-id", default="camera_1")
    parser.add_argument(
        "--model-size",
        choices=sorted(MODEL_SIZES),
        default="medium",
        help="RF-DETR model size. Larger is more accurate but slower to run.",
    )
    parser.add_argument("--confidence", type=float, default=0.25)
    parser.add_argument(
        "--anchor-y-ratio",
        type=float,
        default=0.9,
        help="Vertical position inside the detection box used as the vehicle ground anchor.",
    )
    parser.add_argument(
        "--fallback-overlap-threshold",
        type=float,
        default=0.7,
        help="Only use bbox overlap when no anchor lands inside a space and overlap is very strong.",
    )
    parser.add_argument(
        "--min-occupied-seconds",
        type=float,
        default=DEFAULT_MIN_OCCUPIED_SECONDS,
        help=(
            "An occupied reading shorter than this (flanked by vacant frames on "
            "both sides) is treated as a vehicle passing through, not parking, "
            "and folded back into vacant time instead of counting as an arrival. "
            "Set to 0 to disable and count every occupied reading as a session."
        ),
    )
    return parser.parse_args()


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_tables(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
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
            start_image_id INTEGER,
            start_captured_at TEXT NOT NULL,
            end_image_id INTEGER,
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
        """
    )


def load_images(conn: sqlite3.Connection, camera_id: str) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            """
            SELECT id, camera_id, filename, captured_at
            FROM images
            WHERE camera_id = ?
            ORDER BY captured_at, filename
            """,
            (camera_id,),
        )
    )


def load_spaces(conn: sqlite3.Connection, camera_id: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT id, label, polygon_json, reference_width, reference_height
        FROM parking_spaces
        WHERE camera_id = ?
        ORDER BY id
        """,
        (camera_id,),
    )
    return [
        {
            "id": row["id"],
            "label": row["label"],
            "polygon": json.loads(row["polygon_json"]),
            "reference_width": row["reference_width"],
            "reference_height": row["reference_height"],
        }
        for row in rows
    ]


def store_detections(
    conn: sqlite3.Connection,
    image_id: int,
    model_name: str,
    detections: list[Detection],
) -> list[tuple[int, Detection]]:
    conn.execute("DELETE FROM occupancy_observations WHERE image_id = ?", (image_id,))
    conn.execute("DELETE FROM detections WHERE image_id = ? AND model_name = ?", (image_id, model_name))
    stored = []
    now = utc_now()
    for detection in detections:
        cursor = conn.execute(
            """
            INSERT INTO detections
                (image_id, model_name, class_id, class_name, confidence, bbox_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                image_id,
                model_name,
                detection.class_id,
                detection.class_name,
                detection.confidence,
                json.dumps(box_to_json(detection.box)),
                now,
            ),
        )
        stored.append((cursor.lastrowid, detection))
    return stored


def store_occupancy(
    conn: sqlite3.Connection,
    image_id: int,
    spaces: list[dict[str, Any]],
    stored_detections: list[tuple[int, Detection]],
    anchor_y_ratio: float,
    fallback_overlap_threshold: float,
    image_size: tuple[int, int],
) -> None:
    now = utc_now()
    scaled_spaces = [
        {
            "id": space["id"],
            "polygon": scale_polygon(
                space["polygon"], (space.get("reference_width"), space.get("reference_height")), image_size
            ),
        }
        for space in spaces
    ]
    occupied_by_space_id = assign_detections_to_spaces(
        scaled_spaces,
        stored_detections,
        anchor_y_ratio,
        fallback_overlap_threshold,
    )
    for space in spaces:
        match = occupied_by_space_id.get(space["id"])
        occupied = int(match is not None)
        best_detection_id = match["detection_id"] if match else None
        best_score = match["score"] if match else 0.0
        conn.execute(
            """
            INSERT INTO occupancy_observations
                (image_id, parking_space_id, occupied, score, detection_id, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(image_id, parking_space_id) DO UPDATE SET
                occupied = excluded.occupied,
                score = excluded.score,
                detection_id = excluded.detection_id,
                created_at = excluded.created_at
            """,
            (image_id, space["id"], occupied, best_score, best_detection_id, now),
        )


def main() -> None:
    args = parse_args()
    model = MODEL_SIZES[args.model_size]()
    model_name = f"rfdetr-{args.model_size}"

    with connect() as conn:
        init_tables(conn)
        images = load_images(conn, args.camera_id)
        spaces = load_spaces(conn, args.camera_id)
        if not images:
            raise RuntimeError(f"No images found for {args.camera_id}")
        if not spaces:
            raise RuntimeError(f"No parking spaces found for {args.camera_id}")

        for index, image in enumerate(images):
            image_path = IMAGES_DIR / image["camera_id"] / image["filename"]
            rgb_image = load_rgb_image(image_path)
            polygons = [
                scale_polygon(
                    space["polygon"], (space.get("reference_width"), space.get("reference_height")), rgb_image.size
                )
                for space in spaces
            ]
            detections = detect_vehicles(model, rgb_image, args.confidence, polygons)
            stored_detections = store_detections(conn, image["id"], model_name, detections)
            store_occupancy(
                conn,
                image["id"],
                spaces,
                stored_detections,
                args.anchor_y_ratio,
                args.fallback_overlap_threshold,
                rgb_image.size,
            )
            occupied_count = conn.execute(
                """
                SELECT COUNT(*)
                FROM occupancy_observations
                WHERE image_id = ? AND occupied = 1
                """,
                (image["id"],),
            ).fetchone()[0]
            print(f"{image['filename']}: {len(detections)} vehicles, {occupied_count}/{len(spaces)} occupied")
            # Machine-parseable progress marker, one per completed image, read
            # live by app/server.py's detection worker (it runs this script as
            # a subprocess and streams stdout) to drive a progress indicator in
            # the UI. flush=True matters: without it, Python buffers stdout in
            # full blocks when not attached to a terminal (i.e. when piped to a
            # subprocess), so the parent wouldn't see this until the process
            # exits -- defeating the point of a live progress marker.
            print(f"PROGRESS {index + 1} {len(images)}", flush=True)

        # Per-image detection is done, but there's a second step left --
        # rebuilding every space's occupied/vacant timeline from the results
        # -- that has no per-item progress of its own but can still take a
        # real few seconds for a lot with many spaces. Without a distinct
        # marker for it, the UI has nothing to show here except a stale
        # "100%" for however long this takes, which reads as finished (or
        # stuck) when it's actually still working.
        print("PHASE finalizing", flush=True)
        for space in spaces:
            recompute_space_intervals(conn, space["id"], args.min_occupied_seconds)
        print(
            f"Rebuilt occupancy timelines for {len(spaces)} space(s) "
            f"(min_occupied_seconds={args.min_occupied_seconds})."
        )


if __name__ == "__main__":
    main()
