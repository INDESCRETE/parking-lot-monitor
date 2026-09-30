"""Live camera ingestion for the parking-lot POC (stdlib only, no new dependencies).

What this module does
---------------------
* Reads ``data/camera_sources.json`` (gitignored, because it holds camera
  passwords) and runs one background grabber thread per configured camera.
* Every ``interval_seconds`` (default 3) a grabber gets the newest picture from
  its camera and passes it to an optional ``on_frame`` callback (the
  continuous detector). How it gets the picture depends on the camera's
  ``type``:

  - ``"reolink"`` (the default): Reolink's own snapshot command over HTTP.
  - ``"rtsp"``: the standard video stream nearly every IP camera, NVR and DVR
    offers (Hikvision, Dahua, Axis, Uniview, Lorex, Amcrest, UniFi...). One
    ffmpeg process stays connected and hands over the newest frame.
  - ``"http_snapshot"``: any camera or recorder with a "give me a JPEG" web
    address (basic or digest login both work).
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

import collections
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
# Traffic counting follows moving cars, so it needs several pictures a second.
# That's only possible from a video stream decoded in full (type "rtsp" with
# rtsp_decode "all"); snapshot cameras stay at one picture a second or slower.
MIN_STREAM_INTERVAL_SECONDS = 0.1
MAX_INTERVAL_SECONDS = 3600.0
MAX_FRAME_BYTES = 20 * 1024 * 1024
SNAPSHOT_TIMEOUT_SECONDS = 8.0
MAX_BACKOFF_SECONDS = 60.0
PLACEHOLDER_PASSWORD = "CHANGE_ME"

CAMERA_TYPES = ("reolink", "rtsp", "http_snapshot")
RTSP_DECODE_MODES = ("keyframes", "all")
# A new stream can take a while to hand over its first picture (connecting,
# logging in, waiting for the first full "key" frame).
RTSP_FIRST_FRAME_TIMEOUT_SECONDS = 20.0
# Once running, no new picture for this long means the stream is stuck.
RTSP_STALL_TIMEOUT_SECONDS = 20.0

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
    type: str = "reolink"
    # For type "rtsp" (and optionally "http_snapshot", for Live View): the
    # stream address WITHOUT the login -- the username/password are added at
    # connect time so they never have to sit inside a URL in the config.
    stream_url: Optional[str] = None
    # Optional lighter stream just for the browser's Live View (e.g. a
    # camera's "sub stream"); detection still uses stream_url.
    live_stream_url: Optional[str] = None
    # For type "http_snapshot": the JPEG address (login added via HTTP auth).
    snapshot_address: Optional[str] = None
    # "keyframes" decodes only the stream's full frames (a picture every
    # 1-4 s on most cameras, very little CPU); "all" decodes everything.
    rtsp_decode: str = "keyframes"
    # rtsp_decode "all" only: shrink wider pictures to this width (keeps the
    # shape). Saves a lot of CPU when a 5MP main stream feeds traffic counting.
    max_width: Optional[int] = None
    # False keeps the camera's settings in the file but doesn't connect to it.
    enabled: bool = True

    def snapshot_url(self) -> str:
        if self.type == "http_snapshot":
            return self.snapshot_address or ""
        return self._reolink_snapshot_url()

    def _with_login(self, url: str) -> str:
        """Adds this camera's username/password to a stream address."""
        parts = urllib.parse.urlsplit(url)
        if not self.username or parts.username:
            return url
        user = urllib.parse.quote(self.username, safe="")
        password = urllib.parse.quote(self.password, safe="")
        netloc = f"{user}:{password}@{parts.netloc}" if self.password else f"{user}@{parts.netloc}"
        return urllib.parse.urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))

    def detection_rtsp_url(self) -> Optional[str]:
        """The stream detection reads, login included (type "rtsp" only)."""
        if self.type == "rtsp" and self.stream_url:
            return self._with_login(self.stream_url)
        return None

    def live_view_rtsp_url(self) -> Optional[str]:
        """The stream the browser's Live View relays, or None if there isn't one."""
        if self.type == "reolink":
            return self.rtsp_url()
        url = self.live_stream_url or self.stream_url
        return self._with_login(url) if url else None

    def _reolink_snapshot_url(self) -> str:
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
            for secret in sorted({self.password, urllib.parse.quote(self.password, safe="")}, key=len, reverse=True):
                text = text.replace(secret, "***")
        # Any login left inside an address (e.g. echoed back by ffmpeg).
        return re.sub(r"(rtsps?://|https?://)[^/\s]*@", r"\1***@", text)


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


