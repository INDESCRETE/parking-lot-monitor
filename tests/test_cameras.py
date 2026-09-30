"""Tests for the camera types in app/live.py (reolink / rtsp / http_snapshot).

Run from the repo root:  python3 -m unittest tests.test_cameras -v
Needs ffmpeg for the stream tests (skipped if it can't be found).
No real camera needed: a generated test video stands in for an RTSP stream,
and a tiny local web server stands in for a snapshot camera.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from app import live

FAKE_JPEG = b"\xff\xd8\xff\xe0" + b"fake-jpeg-body" * 10 + b"\xff\xd9"


def _ffmpeg() -> str | None:
    try:
        path = live.find_ffmpeg()
    except Exception:
        return None
    return path if shutil.which(path) or os.path.exists(path) else None


class ConfigTests(unittest.TestCase):
    def test_existing_reolink_config_unchanged(self):
        src = live.parse_config({"cam": {"host": "192.168.1.50", "username": "admin", "password": "pw"}})["cam"]
        self.assertEqual(src.type, "reolink")
        self.assertIn("cmd=Snap", src.snapshot_url())
        self.assertEqual(src.live_view_rtsp_url(), "rtsp://admin:pw@192.168.1.50:554/Preview_01_sub")
        self.assertIsInstance(live.make_fetcher(src), live.ReolinkFetcher)

    def test_rtsp_login_moved_out_of_address(self):
        src = live.parse_config({"cam": {"type": "rtsp", "rtsp_url": "rtsp://admin:s3cr%40t@10.0.0.9:554/Streaming/Channels/101"}})["cam"]
        self.assertEqual(src.stream_url, "rtsp://10.0.0.9:554/Streaming/Channels/101")
        self.assertEqual((src.username, src.password), ("admin", "s3cr@t"))
        self.assertEqual(src.host, "10.0.0.9")
        self.assertEqual(src.detection_rtsp_url(), "rtsp://admin:s3cr%40t@10.0.0.9:554/Streaming/Channels/101")
        self.assertNotIn("s3cr", repr(src))
        self.assertIsInstance(live.make_fetcher(src), live.RtspFetcher)

    def test_rtsp_with_separate_login_and_live_substream(self):
        src = live.parse_config({"cam": {
            "type": "rtsp", "username": "admin", "password": "pw",
            "rtsp_url": "rtsp://10.0.0.9/cam/realmonitor?channel=1&subtype=0",
            "live_rtsp_url": "rtsp://10.0.0.9/cam/realmonitor?channel=1&subtype=1",
        }})["cam"]
        self.assertEqual(src.detection_rtsp_url(), "rtsp://admin:pw@10.0.0.9/cam/realmonitor?channel=1&subtype=0")
        self.assertEqual(src.live_view_rtsp_url(), "rtsp://admin:pw@10.0.0.9/cam/realmonitor?channel=1&subtype=1")

    def test_http_snapshot_without_stream_has_no_live_view(self):
        src = live.parse_config({"cam": {"type": "http_snapshot", "snapshot_url": "http://10.0.0.7/cgi-bin/snapshot.cgi"}})["cam"]
        self.assertIsNone(src.live_view_rtsp_url())
        with self.assertRaises(live.RelayFailed):
            live.build_mjpeg_command(src)

    def test_bad_configs_explain_themselves(self):
        cases = {
            "type": {"type": "webcam"},
            "rtsp_url": {"type": "rtsp"},
            "rtsp://": {"type": "rtsp", "rtsp_url": "http://10.0.0.1/x"},
            "snapshot_url": {"type": "http_snapshot"},
            "rtsp_decode": {"type": "rtsp", "rtsp_url": "rtsp://10.0.0.1/x", "rtsp_decode": "some"},
            "CHANGE_ME": {"type": "rtsp", "rtsp_url": "rtsp://10.0.0.1/x", "username": "a", "password": "CHANGE_ME"},
        }
        for needle, entry in cases.items():
            with self.subTest(needle):
                with self.assertRaises(live.LiveConfigError) as ctx:
                    live.parse_config({"cam": entry})
                self.assertIn(needle, str(ctx.exception))

    def test_scrub_hides_passwords_with_special_characters(self):
        src = live.parse_config({"cam": {"type": "rtsp", "rtsp_url": "rtsp://admin:p%40ss@10.0.0.5/x"}})["cam"]
        text = src.scrub(f"failed {src.detection_rtsp_url()} and rtsp://admin:p@ss@10.0.0.5/x with p@ss")
        self.assertNotIn("p@ss", text)
        self.assertNotIn("p%40ss", text)
        self.assertNotIn("ss@", text)


# --- snapshot camera ---------------------------------------------------------

class CopyConnectionTests(unittest.TestCase):
    """Connecting a new app camera to a physical camera another one already uses."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "camera_sources.json"
        self.path.write_text(json.dumps({
            "_comment": "keep me",
            "reolink_live": {"host": "192.168.1.50", "username": "admin", "password": "p@ss", "interval_seconds": 3},
            "street_traffic": {"type": "rtsp", "rtsp_url": "rtsp://192.168.1.50:554/Preview_01_sub",
                               "username": "admin", "password": "p@ss", "rtsp_decode": "all", "interval_seconds": 0.2},
        }))

    def tearDown(self):
        self.tmp.cleanup()

    def test_traffic_camera_from_reolink_snapshot_uses_the_video_stream(self):
        entry = live.copy_connection("reolink_live", "gate", "traffic", self.path)
        self.assertEqual(entry["type"], "rtsp")
        self.assertEqual(entry["rtsp_url"], "rtsp://192.168.1.50:554/Preview_01_sub")
        self.assertEqual((entry["rtsp_decode"], entry["interval_seconds"]), ("all", 0.2))
        saved = json.loads(self.path.read_text())
        self.assertEqual(saved["_comment"], "keep me")
        self.assertEqual(live.load_config(self.path)["gate"].password, "p@ss")

    def test_parking_camera_from_traffic_stream_slows_down(self):
        entry = live.copy_connection("street_traffic", "lot", "parking", self.path)
        self.assertEqual((entry["rtsp_decode"], entry["interval_seconds"]), ("keyframes", 3))

    def test_refuses_to_overwrite_or_copy_nothing(self):
        with self.assertRaises(live.LiveConfigError):
            live.copy_connection("street_traffic", "reolink_live", "traffic", self.path)
        with self.assertRaises(live.LiveConfigError):
            live.copy_connection("nope", "gate", "traffic", self.path)

    def test_choices_never_include_the_login(self):
        choices = live.connection_choices(self.path)
        self.assertEqual([c["camera_id"] for c in choices], ["reolink_live", "street_traffic"])
        self.assertNotIn("p@ss", json.dumps(choices))


