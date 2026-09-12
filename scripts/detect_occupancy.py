from __future__ import annotations

import argparse
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ultralytics import YOLO


ROOT = Path(__file__).resolve().parents[1]
DB_PATH = ROOT / "data" / "db" / "parking_lot.sqlite"
IMAGES_DIR = ROOT / "data" / "images"
VEHICLE_CLASS_IDS = {2, 3, 5, 7}


@dataclass
class Detection:
    class_id: int
    class_name: str
    confidence: float
    box: tuple[float, float, float, float]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run YOLO vehicle detection and infer per-space occupancy."
    )
    parser.add_argument("--camera-id", default="camera_1")
    parser.add_argument("--model", default="yolov8n.pt")
    parser.add_argument("--confidence", type=float, default=0.25)
    parser.add_argument("--overlap-threshold", type=float, default=0.18)
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
        SELECT id, label, polygon_json
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
        }
        for row in rows
    ]


def run_yolo(model: YOLO, image_path: Path, confidence: float) -> list[Detection]:
    results = model.predict(str(image_path), conf=confidence, verbose=False)
    detections: list[Detection] = []
    names = model.names
    for result in results:
        for box in result.boxes:
            class_id = int(box.cls.item())
            if class_id not in VEHICLE_CLASS_IDS:
                continue
            x1, y1, x2, y2 = box.xyxy[0].tolist()
            detections.append(
                Detection(
                    class_id=class_id,
                    class_name=str(names[class_id]),
                    confidence=float(box.conf.item()),
                    box=(x1, y1, x2, y2),
                )
            )
    return detections


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
    overlap_threshold: float,
) -> None:
    now = utc_now()
    for space in spaces:
        best_detection_id = None
        best_score = 0.0
        for detection_id, detection in stored_detections:
            score = occupancy_score(space["polygon"], detection.box)
            if score > best_score:
                best_detection_id = detection_id
                best_score = score

        occupied = int(best_score >= overlap_threshold)
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


def box_to_json(box: tuple[float, float, float, float]) -> dict[str, float]:
    x1, y1, x2, y2 = box
    return {"x1": x1, "y1": y1, "x2": x2, "y2": y2}


def occupancy_score(polygon: list[dict[str, float]], box: tuple[float, float, float, float]) -> float:
    clipped = clip_polygon_to_box(polygon, box)
    overlap_ratio = polygon_area(clipped) / max(polygon_area(polygon), 1.0)
    center_bonus = 1.0 if point_in_polygon(box_center(box), polygon) else 0.0
    return max(overlap_ratio, center_bonus)


def box_center(box: tuple[float, float, float, float]) -> dict[str, float]:
    x1, y1, x2, y2 = box
    return {"x": (x1 + x2) / 2, "y": (y1 + y2) / 2}


def polygon_area(points: list[dict[str, float]]) -> float:
    if len(points) < 3:
        return 0.0
    total = 0.0
    for index, point in enumerate(points):
        next_point = points[(index + 1) % len(points)]
        total += point["x"] * next_point["y"] - next_point["x"] * point["y"]
    return abs(total) / 2


def clip_polygon_to_box(
    polygon: list[dict[str, float]],
    box: tuple[float, float, float, float],
) -> list[dict[str, float]]:
    x1, y1, x2, y2 = box
    clipped = polygon
    for edge in (
        ("left", x1),
        ("right", x2),
        ("top", y1),
        ("bottom", y2),
    ):
        clipped = clip_against_edge(clipped, edge)
        if not clipped:
            break
    return clipped


def clip_against_edge(
    points: list[dict[str, float]],
    edge: tuple[str, float],
) -> list[dict[str, float]]:
    if not points:
        return []
    output = []
    previous = points[-1]
    for current in points:
        if inside_edge(current, edge):
            if not inside_edge(previous, edge):
                output.append(intersection(previous, current, edge))
            output.append(current)
        elif inside_edge(previous, edge):
            output.append(intersection(previous, current, edge))
        previous = current
    return output


def inside_edge(point: dict[str, float], edge: tuple[str, float]) -> bool:
    name, value = edge
    if name == "left":
        return point["x"] >= value
    if name == "right":
        return point["x"] <= value
    if name == "top":
        return point["y"] >= value
    return point["y"] <= value


def intersection(
    start: dict[str, float],
    end: dict[str, float],
    edge: tuple[str, float],
) -> dict[str, float]:
    name, value = edge
    dx = end["x"] - start["x"]
    dy = end["y"] - start["y"]
    if name in {"left", "right"}:
        t = 0 if dx == 0 else (value - start["x"]) / dx
        return {"x": value, "y": start["y"] + t * dy}
    t = 0 if dy == 0 else (value - start["y"]) / dy
    return {"x": start["x"] + t * dx, "y": value}


def point_in_polygon(point: dict[str, float], polygon: list[dict[str, float]]) -> bool:
    inside = False
    previous = polygon[-1]
    for current in polygon:
        intersects = (
            (current["y"] > point["y"]) != (previous["y"] > point["y"])
            and point["x"]
            < (previous["x"] - current["x"])
            * (point["y"] - current["y"])
            / (previous["y"] - current["y"])
            + current["x"]
        )
        if intersects:
            inside = not inside
        previous = current
    return inside


def main() -> None:
    args = parse_args()
    model = YOLO(args.model)
    model_name = Path(args.model).name

    with connect() as conn:
        init_tables(conn)
        images = load_images(conn, args.camera_id)
        spaces = load_spaces(conn, args.camera_id)
        if not images:
            raise RuntimeError(f"No images found for {args.camera_id}")
        if not spaces:
            raise RuntimeError(f"No parking spaces found for {args.camera_id}")

        for image in images:
            image_path = IMAGES_DIR / image["camera_id"] / image["filename"]
            detections = run_yolo(model, image_path, args.confidence)
            stored_detections = store_detections(conn, image["id"], model_name, detections)
            store_occupancy(
                conn,
                image["id"],
                spaces,
                stored_detections,
                args.overlap_threshold,
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


if __name__ == "__main__":
    main()