def _clean_url(entry: dict, key: str, schemes: tuple, cam: str, username: str, password: str, *, required: bool):
    """Validates one address field. A login typed into the address itself
    (rtsp://admin:pw@...) is moved into username/password, so the address
    kept in memory -- and shown in errors -- never contains the password.
    Returns (url or None, username, password)."""
    value = entry.get(key)
    if value is None or value == "":
        if required:
            raise LiveConfigError(f'{cam}: "{key}" is required for this camera type.')
        return None, username, password
    if not isinstance(value, str):
        raise LiveConfigError(f'{cam}: "{key}" must be text.')
    parts = urllib.parse.urlsplit(value.strip())
    if parts.scheme not in schemes or not parts.hostname:
        raise LiveConfigError(f'{cam}: "{key}" must start with {" or ".join(s + "://" for s in schemes)} and include the camera\'s address.')
    if parts.username is not None:
        username = username or urllib.parse.unquote(parts.username)
        password = password or urllib.parse.unquote(parts.password or "")
        netloc = parts.hostname + (f":{parts.port}" if parts.port else "")
        parts = parts._replace(netloc=netloc)
    return urllib.parse.urlunsplit(parts), username, password


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

        camera_type = entry.get("type", "reolink")
        if camera_type not in CAMERA_TYPES:
            raise LiveConfigError(f'{camera_id}: "type" must be one of: {", ".join(CAMERA_TYPES)}.')

        username = entry.get("username", "")
        password = entry.get("password", "")
        if not isinstance(username, str) or not isinstance(password, str):
            raise LiveConfigError(f'{camera_id}: "username" and "password" must be text.')
        if password == PLACEHOLDER_PASSWORD:
            raise LiveConfigError(f'{camera_id}: replace the placeholder "{PLACEHOLDER_PASSWORD}" with the camera\'s real password.')

        stream_url = snapshot_address = live_stream_url = None
        if camera_type == "reolink":
            if not username:
                raise LiveConfigError(f'{camera_id}: "username" is required (Reolink default is "admin").')
            if not password:
                raise LiveConfigError(f'{camera_id}: "password" is required.')
        else:
            if camera_type == "rtsp":
                stream_url, username, password = _clean_url(entry, "rtsp_url", ("rtsp", "rtsps"), camera_id, username, password, required=True)
            else:
                snapshot_address, username, password = _clean_url(entry, "snapshot_url", ("http", "https"), camera_id, username, password, required=True)
                stream_url, username, password = _clean_url(entry, "rtsp_url", ("rtsp", "rtsps"), camera_id, username, password, required=False)
            live_stream_url, username, password = _clean_url(entry, "live_rtsp_url", ("rtsp", "rtsps"), camera_id, username, password, required=False)

        host = entry.get("host")
        if host is None and camera_type != "reolink":
            host = urllib.parse.urlsplit(stream_url or snapshot_address).hostname or ""
        if not isinstance(host, str) or not _HOST_RE.match(host.strip()):
            raise LiveConfigError(f'{camera_id}: "host" must be the camera\'s IP address, e.g. "192.168.1.50".')

        scheme = entry.get("scheme", "http")
        if scheme not in ("http", "https"):
            raise LiveConfigError(f'{camera_id}: "scheme" must be "http" or "https".')
        stream = entry.get("live_stream", "sub")
        if stream not in ("sub", "main"):
            raise LiveConfigError(f'{camera_id}: "live_stream" must be "sub" or "main".')
        decode = entry.get("rtsp_decode", "keyframes")
        if decode not in RTSP_DECODE_MODES:
            raise LiveConfigError(f'{camera_id}: "rtsp_decode" must be "keyframes" or "all".')
        policy = entry.get("frame_policy", "none")
        if policy not in SUPPORTED_FRAME_POLICIES:
            raise LiveConfigError(
                f'{camera_id}: frame_policy "{policy}" is not available yet. '
                f'Supported today: {", ".join(SUPPORTED_FRAME_POLICIES)} (keeps no photos except the newest).'
            )
        port_raw = entry.get("port")
        port = None if port_raw is None else _integer(entry, "port", 0, 1, 65535, camera_id)
        enabled = entry.get("enabled", True)
        if not isinstance(enabled, bool):
            raise LiveConfigError(f'{camera_id}: "enabled" must be true or false.')
        interval = _number(entry, "interval_seconds", 3.0, MIN_STREAM_INTERVAL_SECONDS, MAX_INTERVAL_SECONDS, camera_id)
        if interval < MIN_INTERVAL_SECONDS and not (camera_type == "rtsp" and decode == "all"):
            raise LiveConfigError(
                f'{camera_id}: "interval_seconds" under {MIN_INTERVAL_SECONDS:g} needs "type": "rtsp" '
                'and "rtsp_decode": "all" (several pictures a second only come from a video stream).'
            )
        max_width_raw = entry.get("max_width")
        max_width = None if max_width_raw is None else _integer(entry, "max_width", 0, 160, 7680, camera_id)

        sources[camera_id] = CameraSource(
            camera_id=camera_id,
            host=host.strip(),
            username=username,
            password=password,
            channel=_integer(entry, "channel", 0, 0, 63, camera_id),
            scheme=scheme,
            port=port,
            rtsp_port=_integer(entry, "rtsp_port", 554, 1, 65535, camera_id),
            interval_seconds=interval,
            frame_policy=policy,
            live_fps=_number(entry, "live_fps", 2.0, 0.5, 10.0, camera_id),
            live_stream=stream,
            type=camera_type,
            stream_url=stream_url,
            live_stream_url=live_stream_url,
            snapshot_address=snapshot_address,
            rtsp_decode=decode,
            max_width=max_width,
            enabled=enabled,
        )
    return sources


