from __future__ import annotations

import subprocess
import time
from datetime import datetime, timedelta
from pathlib import Path


class FfmpegUnavailable(RuntimeError):
    """Raised when the ffmpeg binary needed for video processing isn't installed."""


def get_ffmpeg_exe() -> str:
    try:
        import imageio_ffmpeg
    except ImportError as exc:
        raise FfmpegUnavailable(
            "Video import needs the 'imageio-ffmpeg' package on the server. "
            "Install it with: pip install imageio-ffmpeg"
        ) from exc
    return imageio_ffmpeg.get_ffmpeg_exe()


def extract_frames(
    video_path: Path,
    output_dir: Path,
    interval: float,
    start_time: datetime,
    end_seconds: float | None = None,
    prefix: str | None = None,
) -> list[Path]:
    """Extract still frames from a video file at a fixed interval.

    Frames are written into output_dir with timestamp-encoded filenames so
    they sort chronologically and the app's captured_at parser picks up a
    sensible time for each one. Returns the list of frame paths written.
    """
    if interval <= 0:
        raise ValueError("interval must be greater than 0 seconds")

    output_dir.mkdir(parents=True, exist_ok=True)
    stem = prefix or video_path.stem
    temp_dir = output_dir / f".{stem}_frames_{int(time.time())}"
    temp_dir.mkdir()
    output_pattern = temp_dir / "frame_%06d.jpg"

    ffmpeg = get_ffmpeg_exe()
    command = [ffmpeg, "-hide_banner", "-loglevel", "error", "-i", str(video_path)]
    if end_seconds is not None:
        if end_seconds <= 0:
            raise ValueError("end_seconds must be greater than 0")
        command += ["-to", str(end_seconds)]
    command += ["-vf", f"fps=1/{interval}", "-q:v", "2", str(output_pattern)]

    try:
        subprocess.run(command, check=True, capture_output=True, text=True)
    except subprocess.CalledProcessError as exc:
        temp_dir_cleanup(temp_dir)
        raise RuntimeError(f"ffmpeg failed: {exc.stderr or exc}") from exc

    frames = sorted(temp_dir.glob("frame_*.jpg"))
    written: list[Path] = []
    for index, frame in enumerate(frames):
        captured_at = start_time + timedelta(seconds=index * interval)
        timestamp = captured_at.strftime("%Y%m%dT%H%M%SZ")
        target = output_dir / f"{stem}_{timestamp}_{index + 1:06d}.jpg"
        if target.exists():
            target.unlink()
        frame.rename(target)
        written.append(target)
    temp_dir_cleanup(temp_dir)
    return written


def temp_dir_cleanup(temp_dir: Path) -> None:
    for leftover in temp_dir.glob("*"):
        leftover.unlink(missing_ok=True)
    temp_dir.rmdir()
