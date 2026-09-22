"""Shared vehicle-detection and space-matching logic.

Used by BOTH the batch script (scripts/detect_occupancy.py -- runs once per
photo, for uploaded/imported footage) and the continuous live detector
(app/live_detection.py -- runs once per live camera snapshot), so the two
ways of turning "a detected vehicle box" into "is this parking space
occupied" can never quietly drift apart from each other.

Deliberately does NOT import rfdetr/torch at module load time: that import
takes real time (see scripts/benchmark_detection_speed.py) and would slow
down every server start even for installs with no live camera configured.
Whoever needs an actual model instance (scripts/detect_occupancy.py's main(),
or app/live_detection.py's worker) imports rfdetr itself, only when it's
actually about to be used.
"""

from __future__ import annotations

import io
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from PIL import Image

# RF-DETR's pretrained-COCO checkpoints report raw COCO category IDs (1-90,
# with gaps) as class_id, NOT the 0-indexed 0-79 scheme some other COCO
# detectors use. car=3, motorcycle=4, bus=6, truck=8.
VEHICLE_CLASS_IDS = {3, 4, 6, 8}


@dataclass
class Detection:
    class_id: int
    class_name: str
    confidence: float
    box: tuple[float, float, float, float]


def load_rgb_image(image_path: Path) -> Image.Image:
    """Opens an image file as plain 3-channel RGB.

    RF-DETR insists on exactly 3 channels and rejects anything else with
    "Expected 3 channels (RGB), but got 4 channels". Real-world images are
    often not 3-channel: macOS screenshots are PNGs with an alpha
    (transparency) channel, and cameras in night/infrared mode can produce
    single-channel grayscale pictures. Converting here makes every one of
    those work. See rgb_image_from_bytes for the same conversion starting
    from in-memory JPEG bytes rather than a file on disk.
    """
    with Image.open(image_path) as image:
        return image.convert("RGB")


def rgb_image_from_bytes(data: bytes) -> Image.Image:
    """Same conversion as load_rgb_image, for a JPEG already in memory (a
    live camera snapshot, which is never written to disk before it's
    checked) instead of a file on disk."""
    with Image.open(io.BytesIO(data)) as image:
        return image.convert("RGB")


def run_detector(model: Any, rgb_image: Image.Image, confidence: float) -> list[Detection]:
    result = model.predict(rgb_image, threshold=confidence)
    detections: list[Detection] = []
    class_names = result.data.get("class_name") if result.data else None
    for index in range(len(result.xyxy)):
        class_id = int(result.class_id[index])
        if class_id not in VEHICLE_CLASS_IDS:
            continue
        x1, y1, x2, y2 = [float(v) for v in result.xyxy[index]]
        class_name = str(class_names[index]) if class_names is not None else str(class_id)
        detections.append(
            Detection(
                class_id=class_id,
                class_name=class_name,
                confidence=float(result.confidence[index]),
                box=(x1, y1, x2, y2),
            )
        )
    return detections


def box_to_json(box: tuple[float, float, float, float]) -> dict[str, float]:
    x1, y1, x2, y2 = box
    return {"x1": x1, "y1": y1, "x2": x2, "y2": y2}


def occupancy_overlap_score(
    polygon: list[dict[str, float]],
    box: tuple[float, float, float, float],
) -> float:
    clipped = clip_polygon_to_box(polygon, box)
    return polygon_area(clipped) / max(polygon_area(polygon), 1.0)


def box_anchor(box: tuple[float, float, float, float], y_ratio: float) -> dict[str, float]:
    x1, y1, x2, y2 = box
    clamped_ratio = max(0.0, min(1.0, y_ratio))
    return {
        "x": (x1 + x2) / 2,
        "y": y1 + (y2 - y1) * clamped_ratio,
    }


def box_center(box: tuple[float, float, float, float]) -> dict[str, float]:
    return box_anchor(box, 0.5)


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


def scale_polygon(
    polygon: list[dict[str, float]],
    reference_size: tuple[float | None, float | None],
    target_size: tuple[float, float],
) -> list[dict[str, float]]:
    """Rescales a space's polygon from the pixel size of the image it was
    marked on (`reference_size`) to the size of a different image now being
    analyzed (`target_size`).

    A space's polygon coordinates are just pixel positions on whatever photo
    was on screen when it was drawn. That's fine as long as every photo
    later checked against it is the same size -- true by construction for
    the batch pipeline (it only ever analyzes the exact image it was
    uploaded from) but NOT guaranteed for the live feed, which checks
    whatever resolution the camera happens to hand back, possibly different
    from the (maybe much smaller, e.g. a screenshot) photo used to mark the
    space. Without this, a space marked on a small photo ends up checked
    against a tiny, wrong corner of a bigger live photo -- coordinates that
    used to span the real parking spot now cover empty space near the
    corner, so nothing ever matches and the spot reads as permanently
    vacant no matter what's actually parked there.

    A no-op (returns `polygon` unchanged) when there's nothing to scale by:
    no recorded reference size (an older space, marked before this existed),
    or a reference that already matches the target exactly.
    """
    reference_width, reference_height = reference_size
    target_width, target_height = target_size
    if not reference_width or not reference_height:
        return polygon
    if reference_width == target_width and reference_height == target_height:
        return polygon
    scale_x = target_width / reference_width
    scale_y = target_height / reference_height
    return [{"x": point["x"] * scale_x, "y": point["y"] * scale_y} for point in polygon]


def assign_detections_to_spaces(
    spaces: list[dict[str, Any]],
    stored_detections: list[tuple[int, Detection]],
    anchor_y_ratio: float,
    fallback_overlap_threshold: float,
) -> dict[int, dict[str, float | int]]:
    candidates_by_space_id: dict[int, list[dict[str, float | int]]] = {
        space["id"]: [] for space in spaces
    }

    for detection_id, detection in stored_detections:
        anchor = box_anchor(detection.box, anchor_y_ratio)
        containing_spaces = [
            space
            for space in spaces
            if point_in_polygon(anchor, space["polygon"])
        ]
        if containing_spaces:
            best_space = max(
                containing_spaces,
                key=lambda space: occupancy_overlap_score(space["polygon"], detection.box),
            )
            candidates_by_space_id[best_space["id"]].append(
                {
                    "detection_id": detection_id,
                    "score": 1.0,
                    "confidence": detection.confidence,
                }
            )
            continue

        fallback_space = None
        fallback_score = 0.0
        for space in spaces:
            score = occupancy_overlap_score(space["polygon"], detection.box)
            if score > fallback_score:
                fallback_space = space
                fallback_score = score
        if fallback_space and fallback_score >= fallback_overlap_threshold:
            candidates_by_space_id[fallback_space["id"]].append(
                {
                    "detection_id": detection_id,
                    "score": fallback_score,
                    "confidence": detection.confidence,
                }
            )

    occupied_by_space_id = {}
    for space_id, candidates in candidates_by_space_id.items():
        if candidates:
            occupied_by_space_id[space_id] = max(
                candidates,
                key=lambda candidate: (candidate["score"], candidate["confidence"]),
            )
    return occupied_by_space_id
