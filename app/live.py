"""Live camera ingestion for the parking-lot POC (stdlib only, no new dependencies).

What this module does
---------------------
* Reads ``data/camera_sources.json`` (gitignored, because it holds camera
  passwords) and runs one background grabber thread per configured camera.
* Every ``interval_seconds`` (default 3) a grabber asks its camera for a JPEG
  snapshot and passes it to an optional ``on_frame`` callback -- the hook the
  continuous detector plugs into next.
* Keeps exactly ONE image on disk per camera, ``data/live/<camera_id>/latest.jpg``,
  overwritten atomically every time. Nothing accumulates, so storage stays flat.
* Can relay the camera's RTSP stream to a browser as MJPEG (about 2 fps) through
  ffmpeg, only while someone is actually watching.

Camera credentials stay on the server. They are never sent to the browser, never
included in status output, and are scrubbed from error messages.

Frame policy
------------
``frame_policy`` decides what happens to frames after they have been analysed.
Only ``"none"`` (keep nothing but the newest frame) exists today. ``"events"``
(keep a frame when a space changes state) and ``"all"`` are deliberately not
implemented yet -- the config loader rejects them with a clear message rather
than silently ignoring them, so nobody thinks photos are being saved when they
are not. When they are added, they slot in where ``on_frame`` is called.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import ssl
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Optional

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"
CONFIG_PATH = DATA_DIR / "camera_sources.json"
LIVE_DIR = DATA_DIR / "live"

SUPPORTED_FRAME_POLICIES = ("none",)
MIN_INTERVAL_SECONDS = 1.0
MAX_INTERVAL_SECONDS = 3600.0
MAX_FRAME_BYTES = 20 * 1024 * 1024
SNAPSHOT_TIMEOUT_SECONDS = 8.0
MAX_BACKOFF_SECONDS = 60.0
PLACEHOLDER_PASSWORD = "CHANGE_ME"

MAX_RELAYS_PER_CAMERA = 2
RELAY_FIRST_DATA_TIMEOUT_SECONDS = 12.0
RELAY_STALL_TIMEOUT_SECONDS = 15.0
MJPEG_BOUNDARY = "frame"
MJPEG_CONTENT_TYPE = f"multipart/x-mixed-replace;boundary={MJPEG_BOUNDARY}"

_CAMERA_ID_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
_HOST_RE = re.compile(r"^[A-Za-z0-9.\-]+$")


class LiveConfigError(ValueError):
    """The camera config file is missing something or has a bad value."""


class LiveFrameError(RuntimeError):
    """A snapshot could not be fetched from the camera."""


class RelayBusy(RuntimeError):
    """Too many browsers are already watching this camera's live stream."""


class RelayFailed(RuntimeError):
    """The live stream could not be started or died before sending anything."""


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CameraSource:
    camera_id: str
    host: str
    username: str
    # repr=False so the password can't leak into a stray log line or traceback.
    password: str = field(repr=False)
    channel: int = 0
    scheme: str = "http"
    port: Optional[int] = None
    rtsp_port: int = 554
    interval_seconds: float = 3.0
    frame_policy: str = "none"
    live_fps: float = 2.0
    live_stream: str = "sub"

    def snapshot_url(self) -> str:
        query = urllib.parse.urlencode(
            {
                "cmd": "Snap",
                "channel": self.channel,
                # Cache-buster required by the Reolink API; any string works.
                "rs": secrets.token_hex(4),
                "user": self.username,
                "password": self.password,
            }
        )
        port = f":{self.port}" if self.port else ""
        return f"{self.scheme}://{self.host}{port}/cgi-bin/api.cgi?{query}"

    def rtsp_url(self) -> str:
        user = urllib.parse.quote(self.username, safe="")
        password = urllib.parse.quote(self.password, safe="")
        return (
            f"rtsp://{user}:{password}@{self.host}:{self.rtsp_port}"
            f"/Preview_{self.channel + 1:02d}_{self.live_stream}"
        )

    def scrub(self, text: str) -> str:
        """Removes this camera's password (raw and URL-encoded) from a message."""
        if self.password:
            for secret in {self.password, urllib.parse.quote(self.password, safe="")}:
                text = text.replace(secret, "***")
        return text


def _number(entry: dict, key: str, default: float, low: float, high: float, cam: str) -> float:
    value = entry.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise LiveConfigError(f'{cam}: "{key}" must be a number.')
    if not (low <= value <= high):
        raise LiveConfigError(f'{cam}: "{key}" must be between {low:g} and {high:g}.')
    return float(value)


