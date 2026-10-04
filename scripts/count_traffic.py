"""Count vehicles crossing lines in a recorded video (traffic analysis, step 1).

Draw one or more lines on the picture; every vehicle that crosses a line is
counted once, with its direction and type (car / truck / bus / motorcycle).
Results come out as spreadsheets (CSV) in 15-minute blocks, the format
traffic studies use, plus a preview video showing what was counted so the
numbers can be checked by eye.

Step 1 -- see the picture and pick where the line goes:

    .venv/bin/python scripts/count_traffic.py my_video.mp4 --snapshot

  This saves the first frame with a grid on it. Grid labels are fractions
  of the picture (0.0 = left/top edge, 1.0 = right/bottom edge).

Step 2 -- check the line lands where you want (optional, same command plus --line):

    .venv/bin/python scripts/count_traffic.py my_video.mp4 --snapshot \\
        --line "Main St:0.05,0.62,0.95,0.55:eastbound/westbound"

Step 3 -- count:

    .venv/bin/python scripts/count_traffic.py my_video.mp4 \\
        --line "Main St:0.05,0.62,0.95,0.55:eastbound/westbound" \\
        --start "2026-09-27 14:00"

Line format: "Name:x1,y1,x2,y2:forward-name/reverse-name" (name and
direction names optional). Numbers are fractions of the picture, or pixels.
Direction: standing at the first point looking toward the second point,
crossing from your left to your right is the first ("forward") name. For a
line drawn left-to-right across the picture, that means moving DOWN the
picture (usually toward the camera). If the names come out swapped, just
swap them.

--start is the real clock time the video begins (local time). Without it,
blocks are labelled by time into the video (00:00-00:15, ...).

Output (in data/traffic_runs/<video>_<time>/ unless --out is given):
  counts_15min.csv   one row per 15-minute block, line and direction
  crossings.csv      one row per vehicle counted
  summary.txt        totals, busiest 15 minutes, busiest hour
  preview.mp4        the video with lines, tracked vehicles and running counts

Use --seconds 120 for a quick trial on the first two minutes.

Tip: run the line all the way to the edge of the picture (0.0 or 1.0) when a
lane is right at the edge -- a vehicle is counted by its bottom-middle
point, and a lane at the very bottom of the picture can pass below a line
that stops short.
"""

from __future__ import annotations

import argparse
import csv
import io
import math
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Iterable, Iterator, Optional

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from PIL import Image, ImageDraw, ImageFont  # noqa: E402

from app import detection_core  # noqa: E402
from app.tracking import (  # noqa: E402
    VEHICLE_CLASSES,
    CountLine,
    Crossing,
    LineCounter,
    VehicleTracker,
    crop_region_for_lines,
    parse_line_spec,
)

RUNS_DIR = ROOT / "data" / "traffic_runs"
DEFAULT_FPS = 5.0
DEFAULT_MAX_WIDTH = 1280
DEFAULT_CONFIDENCE = 0.25


# --- Reading the video ----------------------------------------------------------


def find_ffmpeg() -> str:
    """The same ffmpeg the rest of the app uses (the copy bundled with the
    imageio-ffmpeg package in .venv), falling back to one on the system.
    Deliberately doesn't need ffprobe: the bundled copy doesn't include it."""
    from app.live import find_ffmpeg as app_find_ffmpeg

    path = app_find_ffmpeg()
    if shutil.which(path) or Path(path).exists():
        return path
    raise SystemExit(
        "Couldn't find ffmpeg. Run this with the project's own Python "
        "(.venv/bin/python), which has it built in."
    )


def probe_video(path: Path) -> dict:
    """Width and height as displayed (after any phone rotation), and length.

    Size comes from actually decoding the first frame, so rotation is
    already applied; length comes from the "Duration:" line ffmpeg prints."""
    ffmpeg = find_ffmpeg()
    out = subprocess.run(
        [ffmpeg, "-hide_banner", "-i", str(path), "-frames:v", "1", "-f", "image2pipe", "-vcodec", "png", "-"],
        capture_output=True,
    )
    if out.returncode != 0 or not out.stdout:
        detail = out.stderr.decode("utf-8", "replace").strip().splitlines()
        raise SystemExit(f"Couldn't read {path}: {detail[-1] if detail else 'not a video?'}")
    with Image.open(io.BytesIO(out.stdout)) as first:
        width, height = first.size
    duration = None
    match = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", out.stderr.decode("utf-8", "replace"))
    if match:
        h, m, sec = match.groups()
        duration = int(h) * 3600 + int(m) * 60 + float(sec)
    return {"width": width, "height": height, "duration": duration}