# How pictures are taken for each kind of app camera. Traffic counting follows
# moving cars (5 pictures a second from the video stream); parking only needs
# the current state of the spaces every few seconds.
KIND_STREAM_SETTINGS = {
    "traffic": {"rtsp_decode": "all", "interval_seconds": 0.2},
    "parking": {"rtsp_decode": "keyframes", "interval_seconds": 3},
}


def _read_raw_config(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise LiveConfigError(f"camera_sources.json is not valid JSON (line {exc.lineno}, column {exc.colno}).")
    if not isinstance(raw, dict):
        raise LiveConfigError("camera_sources.json must be a JSON object keyed by camera id.")
    return raw


def connection_choices(path: Optional[Path] = None) -> list:
    """Cameras whose connection settings can be reused for another app camera
    (id, type and address only -- never the login)."""
    try:
        sources = load_config(path)
    except LiveConfigError:
        return []
    return [
        {"camera_id": cid, "type": src.type, "host": src.host, "enabled": src.enabled}
        for cid, src in sorted(sources.items())
    ]


def entry_for_kind(entry: dict, kind: str) -> dict:
    """A copy of one camera's settings, adjusted to how `kind` takes pictures.
    A Reolink snapshot entry becomes its video stream when used for traffic
    (snapshots can't come 5 times a second)."""
    new = {k: v for k, v in entry.items() if not k.startswith("_") and k != "enabled"}
    camera_type = new.get("type", "reolink")
    if kind == "traffic" and camera_type == "reolink":
        port = new.get("rtsp_port", 554)
        channel = new.get("channel", 0)
        new = {
            "type": "rtsp",
            "rtsp_url": f"rtsp://{new['host']}:{port}/Preview_{channel + 1:02d}_sub",
            "username": new.get("username", ""),
            "password": new.get("password", ""),
        }
        camera_type = "rtsp"
    if camera_type == "rtsp":
        new.update(KIND_STREAM_SETTINGS.get(kind, KIND_STREAM_SETTINGS["parking"]))
    else:
        new["interval_seconds"] = max(1, KIND_STREAM_SETTINGS["parking"]["interval_seconds"])
        new.pop("rtsp_decode", None)
    return new


def copy_connection(from_id: str, to_id: str, kind: str, path: Optional[Path] = None) -> dict:
    """Gives camera `to_id` the same physical camera as `from_id`, set up for
    `kind`, and saves it to camera_sources.json (login stays in that file).
    Raises LiveConfigError with a plain message if it can't."""
    path = path or CONFIG_PATH
    raw = _read_raw_config(path)
    entry = raw.get(from_id)
    if not isinstance(entry, dict) or from_id.startswith("_"):
        raise LiveConfigError(f'"{from_id}" has no connection settings to copy.')
    if to_id in raw:
        raise LiveConfigError(f'"{to_id}" already has connection settings.')
    if not _CAMERA_ID_RE.match(to_id):
        raise LiveConfigError(f'"{to_id}" is not a valid camera id.')
    updated = dict(raw)
    updated[to_id] = entry_for_kind(entry, kind)
    parse_config(updated)  # never save something that won't load
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(json.dumps(updated, indent=2) + "\n", encoding="utf-8")
    try:
        os.chmod(temp, 0o600)  # it holds camera passwords
    except OSError:
        pass
    os.replace(temp, path)
    return updated[to_id]


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
# Getting pictures from each camera type
# ---------------------------------------------------------------------------


class ReolinkFetcher:
    """Reolink's own snapshot command (the original, still-default path)."""

    def __init__(self, source: CameraSource):
        self.source = source

    def fetch(self) -> bytes:
        return fetch_snapshot(self.source)

    def close(self) -> None:
        pass


class HttpSnapshotFetcher:
    """Any "give me a JPEG" web address, e.g. Hikvision's
    /ISAPI/Streaming/channels/101/picture or Dahua's /cgi-bin/snapshot.cgi.
    Handles both basic and digest logins (most recorders use digest)."""

    def __init__(self, source: CameraSource):
        self.source = source
        handlers: list = []
        if source.username:
            passwords = urllib.request.HTTPPasswordMgrWithDefaultRealm()
            passwords.add_password(None, source.snapshot_address, source.username, source.password)
            handlers += [urllib.request.HTTPDigestAuthHandler(passwords), urllib.request.HTTPBasicAuthHandler(passwords)]
        if (source.snapshot_address or "").startswith("https"):
            handlers.append(urllib.request.HTTPSHandler(context=ssl._create_unverified_context()))
        self._opener = urllib.request.build_opener(*handlers)

    def fetch(self) -> bytes:
        request = urllib.request.Request(self.source.snapshot_address, headers={"User-Agent": "ParkingLotPOC"})
        try:
            with self._opener.open(request, timeout=SNAPSHOT_TIMEOUT_SECONDS) as response:
                data = response.read(MAX_FRAME_BYTES + 1)
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403):
                raise LiveFrameError("the camera rejected the username or password (check them in data/camera_sources.json)") from None
            if exc.code == 404:
                raise LiveFrameError("the camera says that snapshot address doesn't exist (HTTP 404): check snapshot_url") from None
            raise LiveFrameError(f"camera answered HTTP {exc.code}") from None
        except urllib.error.URLError as exc:
            raise LiveFrameError(self.source.scrub(_unreachable_message(self.source, exc.reason))) from None
        except (OSError, ValueError) as exc:
            raise LiveFrameError(self.source.scrub(_unreachable_message(self.source, exc))) from None
        if len(data) > MAX_FRAME_BYTES:
            raise LiveFrameError("camera sent an unexpectedly large response")
        if not data.startswith(b"\xff\xd8"):
            snippet = self.source.scrub(data[:160].decode("utf-8", "replace").strip())
            raise LiveFrameError(f"camera did not return a photo: {snippet or 'empty response'}")
        return data

    def close(self) -> None:
        pass