def _integer(entry: dict, key: str, default: int, low: int, high: int, cam: str) -> int:
    value = entry.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int):
        raise LiveConfigError(f'{cam}: "{key}" must be a whole number.')
    if not (low <= value <= high):
        raise LiveConfigError(f'{cam}: "{key}" must be between {low} and {high}.')
    return value


def parse_config(raw: Any) -> dict:
    """Validates the decoded JSON and returns ``{camera_id: CameraSource}``.

    Error messages name the camera and the field but never echo a password.
    """
    if not isinstance(raw, dict):
        raise LiveConfigError("camera_sources.json must be a JSON object keyed by camera id.")
    sources: dict = {}
    for camera_id, entry in raw.items():
        if camera_id.startswith("_"):
            continue  # allow "_comment" style keys
        if not _CAMERA_ID_RE.match(camera_id):
            raise LiveConfigError(f'"{camera_id}" is not a valid camera id (letters, numbers, _ . - only).')
        if not isinstance(entry, dict):
            raise LiveConfigError(f"{camera_id}: settings must be a JSON object.")

        host = entry.get("host")
        if not isinstance(host, str) or not _HOST_RE.match(host.strip()):
            raise LiveConfigError(f'{camera_id}: "host" must be the camera\'s IP address, e.g. "192.168.1.50".')
        username = entry.get("username")
        if not isinstance(username, str) or not username:
            raise LiveConfigError(f'{camera_id}: "username" is required (Reolink default is "admin").')
        password = entry.get("password")
        if not isinstance(password, str) or not password:
            raise LiveConfigError(f'{camera_id}: "password" is required.')
        if password == PLACEHOLDER_PASSWORD:
            raise LiveConfigError(f'{camera_id}: replace the placeholder "{PLACEHOLDER_PASSWORD}" with the camera\'s real password.')

        scheme = entry.get("scheme", "http")
        if scheme not in ("http", "https"):
            raise LiveConfigError(f'{camera_id}: "scheme" must be "http" or "https".')
        stream = entry.get("live_stream", "sub")
        if stream not in ("sub", "main"):
            raise LiveConfigError(f'{camera_id}: "live_stream" must be "sub" or "main".')
        policy = entry.get("frame_policy", "none")
        if policy not in SUPPORTED_FRAME_POLICIES:
            raise LiveConfigError(
                f'{camera_id}: frame_policy "{policy}" is not available yet. '
                f'Supported today: {", ".join(SUPPORTED_FRAME_POLICIES)} (keeps no photos except the newest).'
            )
        port_raw = entry.get("port")
        port = None if port_raw is None else _integer(entry, "port", 0, 1, 65535, camera_id)

        sources[camera_id] = CameraSource(
            camera_id=camera_id,
            host=host.strip(),
            username=username,
            password=password,
            channel=_integer(entry, "channel", 0, 0, 63, camera_id),
            scheme=scheme,
            port=port,
            rtsp_port=_integer(entry, "rtsp_port", 554, 1, 65535, camera_id),
            interval_seconds=_number(entry, "interval_seconds", 3.0, MIN_INTERVAL_SECONDS, MAX_INTERVAL_SECONDS, camera_id),
            frame_policy=policy,
            live_fps=_number(entry, "live_fps", 2.0, 0.5, 10.0, camera_id),
            live_stream=stream,
        )
    return sources


def load_config(path: Optional[Path] = None) -> dict:
    """Loads and validates the config. A missing file just means "no live cameras"."""
    path = path or CONFIG_PATH
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise LiveConfigError(f"camera_sources.json is not valid JSON (line {exc.lineno}, column {exc.colno}).")
    return parse_config(raw)


# ---------------------------------------------------------------------------
# Snapshot fetching and the single on-disk "latest" frame
# ---------------------------------------------------------------------------


_BAD_LOGIN_MESSAGE = (
    "the camera rejected the username or password "
    "(check them in data/camera_sources.json, then restart the server)"
)


def _unreachable_message(source: CameraSource, reason: Any) -> str:
    text = str(reason)
    if isinstance(reason, ConnectionRefusedError) or "refused" in text.lower():
        hint = "the camera answered but refused the connection: check that HTTP is enabled in the camera's network settings"
    elif isinstance(reason, TimeoutError) or "timed out" in text.lower():
        hint = f"no answer from {source.host}: check the IP address and that the camera is on the same network"
    else:
        hint = f"cannot reach {source.host}: {text}"
    return hint


