#!/usr/bin/env python3
"""Checks that we can get pictures from a camera, before setting it up.

Use it on-site: plug the box into the lot's network, get the camera's (or
recorder's) address and login from the owner, and run one of:

    python3 scripts/test_camera.py rtsp://192.168.1.64:554/Streaming/Channels/101 --user admin
    python3 scripts/test_camera.py http://192.168.1.64/ISAPI/Streaming/channels/101/picture --user admin
    python3 scripts/test_camera.py --camera reolink_live      (one already in data/camera_sources.json)
    python3 scripts/test_camera.py --brands                   (common addresses by brand)

The password is asked for without showing it on screen. It grabs a few
pictures, reports how long they took and their resolution, and saves the
last one as camera_test.jpg so you can see what the camera sees. Nothing
is changed on the camera, and nothing is saved to the settings file.
"""

from __future__ import annotations

import argparse
import getpass
import io
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app import live  # noqa: E402

BRANDS = """Common addresses (replace IP with the camera's or recorder's address).
"Main" = full resolution (use for detection); "sub" = small, for Live View.

Hikvision (also rebrands such as LTS, Annke and some Lorex)
  main  rtsp://IP:554/Streaming/Channels/101      sub  .../102
  NVR camera N: rtsp://NVR-IP:554/Streaming/Channels/N01   (camera 3 = 301)
  snapshot  http://IP/ISAPI/Streaming/channels/101/picture

Dahua (also Amcrest, many Lorex, IC Realtime)
  main  rtsp://IP:554/cam/realmonitor?channel=1&subtype=0    sub  ...&subtype=1
  NVR camera N: same address on the NVR with channel=N
  snapshot  http://IP/cgi-bin/snapshot.cgi?channel=1

Reolink
  main  rtsp://IP:554/h264Preview_01_main   sub  .../h264Preview_01_sub
  (or leave "type" out and use the Reolink settings as today)

Axis
  rtsp://IP/axis-media/media.amp        snapshot  http://IP/axis-cgi/jpg/image.cgi

Uniview
  main  rtsp://IP:554/unicast/c1/s0/live    sub  .../c1/s1/live

Ubiquiti UniFi Protect
  Turn on RTSP per camera in Protect (camera > Settings > Advanced), then
  copy the rtsps://...:7441/... address it shows. No login needed.

Unknown brand: look up "<brand> <model> RTSP URL", or check the camera's or
recorder's network settings page. RTSP must be turned on there, usually on
port 554.
"""


def _describe(jpeg: bytes) -> str:
    try:
        from PIL import Image

        width, height = Image.open(io.BytesIO(jpeg)).size
        return f"{width}x{height}, {len(jpeg) // 1024} KB"
    except Exception:
        return f"{len(jpeg) // 1024} KB"


def main() -> int:
    parser = argparse.ArgumentParser(description="Check we can get pictures from a camera.")
    parser.add_argument("address", nargs="?", help="rtsp://... or http(s)://... address")
    parser.add_argument("--user", default="", help="camera/recorder username")
    parser.add_argument("--password", default=None, help="(better: leave out and type it when asked)")
    parser.add_argument("--camera", help="test a camera already in data/camera_sources.json")
    parser.add_argument("--all-frames", action="store_true", help="RTSP: decode every frame instead of key frames only")
    parser.add_argument("--count", type=int, default=3, help="how many pictures to grab (default 3)")
    parser.add_argument("--brands", action="store_true", help="show common addresses by camera brand")
    args = parser.parse_args()

    if args.brands:
        print(BRANDS)
        return 0

    if args.camera:
        try:
            sources = live.load_config()
        except live.LiveConfigError as exc:
            print(f"Settings file problem: {exc}")
            return 1
        source = sources.get(args.camera)
        if source is None:
            print(f"No camera '{args.camera}' in data/camera_sources.json. Found: {', '.join(sources) or 'none'}")
            return 1
    elif args.address:
        password = args.password
        if password is None and args.user:
            password = getpass.getpass(f"Password for {args.user}: ")
        entry = {"username": args.user, "password": password or ""}
        if args.address.startswith(("rtsp://", "rtsps://")):
            entry.update(type="rtsp", rtsp_url=args.address, rtsp_decode="all" if args.all_frames else "keyframes")
        elif args.address.startswith(("http://", "https://")):
            entry.update(type="http_snapshot", snapshot_url=args.address)
        else:
            print("The address must start with rtsp://, rtsps://, http:// or https://")
            return 1
        try:
            source = live.parse_config({"test": entry})["test"]
        except live.LiveConfigError as exc:
            print(str(exc).replace("test: ", ""))
            return 1
    else:
        parser.print_help()
        return 1

    print(f"Testing {source.type} camera at {source.host} ...")
    fetcher = live.make_fetcher(source)
    last = None
    ok = 0
    try:
        for i in range(args.count):
            started = time.monotonic()
            try:
                last = fetcher.fetch()
            except live.LiveFrameError as exc:
                print(f"  picture {i + 1}: FAILED - {exc}")
                break
            ok += 1
            print(f"  picture {i + 1}: {_describe(last)} in {time.monotonic() - started:.1f} s")
            if source.type != "rtsp" and i + 1 < args.count:
                time.sleep(1)
    finally:
        fetcher.close()

    if last:
        out = Path("camera_test.jpg").resolve()
        out.write_bytes(last)
        print(f"Saved the last picture to {out}")
    if ok == args.count:
        print("OK: this camera works. Add it to data/camera_sources.json (see camera_sources.example.json).")
        return 0
    print("Not working yet. Run with --brands for common addresses, and check the login.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