RATE_WINDOW_SECONDS = 10.0
PACED_BACKLOG = 50  # pictures waiting to be handed over ("all" mode); ~10 s at 5/s
PACED_RESYNC_SECONDS = 1.0
# e.g. "[info] Stream #0:0 ..." or "[swscaler @ 0x7f..] [warning] deprecated ..."
_LEVEL_RE = re.compile(
    r"^(\[[^\]]*@[^\]]*\]\s*)?\[(quiet|panic|fatal|error|warning|info|verbose|debug|trace)\]\s*(.*)$"
)
_VIDEO_STREAM_RE = re.compile(r"Stream #\S+.*?: Video: ([^,\s]+).*?(\d{2,5})x(\d{2,5})")
_FPS_RE = re.compile(r"([\d.]+)\s*(fps|tbr)\b")


def _split_ffmpeg_level(line: str) -> tuple:
    """"[info] Stream #0:0 ..." -> ("info", "Stream #0:0 ..."). Lines without
    a level prefix (other ffmpeg builds) are treated as errors, as before."""
    match = _LEVEL_RE.match(line)
    if not match:
        return "error", line
    return match.group(2), (match.group(1) or "") + match.group(3)


def _parse_stream_description(text: str) -> Optional[dict]:
    """Picks the camera's own video size and frame rate out of ffmpeg's
    description of the INPUT stream (the output one is ours, not the camera's)."""
    if "Video:" not in text or "mjpeg" in text.split("Video:", 1)[1][:12]:
        return None  # our own JPEG output stream
    match = _VIDEO_STREAM_RE.search(text)
    if not match:
        return None
    info = {"codec": match.group(1), "stream_width": int(match.group(2)), "stream_height": int(match.group(3))}
    rates = {kind: float(value) for value, kind in _FPS_RE.findall(text)}
    rate = rates.get("fps") or rates.get("tbr")
    if rate and rate < 1000:
        info["stream_fps"] = rate
    return info


