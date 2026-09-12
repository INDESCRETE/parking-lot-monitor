from __future__ import annotations

import argparse
import re
import shutil
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SOURCE = ROOT / "data" / "images" / "camera_1"
SEQUENCE_RE = re.compile(r"_(\d{6})\.jpg$")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Move sampled frame images into camera folders by frame-number ranges."
    )
    parser.add_argument(
        "--source",
        type=Path,
        default=DEFAULT_SOURCE,
        help="Folder containing the extracted frame images",
    )
    parser.add_argument(
        "splits",
        nargs="+",
        help="Ranges in the form camera_id:start-end, for example camera_2:17-24",
    )
    return parser.parse_args()


def parse_split(value: str) -> tuple[str, range]:
    camera_id, bounds = value.split(":", 1)
    start, end = bounds.split("-", 1)
    return camera_id, range(int(start), int(end) + 1)


def frame_number(path: Path) -> int | None:
    match = SEQUENCE_RE.search(path.name)
    return int(match.group(1)) if match else None


def main() -> None:
    args = parse_args()
    split_ranges = [(camera_id, numbers) for camera_id, numbers in map(parse_split, args.splits)]
    moved = {camera_id: 0 for camera_id, _ in split_ranges}

    for path in sorted(args.source.glob("*.jpg")):
        number = frame_number(path)
        if number is None:
            continue

        target_camera = None
        for camera_id, numbers in split_ranges:
            if number in numbers:
                target_camera = camera_id
                break

        if target_camera is None:
            continue

        target_dir = args.source.parent / target_camera
        target_dir.mkdir(parents=True, exist_ok=True)
        (target_dir / ".gitkeep").touch(exist_ok=True)
        shutil.move(str(path), target_dir / path.name)
        moved[target_camera] += 1

    for camera_id, count in moved.items():
        print(f"{camera_id}: moved {count} frames")


if __name__ == "__main__":
    main()