def fetch_snapshot(source: CameraSource, timeout: float = SNAPSHOT_TIMEOUT_SECONDS) -> bytes:
    """Fetches one JPEG from the camera or raises LiveFrameError (password scrubbed)."""
    url = source.snapshot_url()
    context = None
    if source.scheme == "https":
        # Reolink cameras ship with a self-signed certificate, so verification
        # would always fail on a LAN. Acceptable for a camera on your own network.
        context = ssl._create_unverified_context()
    request = urllib.request.Request(url, headers={"User-Agent": "ParkingLotPOC"})
    try:
        with urllib.request.urlopen(request, timeout=timeout, context=context) as response:
            data = response.read(MAX_FRAME_BYTES + 1)
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            raise LiveFrameError(_BAD_LOGIN_MESSAGE) from None
        raise LiveFrameError(f"camera answered HTTP {exc.code}") from None
    except urllib.error.URLError as exc:
        raise LiveFrameError(source.scrub(_unreachable_message(source, exc.reason))) from None
    except (OSError, ValueError) as exc:
        raise LiveFrameError(source.scrub(_unreachable_message(source, exc))) from None

    if len(data) > MAX_FRAME_BYTES:
        raise LiveFrameError("camera sent an unexpectedly large response")
    if not data.startswith(b"\xff\xd8"):
        # A wrong password / disabled HTTP typically returns a small JSON error.
        snippet = source.scrub(data[:160].decode("utf-8", "replace").strip())
        lowered = snippet.lower().replace(" ", "")
        if "login" in lowered or '"rspcode":-6' in lowered or '"rspcode":-7' in lowered:
            raise LiveFrameError(_BAD_LOGIN_MESSAGE)
        raise LiveFrameError(f"camera did not return a photo: {snippet or 'empty response'}")
    return data


def latest_frame_path(camera_id: str) -> Path:
    return LIVE_DIR / camera_id / "latest.jpg"


def write_latest_frame(camera_id: str, jpeg: bytes) -> None:
    """Atomically replaces the camera's newest frame (readers never see a partial file)."""
    directory = LIVE_DIR / camera_id
    directory.mkdir(parents=True, exist_ok=True)
    temp = directory / "latest.jpg.tmp"
    temp.write_bytes(jpeg)
    os.replace(temp, directory / "latest.jpg")


# ---------------------------------------------------------------------------
# Grabber threads
# ---------------------------------------------------------------------------

FrameCallback = Callable[[str, bytes, str], None]


class FrameGrabber(threading.Thread):
    """Fetches a snapshot on a fixed cadence for one camera, backing off on errors."""

    def __init__(self, source: CameraSource, on_frame: Optional[FrameCallback] = None):
        super().__init__(daemon=True, name=f"grabber-{source.camera_id}")
        self.source = source
        self.on_frame = on_frame
        # NOTE: not named _stop -- threading.Thread already uses that name internally.
        self._halt = threading.Event()
        self._lock = threading.Lock()
        self._status: dict = {
            "running": False,
            "started_at": None,
            "last_frame_at": None,
            "last_frame_bytes": None,
            "frames_grabbed": 0,
            "consecutive_failures": 0,
            "last_error": None,
            "last_error_at": None,
        }

    def stop(self) -> None:
        self._halt.set()

    def status(self) -> dict:
        with self._lock:
            status = dict(self._status)
        status["configured"] = True
        status["camera_id"] = self.source.camera_id
        status["interval_seconds"] = self.source.interval_seconds
        status["frame_policy"] = self.source.frame_policy
        status["live_fps"] = self.source.live_fps
        with _relay_lock:
            status["stream_error"] = _stream_errors.get(self.source.camera_id)
        return status

    def _update(self, **fields: Any) -> None:
        with self._lock:
            self._status.update(fields)

    def run(self) -> None:
        self._update(running=True, started_at=utc_now_iso())
        failures = 0
        try:
            while not self._halt.is_set():
                started = time.monotonic()
                try:
                    jpeg = fetch_snapshot(self.source)
                    captured_at = utc_now_iso()
                    write_latest_frame(self.source.camera_id, jpeg)
                    failures = 0
                    with self._lock:
                        self._status.update(
                            last_frame_at=captured_at,
                            last_frame_bytes=len(jpeg),
                            frames_grabbed=self._status["frames_grabbed"] + 1,
                            consecutive_failures=0,
                            last_error=None,
                        )
                    if self.on_frame is not None:
                        try:
                            self.on_frame(self.source.camera_id, jpeg, captured_at)
                        except Exception as exc:  # a consumer bug must not stop the grabber
                            self._update(last_error=self.source.scrub(f"frame handler failed: {exc}"), last_error_at=utc_now_iso())
                except LiveFrameError as exc:
                    failures += 1
                    self._update(consecutive_failures=failures, last_error=str(exc), last_error_at=utc_now_iso())
                except Exception as exc:
                    failures += 1
                    self._update(
                        consecutive_failures=failures,
                        last_error=self.source.scrub(f"unexpected error: {exc}"),
                        last_error_at=utc_now_iso(),
                    )

                if failures:
                    delay = min(self.source.interval_seconds * (2 ** min(failures, 6)), MAX_BACKOFF_SECONDS)
                else:
                    delay = self.source.interval_seconds
                self._halt.wait(max(0.0, delay - (time.monotonic() - started)))
        finally:
            self._update(running=False)