def _rate(times: list) -> Optional[float]:
    if len(times) < 2 or times[-1] <= times[0]:
        return None
    return round((len(times) - 1) / (times[-1] - times[0]), 2)


def _rtsp_failure_hint(detail: str) -> str:
    """Turns ffmpeg's last error line into plain advice."""
    low = detail.lower()
    if "401" in low or "unauthorized" in low:
        return "the camera rejected the username or password"
    if "404" in low or "not found" in low or "454" in low:
        return "the camera doesn't have a stream at that address: check the path in rtsp_url"
    if "refused" in low:
        return "the camera refused the connection: check the port and that RTSP is turned on in the camera's settings"
    if "timed out" in low or "no route" in low or "unreachable" in low:
        return "no answer from the camera: check its IP address and network cable"
    return ""


_passthrough_flag_cache: dict = {}


def _passthrough_flags(ffmpeg: str) -> list:
    """Stops ffmpeg duplicating frames to fill a fixed frame rate (it would
    otherwise re-send each key frame over and over). The option was renamed
    in ffmpeg 5.1, so ask the installed ffmpeg which one it knows."""
    if ffmpeg not in _passthrough_flag_cache:
        flags = ["-vsync", "0"]
        try:
            out = subprocess.run([ffmpeg, "-hide_banner", "-h", "long"], capture_output=True, text=True, timeout=10).stdout
            if "-fps_mode" in out:
                flags = ["-fps_mode", "passthrough"]
        except (OSError, subprocess.SubprocessError):
            pass
        _passthrough_flag_cache[ffmpeg] = flags
    return _passthrough_flag_cache[ffmpeg]


def build_rtsp_frame_command(source: CameraSource, url: str) -> list:
    """ffmpeg reading the camera's stream and writing JPEGs, one after
    another, to its output (multipart, each part with its byte length)."""
    ffmpeg = find_ffmpeg()
    # "level+info" prefixes each message with its level, so errors can be told
    # apart from the one-time description of the camera's stream (its size and
    # frame rate), which is read for the status page. -nostats: no progress lines.
    command = [ffmpeg, "-hide_banner", "-nostats", "-loglevel", "level+info", "-nostdin"]
    if url.startswith(("rtsp://", "rtsps://")):
        command += ["-rtsp_transport", "tcp"]
    if source.rtsp_decode == "keyframes":
        # Only decode the stream's complete "key" frames. Cameras send one
        # every 1-4 seconds, which is plenty for parking, at a fraction of
        # the CPU of decoding every frame of a 5MP stream.
        command += ["-skip_frame", "nokey"]
    command += ["-i", url, "-an"]
    if source.rtsp_decode == "all":
        filters = [f"fps={round(1.0 / source.interval_seconds, 4):g}"]
        if source.max_width:
            # Only ever shrink; -2 keeps the height even, as JPEG encoders like.
            filters.append(f"scale='min({source.max_width},iw)':-2")
        command += ["-vf", ",".join(filters)]
    command += _passthrough_flags(ffmpeg)
    command += ["-q:v", "3", "-f", "mpjpeg", "-boundary_tag", "frame", "pipe:1"]
    return command


