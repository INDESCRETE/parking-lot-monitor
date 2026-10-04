"""One-off speed check: how long does one frame take to run through the
detection model on THIS machine? Run it yourself with:

    .venv/bin/python scripts/benchmark_detection_speed.py

(or `python3 scripts/benchmark_detection_speed.py` if that's how you
normally run things). It doesn't touch the database or save anything --
it just loads the model, times it on a real photo a few times, and
prints the results.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]

# Reuse one of the real sample photos already in the repo.
SAMPLE_DIRS = [ROOT / "data" / "images" / "camera_1", ROOT / "data" / "live" / "reolink_live"]


def find_sample_image() -> Path:
    for d in SAMPLE_DIRS:
        if d.is_dir():
            candidates = sorted(d.glob("*.jpg")) + sorted(d.glob("*.jpeg")) + sorted(d.glob("*.png"))
            if candidates:
                return candidates[0]
    raise SystemExit("Couldn't find a sample photo to test with -- expected one under data/images/camera_1/")


def main() -> None:
    image_path = find_sample_image()
    print(f"Using sample photo: {image_path.relative_to(ROOT)}")

    try:
        import torch
        print(f"torch {torch.__version__}  |  MPS (Apple GPU) available: {torch.backends.mps.is_available()}  |  CUDA available: {torch.cuda.is_available()}")
    except Exception as exc:  # pragma: no cover
        print(f"(couldn't check torch/GPU info: {exc})")

    print("Loading model (this part is slow and only happens once when the server starts)...")
    t0 = time.perf_counter()
    from rfdetr import RFDETRMedium
    model = RFDETRMedium()
    load_seconds = time.perf_counter() - t0
    print(f"Model load time: {load_seconds:.1f}s")

    with Image.open(image_path) as im:
        rgb_image = im.convert("RGB")

    n_runs = 6
    times = []
    print(f"Running {n_runs} timed detections on the sample photo...")
    for i in range(n_runs):
        t0 = time.perf_counter()
        model.predict(rgb_image, threshold=0.5)
        elapsed = time.perf_counter() - t0
        times.append(elapsed)
        print(f"  run {i + 1}: {elapsed:.2f}s" + ("  (first run, includes warmup)" if i == 0 else ""))

    warm_times = times[1:]  # skip the first run, it's usually slower (warmup/compile)
    avg_warm = sum(warm_times) / len(warm_times)
    print()
    print(f"Average time per frame after warmup: {avg_warm:.2f}s")
    print(f"That means checking a frame every 3 seconds is " + ("FINE -- plenty of headroom." if avg_warm < 2.0 else ("CUTTING IT CLOSE." if avg_warm < 3.0 else "TOO SLOW as-is -- we'll need to either slow down the check interval or use a smaller/faster model.")))


if __name__ == "__main__":
    main()