class ZoomAreaTests(unittest.TestCase):
    """The zoom area ("focus") and full-resolution switch for traffic cameras."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "camera_sources.json"
        self.path.write_text(json.dumps({
            "street": {"type": "rtsp", "rtsp_url": "rtsp://192.168.1.50:554/Preview_01_sub",
                       "username": "admin", "password": "pw", "rtsp_decode": "all", "interval_seconds": 0.2},
            "hik": {"type": "rtsp", "rtsp_url": "rtsp://192.168.1.64:554/Streaming/Channels/101",
                    "username": "admin", "password": "pw", "rtsp_decode": "all", "interval_seconds": 0.2},
            "snap": {"host": "192.168.1.50", "username": "admin", "password": "pw"},
        }))

    def tearDown(self):
        self.tmp.cleanup()

    def test_focus_crops_before_scaling(self):
        live.update_view("street", {"x": 0.5, "y": 0.25, "w": 0.3, "h": 0.2}, True, self.path)
        src = live.load_config(self.path)["street"]
        self.assertEqual(src.focus, (0.5, 0.25, 0.3, 0.2))
        self.assertTrue(src.stream_url.endswith("Preview_01_main"))
        self.assertEqual(src.max_width, live.FULL_RESOLUTION_MAX_WIDTH)
        self.assertEqual(live.stream_quality(src), {"can_switch": True, "full_resolution": True})
        command = live.build_rtsp_frame_command(src, src.detection_rtsp_url())
        vf = command[command.index("-vf") + 1]
        self.assertEqual(
            vf,
            "fps=5,crop=w=trunc(iw*0.3/2)*2:h=trunc(ih*0.2/2)*2:x=trunc(iw*0.5):y=trunc(ih*0.25),"
            f"scale='min({live.FULL_RESOLUTION_MAX_WIDTH},iw)':-2",
        )

    def test_whole_picture_and_back_to_small_stream(self):
        live.update_view("street", {"x": 0.1, "y": 0.1, "w": 0.5, "h": 0.5}, True, self.path)
        live.update_view("street", None, False, self.path)
        src = live.load_config(self.path)["street"]
        self.assertIsNone(src.focus)
        self.assertTrue(src.stream_url.endswith("Preview_01_sub"))
        self.assertIsNone(src.max_width)
        command = live.build_rtsp_frame_command(src, src.detection_rtsp_url())
        self.assertEqual(command[command.index("-vf") + 1], "fps=5")
        self.assertEqual(json.loads(self.path.read_text())["street"]["password"], "pw")

    def test_non_reolink_can_zoom_but_not_switch_quality(self):
        live.update_view("hik", {"x": 0, "y": 0, "w": 0.5, "h": 0.5}, None, self.path)
        self.assertEqual(live.load_config(self.path)["hik"].focus, (0.0, 0.0, 0.5, 0.5))
        with self.assertRaises(live.LiveConfigError):
            live.update_view("hik", None, True, self.path)

    def test_bad_zoom_areas(self):
        for focus in ({"x": 0.9, "y": 0, "w": 0.5, "h": 0.5}, {"x": 0, "y": 0, "w": 0.01, "h": 0.5}, {"x": "a"}):
            with self.assertRaises(live.LiveConfigError):
                live.update_view("street", focus, None, self.path)
        with self.assertRaises(live.LiveConfigError):
            live.update_view("snap", {"x": 0, "y": 0, "w": 0.5, "h": 0.5}, None, self.path)

    def test_copying_a_camera_does_not_copy_its_zoom(self):
        live.update_view("street", {"x": 0.1, "y": 0.1, "w": 0.5, "h": 0.5}, None, self.path)
        entry = live.copy_connection("street", "street2", "traffic", self.path)
        self.assertNotIn("focus", entry)


class _SnapshotHandler(BaseHTTPRequestHandler):
    mode = "basic"  # or "digest" / "none"
    user, password, realm, nonce = "admin", "pw", "cam", "abc123"

    def log_message(self, *a):
        pass

    def _deny(self):
        self.send_response(401)
        if self.mode == "digest":
            self.send_header("WWW-Authenticate", f'Digest realm="{self.realm}", nonce="{self.nonce}", qop="auth"')
        else:
            self.send_header("WWW-Authenticate", f'Basic realm="{self.realm}"')
        self.end_headers()

    def _digest_ok(self, header: str) -> bool:
        fields = {}
        for part in header[len("Digest "):].split(","):
            if "=" in part:
                k, v = part.strip().split("=", 1)
                fields[k] = v.strip('"')
        ha1 = hashlib.md5(f"{self.user}:{self.realm}:{self.password}".encode()).hexdigest()
        ha2 = hashlib.md5(f"GET:{fields.get('uri')}".encode()).hexdigest()
        expected = hashlib.md5(
            f"{ha1}:{fields.get('nonce')}:{fields.get('nc')}:{fields.get('cnonce')}:{fields.get('qop')}:{ha2}".encode()
        ).hexdigest()
        return fields.get("username") == self.user and fields.get("response") == expected

    def do_GET(self):
        if self.path.startswith("/missing"):
            self.send_response(404); self.end_headers(); return
        auth = self.headers.get("Authorization", "")
        import base64
        ok = (
            self.mode == "none"
            or (self.mode == "basic" and auth == "Basic " + base64.b64encode(f"{self.user}:{self.password}".encode()).decode())
            or (self.mode == "digest" and auth.startswith("Digest ") and self._digest_ok(auth))
        )
        if not ok:
            self._deny(); return
        self.send_response(200)
        self.send_header("Content-Type", "image/jpeg")
        self.send_header("Content-Length", str(len(FAKE_JPEG)))
        self.end_headers()
        self.wfile.write(FAKE_JPEG)


class HttpSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _SnapshotHandler)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        _SnapshotHandler.mode = "basic"

    def fetcher(self, path="/snap.jpg", password="pw"):
        src = live.parse_config({"cam": {
            "type": "http_snapshot", "snapshot_url": f"http://127.0.0.1:{self.port}{path}",
            "username": "admin", "password": password,
        }})["cam"]
        return live.make_fetcher(src)

    def test_basic_login(self):
        _SnapshotHandler.mode = "basic"
        self.assertEqual(self.fetcher().fetch(), FAKE_JPEG)

    def test_digest_login(self):
        _SnapshotHandler.mode = "digest"
        self.assertEqual(self.fetcher().fetch(), FAKE_JPEG)

    def test_wrong_password_says_so(self):
        _SnapshotHandler.mode = "digest"
        with self.assertRaises(live.LiveFrameError) as ctx:
            self.fetcher(password="nope").fetch()
        self.assertIn("username or password", str(ctx.exception))

    def test_wrong_address_says_so(self):
        with self.assertRaises(live.LiveFrameError) as ctx:
            self.fetcher(path="/missing").fetch()
        self.assertIn("404", str(ctx.exception))


# --- RTSP-style stream ---------------------------------------------------------

@unittest.skipUnless(_ffmpeg(), "ffmpeg not available")
class RtspFetcherTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.video = Path(cls.tmp.name) / "stream.mp4"
        # 12 s of 1280x720 test pattern, a key frame every 1 s (typical camera).
        subprocess.run(
            [_ffmpeg(), "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=size=1280x720:rate=15",
             "-t", "12", "-c:v", "libx264", "-g", "15", "-pix_fmt", "yuv420p", str(cls.video)],
            check=True,
        )

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def source(self, decode="keyframes", interval=1.0):
        return live.parse_config({"cam": {
            "type": "rtsp", "rtsp_url": "rtsp://10.0.0.9/stream", "rtsp_decode": decode, "interval_seconds": interval,
        }})["cam"]

    def command_for(self, src, url=None):
        # The real command, but reading the test video at real-time speed
        # ("-re") in place of the camera's address.
        command = live.build_rtsp_frame_command(src, url or str(self.video))
        i = command.index("-i")
        return command[:i] + ["-re"] + command[i:]

    def test_keyframes_mode_delivers_fresh_frames(self):
        src = self.source("keyframes")
        fetcher = live.RtspFetcher(src, command=self.command_for(src), first_frame_timeout=10)
        try:
            frames, started = [], time.monotonic()
            for _ in range(4):
                frames.append(fetcher.fetch())
            elapsed = time.monotonic() - started
        finally:
            fetcher.close()
        self.assertTrue(all(f.startswith(b"\xff\xd8") and f.endswith(b"\xff\xd9") for f in frames))
        self.assertEqual(len(set(frames)), 4, "every fetch should hand over a newer picture")
        from PIL import Image
        import io
        self.assertEqual(Image.open(io.BytesIO(frames[-1])).size, (1280, 720), "full resolution is kept")
        self.assertLess(elapsed, 8)

    def test_all_frames_mode_respects_interval(self):
        src = self.source("all", interval=2.0)
        fetcher = live.RtspFetcher(src, command=self.command_for(src), first_frame_timeout=10)
        try:
            fetcher.fetch()
            t0 = time.monotonic()
            fetcher.fetch()
            gap = time.monotonic() - t0
        finally:
            fetcher.close()
        self.assertGreater(gap, 1.0)
        self.assertLess(gap, 4.0)

    def test_all_frames_mode_keeps_every_picture_from_a_bursty_stream(self):
        # A camera's pictures come off the network in bursts. Taking only the
        # newest lost ~25% of them on Rob's Reolink; every one must be kept,
        # with evenly spaced times.
        producer = (
            "import sys,time\n"
            "j=b'\\xff\\xd8' + b'x'*50 + b'\\xff\\xd9'\n"
            "w=sys.stdout.buffer\n"
            "while True:\n"
            "    for _ in range(3):\n"
            "        w.write(b'--frame\\r\\nContent-Type: image/jpeg\\r\\nContent-length: %d\\r\\n\\r\\n' % len(j) + j + b'\\r\\n')\n"
            "    w.flush(); time.sleep(0.6)\n"
        )
        import sys
        from datetime import datetime
        src = self.source("all", interval=0.2)
        fetcher = live.RtspFetcher(src, command=[sys.executable, "-c", producer], first_frame_timeout=10)
        grabber = live.FrameGrabber(src, None, fetcher=fetcher)
        stamps = []
        grabber.on_frame = lambda cam, jpeg, at: stamps.append(datetime.fromisoformat(at).timestamp())
        grabber.start()
        try:
            time.sleep(3.2)
        finally:
            grabber.stop()
        self.assertGreaterEqual(len(stamps), 13, f"only {len(stamps)} pictures in 3.2 s at 5 per second")
        gaps = [b - a for a, b in zip(stamps, stamps[1:])]
        self.assertTrue(all(abs(g - 0.2) < 0.01 for g in gaps), gaps)

    def _stamps_from(self, producer_body, seconds):
        import sys
        from datetime import datetime
        producer = (
            "import sys,time\n"
            "j=b'\\xff\\xd8' + b'x'*50 + b'\\xff\\xd9'\n"
            "w=sys.stdout.buffer\n"
            "def emit():\n"
            "    w.write(b'--frame\\r\\nContent-Type: image/jpeg\\r\\nContent-length: %d\\r\\n\\r\\n' % len(j) + j + b'\\r\\n'); w.flush()\n"
            + producer_body
        )
        src = self.source("all", interval=0.2)
        fetcher = live.RtspFetcher(src, command=[sys.executable, "-c", producer], first_frame_timeout=10)
        grabber = live.FrameGrabber(src, None, fetcher=fetcher)
        stamps = []
        grabber.on_frame = lambda cam, jpeg, at: stamps.append(datetime.fromisoformat(at).timestamp())
        grabber.start()
        try:
            time.sleep(seconds)
        finally:
            grabber.stop()
        return stamps

    def test_pictures_held_up_then_arriving_at_once_keep_their_times(self):
        # 2 s steady, then nothing for 3 s, then those 15 pictures at once
        # (what a Wi-Fi hiccup looks like), then steady again.
        stamps = self._stamps_from(
            "for _ in range(10):\n    emit(); time.sleep(0.2)\n"
            "time.sleep(3)\n"
            "for _ in range(15):\n    emit()\n"
            "while True:\n    emit(); time.sleep(0.2)\n",
            7.0,
        )
        self.assertGreaterEqual(len(stamps), 30)
        gaps = [b - a for a, b in zip(stamps, stamps[1:])]
        self.assertTrue(all(abs(g - 0.2) < 0.01 for g in gaps[:30]), [round(g, 2) for g in gaps[:30]])

    def test_pictures_really_lost_leave_a_gap(self):
        stamps = self._stamps_from(
            "for _ in range(10):\n    emit(); time.sleep(0.2)\n"
            "time.sleep(3)\n"
            "while True:\n    emit(); time.sleep(0.2)\n",
            6.5,
        )
        gaps = [b - a for a, b in zip(stamps, stamps[1:])]
        self.assertTrue(any(g > 2.0 for g in gaps), [round(g, 2) for g in gaps])
        self.assertTrue(all(g > 0 for g in gaps), "times never go backwards")

    def test_ffmpeg_stream_description_is_read(self):
        info = live._parse_stream_description(
            "[in#0/rtsp @ 0x1] Stream #0:0: Video: h264 (Main), yuv420p(progressive), 640x480, 10 fps, 10 tbr, 90k tbn"
        )
        self.assertEqual(info, {"codec": "h264", "stream_width": 640, "stream_height": 480, "stream_fps": 10.0})
        self.assertIsNone(live._parse_stream_description("Stream #0:0: Video: mjpeg, yuvj420p, 640x480, 5 fps"))
        self.assertEqual(live._split_ffmpeg_level("[rtsp @ 0x1] [error] 401 Unauthorized"),
                         ("error", "[rtsp @ 0x1] 401 Unauthorized"))

    def test_stream_that_ends_is_reported_then_reconnects(self):
        src = self.source("keyframes")
        short = Path(self.tmp.name) / "short.mp4"
        subprocess.run([_ffmpeg(), "-hide_banner", "-loglevel", "error", "-y", "-i", str(self.video), "-t", "2", "-c", "copy", str(short)], check=True)
        fetcher = live.RtspFetcher(src, command=self.command_for(src, str(short)), first_frame_timeout=10, stall_timeout=3)
        try:
            got = 0
            with self.assertRaises(live.LiveFrameError):
                for _ in range(10):
                    fetcher.fetch()
                    got += 1
            self.assertGreaterEqual(got, 1)
            # Next fetch starts a fresh connection by itself.
            self.assertTrue(fetcher.fetch().startswith(b"\xff\xd8"))
        finally:
            fetcher.close()

    def test_unreachable_camera_gives_readable_error_without_password(self):
        src = live.parse_config({"cam": {"type": "rtsp", "rtsp_url": "rtsp://admin:hunter2@127.0.0.1:1/stream"}})["cam"]
        fetcher = live.RtspFetcher(src, first_frame_timeout=8)
        try:
            with self.assertRaises(live.LiveFrameError) as ctx:
                fetcher.fetch()
        finally:
            fetcher.close()
        message = str(ctx.exception)
        self.assertIn("refused", message)
        self.assertNotIn("hunter2", message)

    def test_grabber_hands_frames_to_detector(self):
        src = self.source("keyframes", interval=1.0)
        got = []
        fetcher = live.RtspFetcher(src, command=self.command_for(src), first_frame_timeout=10)
        tmp_live = Path(self.tmp.name) / "live"
        old = live.LIVE_DIR
        live.LIVE_DIR = tmp_live
        grabber = live.FrameGrabber(src, on_frame=lambda cam, jpeg, at: got.append((cam, len(jpeg), at)), fetcher=fetcher)
        try:
            grabber.start()
            deadline = time.monotonic() + 10
            while len(got) < 3 and time.monotonic() < deadline:
                time.sleep(0.2)
        finally:
            grabber.stop()
            grabber.join(timeout=5)
            live.LIVE_DIR = old
        self.assertGreaterEqual(len(got), 3)
        self.assertTrue((tmp_live / "cam" / "latest.jpg").exists())
        self.assertEqual(grabber.status()["type"], "rtsp")
        self.assertFalse(grabber.is_alive())
        self.assertIsNone(fetcher._proc, "stopping the grabber closes the stream")


if __name__ == "__main__":
    unittest.main()
