from __future__ import annotations

import argparse
import shutil
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import imageio_ffmpeg
from yt_dlp import YoutubeDL


ROOT = Path(__file__).resolve().parents[1]
DOWNLOAD_DIR = ROOT / "data" / "source_videos"
DEFAULT_OUTPUT_DIR = ROOT / "data" / "images" / "camera_1"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Download a YouTube video and extract still frames at a fixed interval."
    )
    parser.add_argument("url", help="YouTube video URL")
    parser.add_argument(
        "--video-file",
        type=Path,
        help="Use an existing local video file instead of downloading from the URL",
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=10,
        help="Seconds between extracted frames",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help="Directory where extracted frames will be written",
    )
    parser.add_argument(
        "--max-height",
        type=int,
        default=720,
        help="Prefer a source video no taller than this many pixels",
    )
    parser.add_argument(
        "--start-time",
        default="2026-01-01T00:00:00Z",
        help="Synthetic timestamp assigned to the first extracted frame",
    )
    return parser.parse_args()


def download_video(url: str, max_height: int) -> Path:
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    options = {
        "format": f"best[ext=mp4][height<={max_height}]/best[height<={max_height}]/best",
        "outtmpl": str(DOWNLOAD_DIR / "%(id)s.%(ext)s"),
        "noplaylist": True,
        "quiet": False,
        "restrictfilenames": True,
        "extractor_args": {"youtube": {"player_client": ["android"]}},
    }
    with YoutubeDL(options) as ydl:
        info = ydl.extract_info(url, download=False)
        existing = sorted(DOWNLOAD_DIR.glob(f"{info['id']}.*"))
        if existing:
            return existing[0]

        info = ydl.extract_info(url, download=True)
        downloaded = Path(ydl.prepare_filename(info))
        if downloaded.exists():
            return downloaded

        # yt-dlp may adjust the final extension after post-processing.
        candidates = sorted(DOWNLOAD_DIR.glob(f"{info['id']}.*"))
        if candidates:
            return candidates[0]
    raise FileNotFoundError("Downloaded video file was not found.")


def parse_start_time(value: str) -> datetime:
    normalized = value.replace("Z", "+00:00")
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def extract_frames(video_path: Path, output_dir: Path, interval: int, start_time: datetime) -> int:
    output_dir.mkdir(parents=True, exist_ok=True)
    video_id = video_path.stem
    for previous in output_dir.glob(f"{video_id}_*.jpg"):
        previous.unlink()

    temp_dir = output_dir / f".{video_id}_frames"
    if temp_dir.exists():
        shutil.rmtree(temp_dir)
    temp_dir.mkdir()
    output_pattern = temp_dir / "frame_%06d.jpg"
    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    command = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(video_path),
        "-vf",
        f"fps=1/{interval}",
        "-q:v",
        "2",
        str(output_pattern),
    ]
    subprocess.run(command, check=True)

    frames = sorted(temp_dir.glob("frame_*.jpg"))
    for index, frame in enumerate(frames):
        captured_at = start_time + timedelta(seconds=index * interval)
        timestamp = captured_at.strftime("%Y%m%dT%H%M%SZ")
        target = output_dir / f"{video_id}_{timestamp}_{index + 1:06d}.jpg"
        if target.exists():
            target.unlink()
        frame.rename(target)
    temp_dir.rmdir()
    return len(frames)


def main() -> None:
    args = parse_args()
    if args.interval < 1:
        raise ValueError("--interval must be at least 1 second")

    video_path = args.video_file if args.video_file else download_video(args.url, args.max_height)
    if not video_path.exists():
        raise FileNotFoundError(f"Video file not found: {video_path}")
    count = extract_frames(
        video_path,
        args.output_dir,
        args.interval,
        parse_start_time(args.start_time),
    )
    print(f"Downloaded: {video_path}")
    print(f"Extracted {count} frames into: {args.output_dir}")

    if shutil.which("python3"):
        print("Run the POC with: python3 -B -m app.server")


if __name__ == "__main__":
    main()