class RtspFetcher:
    """Keeps one ffmpeg connection open to the camera's stream, (re)connecting
    when needed.

    Two ways of handing pictures over:

    * keyframes mode (parking): fetch() returns the newest picture, skipping
      any older ones -- only the current state of the lot matters.
    * "all" mode (traffic counting, ``paced``): ffmpeg already delivers
      exactly the wanted number of pictures a second, so fetch() returns
      EVERY one of them in order. Taking only the newest lost about a quarter
      of them on a real camera, because pictures come off the network in
      small bursts rather than evenly spaced. Each picture's time is put on
      an even grid (see _paced_time), because a burst's arrival times say
      nothing about when the pictures were actually taken.
    """

    def __init__(self, source: CameraSource, command: Optional[list] = None,
                 first_frame_timeout: float = RTSP_FIRST_FRAME_TIMEOUT_SECONDS,
                 stall_timeout: float = RTSP_STALL_TIMEOUT_SECONDS):
        self.source = source
        self.command = command
        self.first_frame_timeout = first_frame_timeout
        self.stall_timeout = stall_timeout
        self._proc: Optional[subprocess.Popen] = None
        self._cond = threading.Condition()
        self._latest: Optional[bytes] = None
        self._seq = 0
        self._returned_seq = 0
        self._stderr_lines: list = []
        self._reader_error: Optional[str] = None
        # What the camera says its stream is (from ffmpeg's description of it).
        self.stream_info: dict = {}
        # Arrival times of recent pictures from ffmpeg, for a measured rate.
        self._arrivals: list = []
        self.paced = source.rtsp_decode == "all"
        # "all" mode: pictures not handed over yet, oldest first, with the
        # wall-clock time each arrived. Bounded, so a stuck consumer can't
        # eat memory (the oldest are dropped).
        self._pending: "collections.deque" = collections.deque(maxlen=PACED_BACKLOG)
        self._next_time: Optional[float] = None
        self.last_captured_at: Optional[str] = None

    # -- process management ------------------------------------------------
    def _start(self) -> None:
        url = self.source.detection_rtsp_url()
        if not url:
            raise LiveFrameError("no rtsp_url is set for this camera")
        command = self.command or build_rtsp_frame_command(self.source, url)
        try:
            proc = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL)
        except FileNotFoundError:
            raise LiveFrameError("reading camera streams needs ffmpeg, which the server can't find (pip install imageio-ffmpeg)") from None
        with self._cond:
            self._proc = proc
            self._latest = None
            self._pending.clear()
            self._next_time = None
            self._stderr_lines = []
            self._reader_error = None
        threading.Thread(target=self._read_frames, args=(proc,), daemon=True, name=f"rtsp-{self.source.camera_id}").start()
        threading.Thread(target=self._read_stderr, args=(proc,), daemon=True, name=f"rtsp-err-{self.source.camera_id}").start()

    def _read_stderr(self, proc: subprocess.Popen) -> None:
        for raw in iter(proc.stderr.readline, b""):
            line = raw.decode("utf-8", "replace").strip()
            if not line:
                continue
            level, text = _split_ffmpeg_level(line)
            if level not in ("error", "fatal", "panic"):
                # Only errors are kept for failure messages (as with the old
                # "-loglevel error"); info is read for the stream description.
                info = _parse_stream_description(text)
                if info:
                    with self._cond:
                        self.stream_info = info
                continue
            with self._cond:
                self._stderr_lines = (self._stderr_lines + [text])[-20:]

    def _read_frames(self, proc: subprocess.Popen) -> None:
        stream = proc.stdout
        try:
            while True:
                length = None
                # Part headers, ending in a blank line.
                while True:
                    line = stream.readline()
                    if not line:
                        return  # ffmpeg exited
                    line = line.strip()
                    if not line:
                        if length is not None:
                            break
                        continue
                    if line.lower().startswith(b"content-length:"):
                        length = int(line.split(b":", 1)[1])
                if length <= 0 or length > MAX_FRAME_BYTES:
                    raise ValueError(f"bad frame size {length}")
                data = stream.read(length)
                if len(data) < length:
                    return
                if not data.startswith(b"\xff\xd8"):
                    continue
                with self._cond:
                    if self._proc is not proc:
                        return  # replaced by a newer connection
                    self._latest = data
                    self._seq += 1
                    if self.paced:
                        self._pending.append((data, time.time()))
                    now = time.monotonic()
                    self._arrivals.append(now)
                    while self._arrivals and self._arrivals[0] < now - RATE_WINDOW_SECONDS:
                        self._arrivals.pop(0)
                    self._cond.notify_all()
        except (OSError, ValueError) as exc:
            with self._cond:
                self._reader_error = str(exc)
        finally:
            with self._cond:
                self._cond.notify_all()

    def _stop_proc(self) -> None:
        with self._cond:
            proc, self._proc = self._proc, None
        if proc is None:
            return
        if proc.poll() is None:
            try:
                proc.kill()
            except OSError:
                pass
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

    def _failure(self, base: str) -> LiveFrameError:
        with self._cond:
            lines = list(self._stderr_lines)
            reader_error = self._reader_error
        self._stop_proc()
        detail = self.source.scrub(lines[-1] if lines else (reader_error or ""))[-200:]
        hint = _rtsp_failure_hint(detail)
        message = hint or base
        if detail and detail not in message:
            message = f"{message} ({detail})"
        return LiveFrameError(message)

    # -- public ------------------------------------------------------------
    def fetch(self) -> bytes:
        with self._cond:
            running = self._proc is not None and self._proc.poll() is None
        if not running:
            self._stop_proc()
            self._start()
            timeout = self.first_frame_timeout
        else:
            timeout = self.stall_timeout
        deadline = time.monotonic() + timeout
        if self.paced:
            return self._fetch_next_in_order(deadline, timeout)
        with self._cond:
            while self._seq <= self._returned_seq:
                if self._proc is None or self._proc.poll() is not None:
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self._cond.wait(min(remaining, 0.5))
            if self._seq > self._returned_seq and self._latest is not None:
                self._returned_seq = self._seq
                return self._latest
            exited = self._proc is None or self._proc.poll() is not None
        if exited:
            raise self._failure("the camera's video stream stopped")
        raise self._failure(f"no new picture from the camera's video stream for {timeout:g} seconds")

    def _fetch_next_in_order(self, deadline: float, timeout: float) -> bytes:
        with self._cond:
            while not self._pending:
                if self._proc is None or self._proc.poll() is not None:
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self._cond.wait(min(remaining, 0.5))
            if self._pending:
                data, arrived = self._pending.popleft()
                self.last_captured_at = datetime.fromtimestamp(self._paced_time(arrived), timezone.utc).isoformat()
                return data
            exited = self._proc is None or self._proc.poll() is not None
        if exited:
            raise self._failure("the camera's video stream stopped")
        raise self._failure(f"no new picture from the camera's video stream for {timeout:g} seconds")

    def _paced_time(self, arrived: float) -> float:
        """ffmpeg's fps filter makes pictures exactly interval_seconds apart
        in the camera's own time, so consecutive pictures get consecutive
        grid times. The grid is re-anchored to the real clock when it drifts
        more than a second away (a hiccup, or the camera's frame rate
        differing from what was asked for)."""
        expected = self._next_time
        stamp = arrived if expected is None or abs(arrived - expected) > PACED_RESYNC_SECONDS else expected
        self._next_time = stamp + self.source.interval_seconds
        return stamp

    def close(self) -> None:
        self._stop_proc()

    def diagnostics(self) -> dict:
        """What the stream reports about itself, and how many pictures a
        second actually came out of ffmpeg recently."""
        with self._cond:
            info = dict(self.stream_info)
            info["stream_pictures_per_second"] = _rate(self._arrivals)
        return info