_registry_lock = threading.Lock()
_grabbers: dict = {}
_config_error: Optional[str] = None


def start_all(
    on_frame: Optional[FrameCallback] = None,
    config_path: Optional[Path] = None,
    only_camera_ids: Optional[Iterable[str]] = None,
) -> list:
    """(Re)starts a grabber for every camera in the config. Never raises: a bad
    config is reported through get_status() instead of stopping the server.

    If only_camera_ids is given, config entries for any other id are skipped --
    this is how a camera deleted on the website stays stopped after a restart
    even though its entry is still in the settings file."""
    global _config_error
    stop_all()
    try:
        sources = load_config(config_path)
    except LiveConfigError as exc:
        with _registry_lock:
            _config_error = str(exc)
        print(f"Live camera config problem: {exc}")
        return []
    with _registry_lock:
        _config_error = None
        allowed = None if only_camera_ids is None else set(only_camera_ids)
        for camera_id in list(sources):
            if allowed is not None and camera_id not in allowed:
                print(f"Skipping live camera '{camera_id}': no camera with that id on the website.")
                del sources[camera_id]
        for camera_id, source in sources.items():
            grabber = FrameGrabber(source, on_frame)
            _grabbers[camera_id] = grabber
            grabber.start()
    return sorted(sources)


def stop_all() -> None:
    with _registry_lock:
        grabbers = list(_grabbers.values())
        _grabbers.clear()
    for grabber in grabbers:
        grabber.stop()


def stop_camera(camera_id: str) -> bool:
    """Stops and forgets one camera's grabber. Returns True if it was running."""
    with _registry_lock:
        grabber = _grabbers.pop(camera_id, None)
    if grabber is None:
        return False
    grabber.stop()
    return True


def get_source(camera_id: str) -> Optional[CameraSource]:
    with _registry_lock:
        grabber = _grabbers.get(camera_id)
    return grabber.source if grabber else None


def get_status(camera_id: str) -> dict:
    with _registry_lock:
        grabber = _grabbers.get(camera_id)
        config_error = _config_error
    if grabber is None:
        return {"configured": False, "camera_id": camera_id, "config_error": config_error}
    return grabber.status()


# ---------------------------------------------------------------------------
# Live view: RTSP -> MJPEG relay (only runs while a browser is watching)
# ---------------------------------------------------------------------------

_relay_lock = threading.Lock()
_relay_counts: dict = {}
# Why the most recent live-stream attempt failed (None once one succeeds), so
# the website can explain it instead of just showing a broken picture.
_stream_errors: dict = {}


def _set_stream_error(camera_id: str, message: Optional[str]) -> None:
    with _relay_lock:
        if message is None:
            _stream_errors.pop(camera_id, None)
        else:
            _stream_errors[camera_id] = message


def find_ffmpeg() -> str:
    """The ffmpeg this app already uses for video import (the copy bundled with the
    imageio-ffmpeg package), falling back to one installed on the system PATH."""
    try:
        from app.video_extract import get_ffmpeg_exe

        return get_ffmpeg_exe()
    except Exception:  # imageio-ffmpeg missing (FfmpegUnavailable) or its binary unusable
        return shutil.which("ffmpeg") or "ffmpeg"


