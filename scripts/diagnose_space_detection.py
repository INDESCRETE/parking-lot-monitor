"""Diagnostic: overlays the raw detector output on top of the live camera's
current frame, alongside the marked space polygons, so a mismatch between
"where the model thinks the car is" and "where a space is drawn" can be seen
directly instead of guessed at.

Run from the project root with the project's own virtualenv (same one that
runs the live server), e.g.:

    .venv/bin/python scripts/diagnose_space_detection.py --camera-id reolink_live

Writes <camera-id>_diagnostic.png next to this script's working directory:
  - each space's polygon, outlined in the color the assignment logic
    actually decided (red = occupied, teal = vacant) -- matching the app's
    own colors
  - each raw vehicle detection's bounding box, outlined in yellow, with its
    confidence score
  - each detection's anchor point (the single point the app tests against
    every polygon -- see app/detection_core.py's box_anchor/ANCHOR_Y_RATIO),
    as a small yellow dot with a crosshair -- if this dot lands inside the
    wrong polygon, or in the gap between two polygons, that IS the bug

Also prints a text summary of every detection: its confidence, which
space(s) contain its anchor point, and its area-overlap score against every
space it meaningfully overlaps -- so a "should be occupied but reads
vacant" case (no polygon contains the anchor AND overlap stays under the
fallback threshold) is distinguishable from a "wrong space" case (anchor
lands inside a neighboring polygon).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from PIL import ImageDraw, ImageFont  # noqa: E402

from app import detection_core  # noqa: E402
from app.server import db  # noqa: E402

CONFIDENCE = 0.25
ANCHOR_Y_RATIO = 0.9
FALLBACK_OVERLAP_THRESHOLD = 0.7

OCCUPIED_COLOR = (220, 38, 38)
VACANT_COLOR = (38, 166, 154)
DETECTION_COLOR = (250, 204, 21)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--camera-id", default="reolink_live")
    parser.add_argument("--model-size", default="medium", choices=["nano", "small", "medium", "large"])
    args = parser.parse_args()

    camera_id = args.camera_id
    spaces = db.list_spaces(camera_id)
    if not spaces:
        print(f"No spaces marked for camera '{camera_id}'.")
        return

    frame_path = ROOT / "data" / "live" / camera_id / "latest.jpg"
    if not frame_path.exists():
        print(f"No live frame at {frame_path} -- is the live feed running for this camera?")
        return
    jpeg = frame_path.read_bytes()
    rgb_image = detection_core.rgb_image_from_bytes(jpeg)
    print(f"Frame: {frame_path} ({rgb_image.size[0]}x{rgb_image.size[1]})")

    print(f"Loading RF-DETR ({args.model_size})...")
    from rfdetr import RFDETRLarge, RFDETRMedium, RFDETRNano, RFDETRSmall

    model_cls = {"nano": RFDETRNano, "small": RFDETRSmall, "medium": RFDETRMedium, "large": RFDETRLarge}[
        args.model_size
    ]
    model = model_cls()
    detections = detection_core.run_detector(model, rgb_image, CONFIDENCE)
    print(f"{len(detections)} vehicle detection(s) at confidence >= {CONFIDENCE}\n")

    space_dicts = [
        {
            "id": s["id"],
            "label": s["label"],
            "polygon": detection_core.scale_polygon(
                s["polygon"], (s.get("reference_width"), s.get("reference_height")), rgb_image.size
            ),
        }
        for s in spaces
    ]

    indexed_detections = list(enumerate(detections))
    occupied_by_space_id = detection_core.assign_detections_to_spaces(
        space_dicts, indexed_detections, ANCHOR_Y_RATIO, FALLBACK_OVERLAP_THRESHOLD
    )

    # ---- text summary ----
    print("--- per-space result ---")
    for s in space_dicts:
        if s["id"] in occupied_by_space_id:
            c = occupied_by_space_id[s["id"]]
            det = detections[c["detection_id"]]
            print(
                f"  {s['label']}: OCCUPIED  (det conf={det.confidence:.2f}, "
                f"overlap score={c['score']:.2f}, box={tuple(round(v, 1) for v in det.box)})"
            )
        else:
            print(f"  {s['label']}: vacant")

    print("\n--- per-detection diagnostic ---")
    for i, d in enumerate(detections):
        anchor = detection_core.box_anchor(d.box, ANCHOR_Y_RATIO)
        containing = [s["label"] for s in space_dicts if detection_core.point_in_polygon(anchor, s["polygon"])]
        overlaps = [
            (s["label"], round(detection_core.occupancy_overlap_score(s["polygon"], d.box), 2))
            for s in space_dicts
        ]
        overlaps = sorted([o for o in overlaps if o[1] > 0.05], key=lambda o: -o[1])
        flag = ""
        if not containing and (not overlaps or overlaps[0][1] < FALLBACK_OVERLAP_THRESHOLD):
            flag = "  <-- anchor lands in no space, and no space clears the fallback overlap threshold: THIS CAR WON'T REGISTER ANYWHERE"
        print(
            f"  det[{i}] class={d.class_name} conf={d.confidence:.2f} box={tuple(round(v, 1) for v in d.box)}\n"
            f"          anchor=({anchor['x']:.1f}, {anchor['y']:.1f})  contains={containing or '(none)'}  "
            f"overlaps={overlaps or '(none)'}{flag}"
        )

    # ---- annotated image ----
    annotated = rgb_image.copy()
    draw = ImageDraw.Draw(annotated)
    try:
        font = ImageFont.load_default(size=28)
    except TypeError:
        font = ImageFont.load_default()

    for s in space_dicts:
        color = OCCUPIED_COLOR if s["id"] in occupied_by_space_id else VACANT_COLOR
        points = [(p["x"], p["y"]) for p in s["polygon"]]
        draw.polygon(points, outline=color, width=4)
        cx = sum(p[0] for p in points) / len(points)
        cy = sum(p[1] for p in points) / len(points)
        draw.text((cx, cy), s["label"], fill=color, font=font, anchor="mm")

    for i, d in enumerate(detections):
        x1, y1, x2, y2 = d.box
        draw.rectangle([x1, y1, x2, y2], outline=DETECTION_COLOR, width=3)
        draw.text((x1 + 4, y1 + 4), f"#{i} {d.confidence:.2f}", fill=DETECTION_COLOR, font=font)
        anchor = detection_core.box_anchor(d.box, ANCHOR_Y_RATIO)
        ax, ay = anchor["x"], anchor["y"]
        r = 10
        draw.ellipse([ax - r, ay - r, ax + r, ay + r], outline=DETECTION_COLOR, width=3)
        draw.line([ax - 16, ay, ax + 16, ay], fill=DETECTION_COLOR, width=2)
        draw.line([ax, ay - 16, ax, ay + 16], fill=DETECTION_COLOR, width=2)

    out_path = Path(f"{camera_id}_diagnostic.png")
    annotated.save(out_path)
    print(f"\nWrote {out_path.resolve()}")


if __name__ == "__main__":
    main()