def make_fetcher(source: CameraSource):
    if source.type == "rtsp":
        return RtspFetcher(source)
    if source.type == "http_snapshot":
        return HttpSnapshotFetcher(source)
    return ReolinkFetcher(source)


# ---------------------------------------------------------------------------
# Grabber threads
# ---------------------------------------------------------------------------

FrameCallback = Callable[[str, bytes, str], None]


class FrameGrabber(threading.Thread):
    """Fetches a snapshot on a fixed cadence for one camera, backing off on errors."""

    def __init__(self, source: CameraSource, on_frame: Optional[FrameCallback] = None, fetcher: Any = None):
        super().__init__(daemon=True, name=f"grabber-{source.camera_id}")
        self.source = source
        self.on_frame = on_frame
        self.fetcher = fetcher or make_fetcher(source)
        # NOTE: not named _stop -- threading.Thread already uses that name internally.
        self._halt = threading.Event()
        self._lock = threading.Lock()
        self._grab_times: list = []
        self._status: dict = {
            "running": False,
            "started_at": None,
            "last_frame_at": None,
            "last_frame_bytes": None,
            "frames_grabbed": 0,
            "consecutive_failures": 0,
            "last_error": None,
            "last_error_at": None,
            # The last camera problem, kept after it clears (last_error is
            # reset on the next good picture) so a gap can be explained later.
            "last_failure": None,
        }

    def stop(self) -> None:
        self._halt.set()

    def status(self) -> dict:
        with self._lock:
            status = dict(self._status)
        status["configured"] = True
        status["camera_id"] = self.source.camera_id
        status["type"] = self.source.type
        status["interval_seconds"] = self.source.interval_seconds
        status["frame_policy"] = self.source.frame_policy
        status["live_fps"] = self.source.live_fps
        with self._lock:
            status["pictures_per_second"] = _rate(self._grab_times)
        diagnostics = getattr(self.fetcher, "diagnostics", None)
        if diagnostics is not None:
            try:
                status.update(diagnostics())
            except Exception:
                pass
        with _relay_lock:
            status["stream_error"] = _stream_errors.get(self.source.camera_id)
        return status

    def _update(self, **fields: Any) -> None:
        with self._lock:
            self._status.update(fields)

    def run(self) -> None:
        self._update(running=True, started_at=utc_now_iso())
        failures = 0
        paced = bool(getattr(self.fetcher, "paced", False))
        try:
            while not self._halt.is_set():
                started = time.monotonic()
                try:
                    jpeg = self.fetcher.fetch()
                    captured_at = getattr(self.fetcher, "last_captured_at", None) if paced else None
                    captured_at = captured_at or utc_now_iso()
                    write_latest_frame(self.source.camera_id, jpeg)
                    failures = 0
                    with self._lock:
                        now = time.monotonic()
                        self._grab_times.append(now)
                        while self._grab_times and self._grab_times[0] < now - RATE_WINDOW_SECONDS:
                            self._grab_times.pop(0)
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
                    self._update(consecutive_failures=failures, last_error=str(exc), last_failure=str(exc),
                                 last_error_at=utc_now_iso())
                except Exception as exc:
                    failures += 1
                    self._update(
                        consecutive_failures=failures,
                        last_error=self.source.scrub(f"unexpected error: {exc}"),
                        last_error_at=utc_now_iso(),
                    )

                if failures:
                    delay = min(self.source.interval_seconds * (2 ** min(failures, 6)), MAX_BACKOFF_SECONDS)
                elif paced:
                    delay = 0.0  # the stream sets the pace; fetch() waits for the next picture
                else:
                    delay = self.source.interval_seconds
                self._halt.wait(max(0.0, delay - (time.monotonic() - started)))
        finally:
            try:
                self.fetcher.close()
            except Exception:
                pass
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
        for camera_id in [c for c, src in sources.items() if not src.enabled]:
            print(f"Live camera '{camera_id}' is turned off (\"enabled\": false in camera_sources.json).")
            del sources[camera_id]
        for camera_id, source in sources.items():
            grabber = FrameGrabber(source, on_frame)
            _grabbers[camera_id] = grabber
            grabber.start()
    return sorted(sources)