def build_mjpeg_command(source: CameraSource) -> list:
    video_filter = f"fps={source.live_fps:g}"
    if source.live_stream == "main":
        video_filter += ",scale=1280:-2"  # main stream is 5MP; shrink it for the browser
    return [
        find_ffmpeg(),
        "-hide_banner", "-loglevel", "error",
        "-rtsp_transport", "tcp",
        "-i", source.rtsp_url(),
        "-an",
        "-vf", video_filter,
        "-q:v", "5",
        "-f", "mpjpeg",
        "-boundary_tag", MJPEG_BOUNDARY,
        "pipe:1",
    ]


class MjpegRelay:
    """One ffmpeg process turning the camera's RTSP stream into MJPEG for one browser.

    Usage: ``first = relay.start()``, then iterate ``relay.chunks()``, and always
    call ``relay.close()`` (kills ffmpeg and frees this camera's relay slot).
    """

    def __init__(self, source: CameraSource, command: Optional[list] = None):
        self.source = source
        self.command = command
        self._proc: Optional[subprocess.Popen] = None
        self._slot_held = False
        self._closed = threading.Event()
        self._last_data = time.monotonic()
        self._got_data = False
        self._timed_out = False

    def _acquire_slot(self) -> None:
        with _relay_lock:
            count = _relay_counts.get(self.source.camera_id, 0)
            if count >= MAX_RELAYS_PER_CAMERA:
                raise RelayBusy("This camera's live view is already open in other browser tabs.")
            _relay_counts[self.source.camera_id] = count + 1
            self._slot_held = True

    def _watchdog(self) -> None:
        while not self._closed.wait(1.0):
            limit = RELAY_STALL_TIMEOUT_SECONDS if self._got_data else RELAY_FIRST_DATA_TIMEOUT_SECONDS
            if time.monotonic() - self._last_data > limit:
                self._timed_out = True
                self._kill()
                return

    def _kill(self) -> None:
        proc = self._proc
        if proc is not None and proc.poll() is None:
            try:
                proc.kill()
            except OSError:
                pass

    def _read(self) -> bytes:
        assert self._proc is not None and self._proc.stdout is not None
        chunk = os.read(self._proc.stdout.fileno(), 65536)
        if chunk:
            self._last_data = time.monotonic()
            self._got_data = True
        return chunk

    def _failure_message(self) -> str:
        detail = ""
        if self._proc is not None and self._proc.stderr is not None:
            try:
                detail = self._proc.stderr.read().decode("utf-8", "replace").strip()
            except (OSError, ValueError):
                detail = ""
        # ffmpeg can print several noisy lines; the last one is the actual reason
        # (e.g. "...: Connection refused"). Keep it short and free of the password.
        lines = [line.strip() for line in detail.splitlines() if line.strip()]
        detail = self.source.scrub(lines[-1] if lines else "")[-200:]
        if self._timed_out:
            base = "The camera's video stream didn't start in time (is RTSP enabled on the camera?)"
        else:
            base = "The camera's video stream stopped"
        return f"{base}. {detail}".strip() if detail else base + "."

    def start(self) -> bytes:
        self._acquire_slot()
        try:
            command = self.command or build_mjpeg_command(self.source)
            try:
                self._proc = subprocess.Popen(
                    command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL
                )
            except FileNotFoundError:
                message = "Live video needs ffmpeg, which the server can't find. Install it with: pip install imageio-ffmpeg (then restart the server)."
                _set_stream_error(self.source.camera_id, message)
                raise RelayFailed(message) from None
            self._last_data = time.monotonic()
            threading.Thread(target=self._watchdog, daemon=True, name=f"relay-watch-{self.source.camera_id}").start()
            first = self._read()
            if not first:
                message = self._failure_message()
                _set_stream_error(self.source.camera_id, message)
                raise RelayFailed(message)
            _set_stream_error(self.source.camera_id, None)
            return first
        except BaseException:
            self.close()
            raise

    def chunks(self) -> Iterator[bytes]:
        while not self._closed.is_set():
            chunk = self._read()
            if not chunk:
                return
            yield chunk

    def close(self) -> None:
        self._closed.set()
        self._kill()
        proc = self._proc
        if proc is not None:
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
            for pipe in (proc.stdout, proc.stderr):
                try:
                    if pipe is not None:
                        pipe.close()
                except OSError:
                    pass
        if self._slot_held:
            with _relay_lock:
                _relay_counts[self.source.camera_id] = max(0, _relay_counts.get(self.source.camera_id, 1) - 1)
            self._slot_held = False
