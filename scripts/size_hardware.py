"""How big a computer does this software actually need?

Times the vehicle-detection model on real photos from your cameras while
pretending to be a smaller computer:
  * CPU only (no Apple graphics chip -- cheap mini PCs don't have one)
  * limited to 2 or 4 processor cores
  * three model sizes: nano (smallest/fastest), small, medium (what we use now)

Then turns the results into a plain answer: roughly how many cameras a
budget mini PC could keep up with.

STOP THE SERVER FIRST (Control + C in its Terminal window) so it isn't
competing for the processor and skewing the numbers. Then run:

    python3 scripts/size_hardware.py

(or .venv/bin/python scripts/size_hardware.py). Takes about 5-10 minutes.
It doesn't touch the database. The first run downloads the "small" model
file (~100 MB) if it isn't already here.
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import resource
import statistics
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

N_PHOTOS = 10  # timed photos per setup
N_WARMUP = 2  # untimed runs first (the first couple are always slow)
CONFIDENCE = 0.25  # same threshold the live detector uses
VEHICLE_CLASS_IDS = {3, 4, 6, 8}  # car, motorcycle, bus, truck

# Rough per-core speed of this Mac vs. a budget Intel mini PC (N100/N150
# class, ~$150-250). Apple M-series cores are roughly 2x faster per core on
# this kind of work; 2.5 leaves a safety margin. It's an estimate -- the
# only exact answer is running this same script on the real machine.
BUDGET_PC_SLOWDOWN = 2.5
# Don't plan to run the processor flat out 24/7 -- leave 30% spare.
MAX_BUSY = 0.70

SETUPS = [
    # (label, model, device, threads)
    ("Your Mac, graphics chip (today's setup)", "medium", "mps", None),
    ("Medium model, 4 cores", "medium", "cpu", 4),
    ("Medium model, 2 cores", "medium", "cpu", 2),
    ("Small model, 4 cores", "small", "cpu", 4),
    ("Small model, 2 cores", "small", "cpu", 2),
    ("Nano model, 4 cores", "nano", "cpu", 4),
    ("Nano model, 2 cores", "nano", "cpu", 2),
]


def pick_photos() -> list[Path]:
    photos: list[Path] = []
    live = ROOT / "data" / "live" / "reolink_live" / "latest.jpg"
    if live.exists():
        photos.append(live)
    images = ROOT / "data" / "images"
    if images.is_dir():
        for cam in sorted(p for p in images.iterdir() if p.is_dir()):
            files = sorted(cam.glob("*.jpg")) + sorted(cam.glob("*.png"))
            if not files:
                continue
            step = max(1, len(files) // 3)
            photos.extend(files[::step][:3])  # a few spread-out photos per camera
    if not photos:
        raise SystemExit("Couldn't find any camera photos under data/ to test with.")
    return photos[:N_PHOTOS]


# ---------------------------------------------------------------- worker ---
def worker(model_size: str, device: str, threads: int | None, photo_paths: list[str]) -> None:
    if threads:
        for var in ("OMP_NUM_THREADS", "MKL_NUM_THREADS", "VECLIB_MAXIMUM_THREADS"):
            os.environ[var] = str(threads)
    import torch

    if threads:
        torch.set_num_threads(threads)
    from PIL import Image
    from rfdetr import RFDETRMedium, RFDETRNano, RFDETRSmall

    cls = {"nano": RFDETRNano, "small": RFDETRSmall, "medium": RFDETRMedium}[model_size]
    t0 = time.perf_counter()
    model = cls(device=device)
    load_s = time.perf_counter() - t0

    def check(path: str) -> int:
        # Includes opening/decoding the photo, like the live detector does.
        with Image.open(path) as im:
            rgb = im.convert("RGB")
        result = model.predict(rgb, threshold=CONFIDENCE)
        return sum(1 for c in result.class_id if int(c) in VEHICLE_CLASS_IDS)

    for i in range(N_WARMUP):
        check(photo_paths[i % len(photo_paths)])

    times, counts = [], []
    for path in photo_paths:
        t0 = time.perf_counter()
        counts.append(check(path))
        times.append(time.perf_counter() - t0)

    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    peak_mb = peak / (1024 * 1024) if sys.platform == "darwin" else peak / 1024
    print("RESULT " + json.dumps({
        "load_s": load_s,
        "median_s": statistics.median(times),
        "max_s": max(times),
        "peak_mb": peak_mb,
        "counts": counts,
    }), flush=True)


# ------------------------------------------------------------ controller ---
def run_setup(model_size: str, device: str, threads: int | None, photos: list[Path]) -> dict | None:
    cmd = [sys.executable, str(Path(__file__).resolve()), "--worker",
           "--model", model_size, "--device", device, "--threads", str(threads or 0),
           "--photos", json.dumps([str(p) for p in photos])]
    env = dict(os.environ)
    if device == "mps":
        env["PYTORCH_ENABLE_MPS_FALLBACK"] = "1"
    proc = subprocess.run(cmd, cwd=str(ROOT), env=env, capture_output=True, text=True)
    for line in proc.stdout.splitlines():
        if line.startswith("RESULT "):
            return json.loads(line[len("RESULT "):])
    tail = (proc.stderr or proc.stdout).strip().splitlines()[-3:]
    print("    (this setup failed: " + " | ".join(tail) + ")")
    return None


def cameras_supported(seconds_per_photo: float, interval: float) -> float:
    return (interval * MAX_BUSY) / seconds_per_photo


def main() -> None:
    photos = pick_photos()
    try:
        chip = subprocess.run(["sysctl", "-n", "machdep.cpu.brand_string"],
                              capture_output=True, text=True).stdout.strip()
    except Exception:
        chip = ""
    print(f"Computer: {chip or platform.processor() or platform.machine()}  |  Python {platform.python_version()}")
    print(f"Testing on {len(photos)} real photos from your cameras.\n")

    import torch  # only to check whether the Mac graphics chip is usable
    mps_ok = bool(getattr(torch.backends, "mps", None)) and torch.backends.mps.is_available()

    results = []
    for label, model_size, device, threads in SETUPS:
        if device == "mps" and not mps_ok:
            continue
        print(f"-> {label} ...", flush=True)
        r = run_setup(model_size, device, threads, photos)
        if r:
            print(f"    {r['median_s']:.2f} s per photo (slowest {r['max_s']:.2f} s), "
                  f"memory {r['peak_mb']:.0f} MB, cars found per photo {r['counts']}")
            results.append((label, model_size, device, threads, r))

    if not results:
        raise SystemExit("\nNo setup finished -- paste the messages above to Claude.")

    # Accuracy check: how closely does each smaller setup's car count match
    # the medium model's (our current, most accurate choice)?
    reference = next((r["counts"] for _, m, _, _, r in results if m == "medium"), None)

    print("\n" + "=" * 78)
    print("WHAT THIS MEANS FOR A BUDGET MINI PC  (N100/N150 class, about $150-250)")
    print("Estimated cameras one box can keep up with, checking each camera every:")
    print("=" * 78)
    print(f"{'Setup':34} {'every 3s':>9} {'every 10s':>10} {'every 15s':>10}  {'cars vs medium':>14}")
    for label, model_size, device, threads, r in results:
        if device != "cpu":
            continue
        budget_s = r["median_s"] * BUDGET_PC_SLOWDOWN
        cams = [cameras_supported(budget_s, iv) for iv in (3, 10, 15)]
        if reference:
            diff = sum(abs(a - b) for a, b in zip(r["counts"], reference))
            match = f"{diff} off / {sum(reference)}"
        else:
            match = "-"
        print(f"{label:34} {cams[0]:>9.1f} {cams[1]:>10.1f} {cams[2]:>10.1f}  {match:>14}")

    print("\nNotes:")
    print(f"  * Budget-PC numbers assume it's {BUDGET_PC_SLOWDOWN}x slower per core than this Mac")
    print(f"    and never run more than {int(MAX_BUSY * 100)}% busy. Treat them as estimates.")
    print("  * Below 1.0 means that box can't even keep up with one camera at that speed.")
    print("  * 'cars vs medium' = how many cars the smaller model missed or added across")
    print("    all test photos compared to the medium model. Small numbers = about as accurate.")
    print("\nCopy everything above and paste it to Claude.")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--worker", action="store_true")
    ap.add_argument("--model")
    ap.add_argument("--device")
    ap.add_argument("--threads", type=int, default=0)
    ap.add_argument("--photos")
    a = ap.parse_args()
    if a.worker:
        worker(a.model, a.device, a.threads or None, json.loads(a.photos))
    else:
        main()