def start_camera(
    camera_id: str,
    on_frame: Optional[FrameCallback] = None,
    config_path: Optional[Path] = None,
) -> bool:
    """Starts one camera's grabber if it has (enabled) settings in the file and
    isn't running yet -- used right after a camera is added on the website, so
    a new camera starts without restarting the server. Returns True if running."""
    global _config_error
    try:
        sources = load_config(config_path)
    except LiveConfigError as exc:
        with _registry_lock:
            _config_error = str(exc)
        return False
    source = sources.get(camera_id)
    if source is None or not source.enabled:
        return False
    with _registry_lock:
        if camera_id in _grabbers:
            return True
        grabber = FrameGrabber(source, on_frame)
        _grabbers[camera_id] = grabber
    grabber.start()
    return True


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


def configured_camera_ids() -> list:
    """Ids of every camera that currently has a running grabber."""
    with _registry_lock:
        return sorted(_grabbers)


def get_config_error() -> Optional[str]:
    """Why camera_sources.json couldn't be used, or None if it's fine."""
    with _registry_lock:
        return _config_error


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
    if source.live_stream == "main" or source.type != "reolink":
        # Keep it browser-sized: shrink anything wider than 1280px.
        video_filter += ",scale='min(1280,iw)':-2"
    url = source.live_view_rtsp_url()
    if not url:
        raise RelayFailed("This camera has no video stream set up for Live View (add an rtsp_url to its settings).")
    transport = ["-rtsp_transport", "tcp"] if url.startswith(("rtsp://", "rtsps://")) else []
    return [
        find_ffmpeg(),
        "-hide_banner", "-loglevel", "error",
        *transport,
        "-i", url,
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