def output_size(width: int, height: int, max_width: int) -> tuple:
    if max_width and width > max_width:
        scale = max_width / width
        width, height = max_width, int(round(height * scale))
    return (width - width % 2, height - height % 2)


def read_frames(path: Path, fps: float, size: tuple, seconds: Optional[float]) -> Iterator[Image.Image]:
    """Yields RGB frames, `fps` per second of video, resized to `size`."""
    ffmpeg = find_ffmpeg()
    width, height = size
    cmd = [ffmpeg, "-v", "error", "-i", str(path)]
    if seconds:
        cmd += ["-t", str(seconds)]
    cmd += ["-vf", f"fps={fps},scale={width}:{height}", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    frame_bytes = width * height * 3
    try:
        while True:
            data = proc.stdout.read(frame_bytes)
            if len(data) < frame_bytes:
                break
            yield Image.frombytes("RGB", (width, height), data)
    finally:
        proc.stdout.close()
        proc.kill()
        proc.wait()
        proc.stderr.close()


# --- Detection -----------------------------------------------------------------


def make_rfdetr_detector(model_size: str, confidence: float) -> Callable:
    from rfdetr import RFDETRLarge, RFDETRMedium, RFDETRNano, RFDETRSmall

    sizes = {"nano": RFDETRNano, "small": RFDETRSmall, "medium": RFDETRMedium, "large": RFDETRLarge}
    model = sizes[model_size]()

    def detect(image: Image.Image, region: Optional[tuple]) -> list:
        if region is None:
            found = detection_core.run_detector(model, image, confidence)
        else:
            left, top = region[0], region[1]
            found = [
                detection_core.Detection(
                    d.class_id, d.class_name, d.confidence,
                    (d.box[0] + left, d.box[1] + top, d.box[2] + left, d.box[3] + top),
                )
                for d in detection_core.run_detector(model, image.crop(region), confidence)
            ]
        return detection_core.remove_duplicate_detections(found)

    return detect


# --- Drawing -----------------------------------------------------------------------

LINE_COLOR = (255, 214, 0)
TRACK_COLOR = (0, 190, 255)
COUNTED_COLOR = (40, 220, 90)
ZONE_COLOR = (255, 255, 255)


def _font(size: int):
    for name in ("/System/Library/Fonts/Supplemental/Arial.ttf", "/System/Library/Fonts/Helvetica.ttc",
                 "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default()


def _label(draw: ImageDraw.ImageDraw, xy: tuple, text: str, font, fill=(255, 255, 255)) -> None:
    x, y = xy
    bbox = draw.textbbox((x, y), text, font=font)
    draw.rectangle((bbox[0] - 3, bbox[1] - 2, bbox[2] + 3, bbox[3] + 2), fill=(0, 0, 0))
    draw.text((x, y), text, font=font, fill=fill)


def draw_lines(draw: ImageDraw.ImageDraw, lines: Iterable[CountLine], font, totals: Optional[dict] = None) -> None:
    for line in lines:
        (x1, y1), (x2, y2) = line.p1, line.p2
        draw.line((x1, y1, x2, y2), fill=LINE_COLOR, width=4)
        # Arrow from the middle of the line toward the "forward" side.
        mx, my = (x1 + x2) / 2, (y1 + y2) / 2
        length = math.hypot(x2 - x1, y2 - y1) or 1
        nx, ny = -(y2 - y1) / length, (x2 - x1) / length  # points to the forward (right-hand) side
        tip = (mx + nx * 40, my + ny * 40)
        draw.line((mx, my, tip[0], tip[1]), fill=LINE_COLOR, width=3)
        for sign in (1, -1):
            ax = tip[0] - nx * 12 + sign * (-ny) * 8
            ay = tip[1] - ny * 12 + sign * nx * 8
            draw.line((tip[0], tip[1], ax, ay), fill=LINE_COLOR, width=3)
        text = f"{line.name}  → {line.forward_label}"
        if totals is not None:
            text = (
                f"{line.name}: {line.forward_label} {totals.get((line.line_id, 'forward'), 0)}"
                f" | {line.reverse_label} {totals.get((line.line_id, 'reverse'), 0)}"
            )
        _label(draw, (min(x1, x2), min(y1, y2) - 28), text, font, LINE_COLOR)


def snapshot_image(frame: Image.Image, lines: list, region: Optional[tuple]) -> Image.Image:
    image = frame.copy()
    draw = ImageDraw.Draw(image)
    font = _font(max(14, image.width // 70))
    w, h = image.size
    for i in range(1, 10):
        x, y = w * i / 10, h * i / 10
        draw.line((x, 0, x, h), fill=(255, 255, 255), width=1)
        draw.line((0, y, w, y), fill=(255, 255, 255), width=1)
        _label(draw, (x + 3, 3), f"{i / 10:.1f}", font)
        _label(draw, (3, y + 3), f"{i / 10:.1f}", font)
    if region:
        draw.rectangle(region, outline=ZONE_COLOR, width=2)
        _label(draw, (region[0] + 4, region[3] - 26), "area the model looks at", font)
    draw_lines(draw, lines, font)
    return image


class PreviewWriter:
    def __init__(self, path: Path, size: tuple, fps: float) -> None:
        ffmpeg = find_ffmpeg()
        encoders = subprocess.run([ffmpeg, "-hide_banner", "-encoders"], capture_output=True, text=True).stdout
        codec = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "26"] if "libx264" in encoders else ["-c:v", "mpeg4", "-q:v", "5"]
        self.proc = subprocess.Popen(
            [ffmpeg, "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "rgb24",
             "-s", f"{size[0]}x{size[1]}", "-r", str(fps), "-i", "-",
             *codec, "-pix_fmt", "yuv420p", str(path)],
            stdin=subprocess.PIPE,
        )
        self.font = _font(max(14, size[0] // 80))

    def write(self, frame: Image.Image, lines, tracks, counted_ids: set, totals: dict, region, clock: str) -> None:
        draw = ImageDraw.Draw(frame)
        if region:
            draw.rectangle(region, outline=ZONE_COLOR, width=1)
        for track in tracks:
            color = COUNTED_COLOR if track.track_id in counted_ids else TRACK_COLOR
            draw.rectangle(track.box, outline=color, width=3)
            if len(track.path) > 1:
                draw.line([p for _, p in track.path[-15:]], fill=color, width=2)
            _label(draw, (track.box[0], track.box[1] - 22), f"#{track.track_id} {track.vehicle_class}", self.font, color)
        draw_lines(draw, lines, self.font, totals)
        _label(draw, (8, frame.height - 30), clock, self.font)
        self.proc.stdin.write(frame.tobytes())

    def close(self) -> None:
        self.proc.stdin.close()
        self.proc.wait()


# --- Counting --------------------------------------------------------------------


def count_video(
    frames: Iterable[Image.Image],
    lines: list,
    detect: Callable,
    fps: float,
    *,
    region: Optional[tuple] = None,
    preview: Optional[PreviewWriter] = None,
    clock: Callable = lambda seconds: f"{seconds:.1f}s",
    progress: Optional[Callable] = None,
) -> list:
    """Runs detect -> track -> count over every frame. Returns the crossings."""
    tracker = VehicleTracker()
    counter = LineCounter(lines)
    crossings: list = []
    counted_ids: set = set()
    for index, frame in enumerate(frames):
        now = index / fps
        detections = detect(frame, region)
        seen = tracker.update(detections, now)
        new = counter.update(seen, now)
        crossings.extend(new)
        counted_ids.update(c.track_id for c in new)
        counter.forget_missing(t.track_id for t in tracker.tracks)
        if preview is not None:
            preview.write(frame, lines, seen, counted_ids, counter.totals, region, clock(now))
        if progress is not None:
            progress(index + 1, now)
    return crossings


def bin_crossings(crossings: list, lines: list, bin_seconds: float, total_seconds: float) -> list:
    """Rows of {bin, line, direction, per-class counts, total}, including
    empty blocks so the table has no holes."""
    bins = max(1, math.ceil(total_seconds / bin_seconds)) if total_seconds else 1
    table = {}
    for b in range(bins):
        for line in lines:
            for direction in ("forward", "reverse"):
                table[(b, line.line_id, direction)] = {c: 0 for c in VEHICLE_CLASSES}
    for c in crossings:
        b = min(int(c.time // bin_seconds), bins - 1)
        table[(b, c.line_id, c.direction)][c.vehicle_class] += 1
    rows = []
    for (b, line_id, direction), counts in sorted(table.items(), key=lambda kv: (kv[0][0], kv[0][1], kv[0][2])):
        line = next(l for l in lines if l.line_id == line_id)
        rows.append({
            "bin": b, "line": line.name, "line_id": line_id, "direction": line.label_for(direction),
            **counts, "total": sum(counts.values()),
        })
    return rows


def summarize(crossings: list, lines: list, rows: list, bin_seconds: float, bin_label: Callable) -> str:
    block = f"{bin_seconds / 60:g} min"
    out = []
    for line in lines:
        mine = [c for c in crossings if c.line_id == line.line_id]
        out.append(f"{line.name}: {len(mine)} vehicles")
        for direction in ("forward", "reverse"):
            d = [c for c in mine if c.direction == direction]
            by_class = ", ".join(
                f"{sum(1 for c in d if c.vehicle_class == k)} {k}" for k in VEHICLE_CLASSES
                if any(c.vehicle_class == k for c in d)
            )
            out.append(f"  {line.label_for(direction)}: {len(d)}" + (f"  ({by_class})" if by_class else ""))
        per_bin = {}
        for row in rows:
            if row["line_id"] == line.line_id:
                per_bin[row["bin"]] = per_bin.get(row["bin"], 0) + row["total"]
        if per_bin and max(per_bin.values()) > 0:
            peak_bin = max(per_bin, key=lambda b: (per_bin[b], -b))
            out.append(f"  busiest {block}: {bin_label(peak_bin)} with {per_bin[peak_bin]}")
            per_hour_blocks = max(1, int(round(3600 / bin_seconds)))
            if len(per_bin) >= per_hour_blocks:
                best = max(
                    range(len(per_bin) - per_hour_blocks + 1),
                    key=lambda s: (sum(per_bin.get(s + k, 0) for k in range(per_hour_blocks)), -s),
                )
                total = sum(per_bin.get(best + k, 0) for k in range(per_hour_blocks))
                out.append(f"  busiest hour: starting {bin_label(best).split('-')[0]} with {total}")
    return "\n".join(out)


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(description="Count vehicles crossing lines in a video.")
    parser.add_argument("video", type=Path)
    parser.add_argument("--line", action="append", default=[], help='"Name:x1,y1,x2,y2:forward/reverse" (repeatable)')
    parser.add_argument("--snapshot", action="store_true", help="save the first frame with a grid (and lines) and stop")
    parser.add_argument("--fps", type=float, default=DEFAULT_FPS, help="photos per second of video to check (default 5)")
    parser.add_argument("--model", choices=("nano", "small", "medium", "large"), default="medium")
    parser.add_argument("--confidence", type=float, default=DEFAULT_CONFIDENCE)
    parser.add_argument("--max-width", type=int, default=DEFAULT_MAX_WIDTH, help="shrink wider videos to this (default 1280)")
    parser.add_argument("--start", help='real clock time the video starts, e.g. "2026-09-27 14:00"')
    parser.add_argument("--bin-minutes", type=float, default=15.0)
    parser.add_argument("--seconds", type=float, help="only look at the first N seconds")
    parser.add_argument(
        "--crop", action="store_true",
        help="look only at the area around the lines (helps for big, high-res pictures with small far-away "
             "vehicles; off by default because at normal video sizes it cuts off big nearby vehicles)",
    )
    parser.add_argument("--no-preview", action="store_true", help="skip writing preview.mp4 (a bit faster)")
    parser.add_argument("--out", type=Path, help="output folder")
    args = parser.parse_args(argv)

    if not args.video.exists():
        parser.error(f"{args.video} doesn't exist")
    if not 0.5 <= args.fps <= 30:
        parser.error("--fps must be between 0.5 and 30")
    info = probe_video(args.video)
    size = output_size(info["width"], info["height"], args.max_width)
    # Lines given in pixels refer to the ORIGINAL video size; fractions don't care.
    scale = size[0] / info["width"]
    try:
        lines = []
        for i, spec in enumerate(args.line, start=1):
            line = parse_line_spec(spec, (info["width"], info["height"]), i)
            line.p1 = (line.p1[0] * scale, line.p1[1] * scale)
            line.p2 = (line.p2[0] * scale, line.p2[1] * scale)
            lines.append(line)
    except ValueError as exc:
        parser.error(str(exc))
    # Off by default: the first real test (a 960x540 street video) showed
    # the crop zone cutting big near-lane vehicles in half, so they were
    # missed. Cropping pays off only for large, high-res frames where the
    # vehicles near the line are small.
    region = crop_region_for_lines(lines, size) if args.crop else None

    stem = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in args.video.stem)[:40]
    out_dir = args.out or RUNS_DIR / f"{stem}_{datetime.now().strftime('%Y%m%d-%H%M%S')}"
    out_dir.mkdir(parents=True, exist_ok=True)

    if args.snapshot:
        first = next(read_frames(args.video, args.fps, size, 1.0), None)
        if first is None:
            raise SystemExit("Couldn't read a frame from the video.")
        path = out_dir / "snapshot_grid.jpg"
        snapshot_image(first, lines, region).save(path, quality=90)
        print(f"Saved {path}")
        print("Grid labels are fractions of the picture: 0.0 = left/top, 1.0 = right/bottom.")
        return 0

    if not lines:
        parser.error("add at least one --line (run with --snapshot first to see the picture)")

    start_at = None
    if args.start:
        try:
            start_at = datetime.fromisoformat(args.start.replace("T", " "))
        except ValueError:
            parser.error('--start must look like "2026-09-27 14:00"')

    bin_seconds = args.bin_minutes * 60

    def clock(seconds: float) -> str:
        if start_at:
            return (start_at + timedelta(seconds=seconds)).strftime("%Y-%m-%d %H:%M:%S")
        return f"{int(seconds // 3600):02d}:{int(seconds % 3600 // 60):02d}:{seconds % 60:04.1f}"

    with_seconds = bin_seconds % 60 != 0

    def bin_label(b: int) -> str:
        a, z = b * bin_seconds, (b + 1) * bin_seconds
        if start_at:
            fmt = "%H:%M:%S" if with_seconds else "%H:%M"
            return f"{(start_at + timedelta(seconds=a)).strftime(fmt)}-{(start_at + timedelta(seconds=z)).strftime(fmt)}"

        def hms(x: float) -> str:
            text = f"{int(x // 3600):02d}:{int(x % 3600 // 60):02d}"
            return text + f":{int(x % 60):02d}" if with_seconds else text

        return f"{hms(a)}-{hms(z)}"

    video_seconds = min(filter(None, [info["duration"], args.seconds])) if (info["duration"] or args.seconds) else None
    expected = int(video_seconds * args.fps) if video_seconds else None
    print(f"Video {info['width']}x{info['height']} -> checking {size[0]}x{size[1]} at {args.fps:g} photos/sec"
          + (f", about {expected} photos" if expected else ""))
    if region:
        print(f"Looking only at the area around the lines: {region}")
    print(f"Loading the {args.model} model...")
    detect = make_rfdetr_detector(args.model, args.confidence)

    preview = None if args.no_preview else PreviewWriter(out_dir / "preview.mp4", size, args.fps)
    began = time.monotonic()
    last_print = [0.0]

    def progress(done: int, now: float) -> None:
        if time.monotonic() - last_print[0] >= 10:
            last_print[0] = time.monotonic()
            pct = f" ({100 * done / expected:.0f}%)" if expected else ""
            print(f"  {done} photos{pct}, {now:.0f}s into the video")

    try:
        crossings = count_video(
            read_frames(args.video, args.fps, size, args.seconds), lines, detect, args.fps,
            region=region, preview=preview, clock=clock, progress=progress,
        )
    finally:
        if preview is not None:
            preview.close()
    elapsed = time.monotonic() - began
    processed_seconds = video_seconds or 0.0

    with open(out_dir / "crossings.csv", "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["time", "seconds_into_video", "line", "direction", "vehicle_type", "vehicle_id"])
        for c in crossings:
            writer.writerow([clock(c.time), f"{c.time:.1f}", c.line_name, c.direction_label, c.vehicle_class, c.track_id])

    rows = bin_crossings(crossings, lines, bin_seconds, processed_seconds)
    with open(out_dir / "counts_15min.csv" if args.bin_minutes == 15 else out_dir / "counts.csv", "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["period", "line", "direction", *VEHICLE_CLASSES, "total"])
        for row in rows:
            writer.writerow([bin_label(row["bin"]), row["line"], row["direction"], *[row[k] for k in VEHICLE_CLASSES], row["total"]])

    summary = summarize(crossings, lines, rows, bin_seconds, bin_label)
    speed = (processed_seconds / elapsed) if elapsed > 0 and processed_seconds else 0
    footer = (
        f"\nChecked {processed_seconds:.0f}s of video in {elapsed:.0f}s "
        f"({speed:.1f}x real time at {args.fps:g} photos/sec, {args.model} model)."
    )
    if speed and speed < 1:
        footer += "\nNote: slower than real time -- a live camera on this computer would need fewer photos/sec or a smaller model."
    (out_dir / "summary.txt").write_text(summary + footer + "\n")
    print()
    print(summary + footer)
    print(f"\nResults in {out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
