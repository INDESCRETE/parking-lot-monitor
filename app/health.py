"""Health monitoring and alerts: tells Rob when something breaks, and when it recovers.

What it watches (a check runs every 30 seconds):
* Downtime while the program was off. At startup it looks at when it was last alive.
  If it was down for a while, or stopped without a normal shutdown, it sends a
  "back online, was down from X to Y" alert. The alert also says whether the
  computer rebooted, which usually means a power outage.
* The computer sleeping or freezing. If a 30-second check suddenly comes 10 minutes
  late, the computer wasn't running us in between.
* Each live camera. If one stops sending pictures for ``camera_down_minutes``, you
  get an alert. When pictures come back, you get a "camera back" alert. Traffic
  cameras are checked every few seconds and alert after only
  ``traffic_camera_down_seconds`` (30 by default): every second a traffic camera
  is down, passing vehicles go uncounted.
* Car detection stalling while pictures still arrive (for example, the model crashed).
* The internet. If it drops, alerts wait in an outbox on disk and go out when it
  returns, along with an "internet was down from X to Y" note. Counting never stops
  meanwhile, because it only needs the camera cable.
* The disk filling up.
* Nightly database backup to data/backups/ (the newest few are kept), plus a short
  daily check-in, so a quiet phone means "all fine", not "alerts are broken".

How alerts reach you (settings live in data/alerts.json, created on first run):
* Phone push through ntfy (free, no account). Install the ntfy app and subscribe
  to the topic name printed at startup.
* Email (optional), through any SMTP account such as Gmail with an app password.
* An outside "dead man's switch" (optional, e.g. healthchecks.io). We ping it every
  minute. If the pings stop because the power, the internet or this computer is
  down, *that service* alerts you. A dead computer can't send its own alert, so
  this is the only way to learn about an outage while it is still going on.

Everything here is stdlib only and is written so no failure inside the health monitor
can take down the parking monitor itself.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import shutil
import smtplib
import sqlite3
import ssl
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from pathlib import Path
from typing import Any, Callable, Optional

ROOT = Path(__file__).resolve().parents[1]
APP_DIR = ROOT / "app"
DATA_DIR = ROOT / "data"
CONFIG_PATH = DATA_DIR / "alerts.json"
HEALTH_DIR = DATA_DIR / "health"
BACKUP_DIR = DATA_DIR / "backups"

CHECK_INTERVAL_SECONDS = 30.0
# Cameras are checked more often than everything else, so a traffic camera's
# short alert delay actually means something.
CAMERA_CHECK_INTERVAL_SECONDS = 5.0
HEARTBEAT_INTERVAL_SECONDS = 60.0
SEND_RETRY_SECONDS = 30.0
SEND_TIMEOUT_SECONDS = 10.0
MAX_OUTBOX = 200
LATE_DELIVERY_SECONDS = 120.0

DEFAULT_CONFIG: dict = {
    "site_name": "Parking lot monitor",
    "ntfy_server": "https://ntfy.sh",
    "ntfy_topic": "",
    "heartbeat_url": "",
    "email": {
        "enabled": False,
        "smtp_host": "smtp.gmail.com",
        "smtp_port": 587,
        "username": "",
        "password": "",
        "to": "",
    },
    "camera_down_minutes": 5,
    "detection_stall_minutes": 10,
    "traffic_camera_down_seconds": 30,
    "traffic_detection_stall_seconds": 60,
    "outage_report_minutes": 2,
    "disk_warn_percent": 85,
    "backup_hour": 3,
    "backups_to_keep": 7,
    "daily_checkin_hour": 8,
}

_NUMBER_LIMITS = {
    "camera_down_minutes": (1, 1440),
    "detection_stall_minutes": (1, 1440),
    "traffic_camera_down_seconds": (10, 3600),
    "traffic_detection_stall_seconds": (15, 3600),
    "outage_report_minutes": (0.5, 1440),
    "disk_warn_percent": (10, 99),
    "backup_hour": (0, 23),
    "backups_to_keep": (1, 365),
    "daily_checkin_hour": (0, 23),
}


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def code_fingerprints(app_dir: Path = APP_DIR) -> dict:
    """A short hash of each of the program's .py files. Compared with the
    previous run's at startup: if any changed, the restart was a software
    update (whoever installed it), not the program or the camera failing."""
    prints = {}
    for path in sorted(app_dir.glob("*.py")):
        try:
            prints[path.name] = hashlib.sha1(path.read_bytes()).hexdigest()[:12]
        except OSError:
            pass
    return prints


# Why the program (re)started, as shown next to gaps in the data.
STARTUP_LABELS = {
    "first_start": "Monitoring started for the first time",
    "update": "Software update (program restarted to install it)",
    "restart": "Program restarted",
    "stopped": "Program was stopped",
    "crash": "Program crashed or was force-quit",
    "reboot": "Computer was restarted",
    "power": "Computer lost power",
}


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def parse_iso(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def fmt_local(moment: datetime) -> str:
    """'Sep 24, 2:14 PM' in this computer's time zone."""
    local = moment.astimezone()
    hour = local.hour % 12 or 12
    return f"{local:%b} {local.day}, {hour}:{local:%M} {'AM' if local.hour < 12 else 'PM'}"


def fmt_span(start: datetime, end: datetime) -> str:
    """'from Sep 24, 2:14 PM to 3:02 PM' (the date is left off the end if it's the same day)."""
    same_day = start.astimezone().date() == end.astimezone().date()
    end_text = fmt_local(end).split(", ", 1)[1] if same_day else fmt_local(end)
    return f"from {fmt_local(start)} to {end_text}"


def fmt_duration(seconds: float) -> str:
    seconds = max(0, int(round(seconds)))
    if seconds < 60:
        return f"{seconds} sec"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes} min"
    hours, minutes = divmod(minutes, 60)
    if hours < 48:
        return f"{hours} h {minutes} min" if minutes else f"{hours} h"
    days, hours = divmod(hours, 24)
    return f"{days} days {hours} h"


def _atomic_write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.replace(temp, path)


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def system_boot_time() -> Optional[datetime]:
    """When this computer last booted, or None if we can't tell."""
    try:
        if sys.platform.startswith("linux"):
            for line in Path("/proc/stat").read_text().splitlines():
                if line.startswith("btime "):
                    return datetime.fromtimestamp(int(line.split()[1]), timezone.utc)
        elif sys.platform == "darwin":
            out = subprocess.run(
                ["sysctl", "-n", "kern.boottime"], capture_output=True, text=True, timeout=5
            ).stdout
            match = re.search(r"sec\s*=\s*(\d+)", out)
            if match:
                return datetime.fromtimestamp(int(match.group(1)), timezone.utc)
    except Exception:
        pass
    return None


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


def load_config(path: Optional[Path] = None) -> tuple:
    """Returns (config, error). Creates the file with a fresh private ntfy topic on
    first run. A broken file is never overwritten: defaults are used and the error is
    reported instead."""
    path = path or CONFIG_PATH
    config = json.loads(json.dumps(DEFAULT_CONFIG))
    if not path.exists():
        config["ntfy_topic"] = f"parking-{secrets.token_hex(6)}"
        try:
            _atomic_write_json(path, config)
        except OSError as exc:
            return config, f"could not create {path.name}: {exc}"
        return config, None
    raw = _read_json(path)
    if not isinstance(raw, dict):
        return config, f"{path.name} is not valid JSON; using default settings (no alerts sent) until it is fixed"
    problems = []
    for key, value in raw.items():
        if key.startswith("_"):
            continue
        if key in _NUMBER_LIMITS:
            low, high = _NUMBER_LIMITS[key]
            if value is None and key == "daily_checkin_hour":
                config[key] = None
            elif isinstance(value, bool) or not isinstance(value, (int, float)) or not low <= value <= high:
                problems.append(f'"{key}" must be a number from {low:g} to {high:g}')
            else:
                config[key] = value
        elif key == "email":
            if isinstance(value, dict):
                config["email"].update(value)
            else:
                problems.append('"email" must be an object')
        elif key in config:
            config[key] = value if isinstance(value, str) else config[key]
    return config, ("; ".join(problems) or None)


# ---------------------------------------------------------------------------
# Delivery: ntfy push + email, with an on-disk outbox for when the internet is down
# ---------------------------------------------------------------------------


def _header_safe(text: str) -> str:
    return text.encode("latin-1", "replace").decode("latin-1")


def send_ntfy(config: dict, title: str, message: str, priority: str, tags: str) -> None:
    server = (config.get("ntfy_server") or "https://ntfy.sh").rstrip("/")
    request = urllib.request.Request(
        f"{server}/{config['ntfy_topic']}",
        data=message.encode("utf-8"),
        method="POST",
        headers={"Title": _header_safe(title), "Priority": priority, "Tags": tags},
    )
    with urllib.request.urlopen(request, timeout=SEND_TIMEOUT_SECONDS) as response:
        response.read()


def send_email(config: dict, title: str, message: str) -> None:
    email = config["email"]
    msg = EmailMessage()
    msg["Subject"] = title
    msg["From"] = email.get("from") or email["username"]
    msg["To"] = email["to"]
    msg.set_content(message)
    port = int(email.get("smtp_port") or 587)
    context = ssl.create_default_context()
    if port == 465:
        with smtplib.SMTP_SSL(email["smtp_host"], port, timeout=SEND_TIMEOUT_SECONDS, context=context) as smtp:
            smtp.login(email["username"], email["password"])
            smtp.send_message(msg)
    else:
        with smtplib.SMTP(email["smtp_host"], port, timeout=SEND_TIMEOUT_SECONDS) as smtp:
            smtp.starttls(context=context)
            smtp.login(email["username"], email["password"])
            smtp.send_message(msg)


def configured_channels(config: dict) -> list:
    channels = []
    if config.get("ntfy_topic"):
        channels.append("push")
    email = config.get("email") or {}
    if email.get("enabled") and email.get("smtp_host") and email.get("username") and email.get("password") and email.get("to"):
        channels.append("email")
    return channels


# Alert history: every alert is kept for HISTORY_KEEP_DAYS, then deleted,
# unless it's been saved. Saved alerts stay until they're unsaved. An alert
# unsaved after it's already past HISTORY_KEEP_DAYS gets UNSAVE_GRACE_HOURS
# before it goes, so an accidental unsave can be undone.
HISTORY_KEEP_DAYS = 30
UNSAVE_GRACE_HOURS = 24
MAX_HISTORY = 5000  # safety cap; the oldest unsaved alerts go first


class AlertHistory:
    """Every alert ever sent, stored in data/health/alerts.json, each with a
    "saved" flag. Thread-safe."""

    def __init__(self, path: Path, clock: Callable[[], datetime] = utc_now, legacy_logs: tuple = ()):
        self.path = path
        self.clock = clock
        self._lock = threading.Lock()
        loaded = _read_json(path)
        if isinstance(loaded, list):
            self._items: list = [a for a in loaded if isinstance(a, dict) and a.get("id")]
        else:
            self._items = self._import_legacy(legacy_logs)
            self._save()

    @staticmethod
    def _import_legacy(paths: tuple) -> list:
        """Carries over alerts from the older one-line-per-alert log files."""
        items = []
        for path in paths:
            try:
                lines = Path(path).read_text(encoding="utf-8").splitlines()
            except OSError:
                continue
            for line in lines:
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue
                if isinstance(entry, dict) and entry.get("created_at"):
                    items.append(
                        {
                            "id": secrets.token_hex(6),
                            "created_at": entry["created_at"],
                            "kind": entry.get("kind", "info"),
                            "title": entry.get("title", ""),
                            "message": entry.get("message", ""),
                            "priority": entry.get("priority", "default"),
                            "saved": False,
                            "unsaved_at": None,
                        }
                    )
        items.sort(key=lambda a: a["created_at"])
        return items

    def _save(self) -> None:
        try:
            _atomic_write_json(self.path, self._items)
        except OSError:
            pass

    def add(self, alert: dict) -> dict:
        entry = {k: alert[k] for k in ("id", "created_at", "kind", "title", "message", "priority")}
        entry.update(saved=False, unsaved_at=None)
        with self._lock:
            self._items.append(entry)
            if len(self._items) > MAX_HISTORY:
                unsaved = [a for a in self._items if not a["saved"]]
                drop = {a["id"] for a in unsaved[: len(self._items) - MAX_HISTORY]}
                self._items = [a for a in self._items if a["id"] not in drop]
            self._save()
        return dict(entry)

    def delete_at(self, entry: dict) -> Optional[datetime]:
        """When this alert will be deleted, or None while it's saved."""
        if entry.get("saved"):
            return None
        created = parse_iso(entry["created_at"]) or self.clock()
        when = created + timedelta(days=HISTORY_KEEP_DAYS)
        unsaved_at = parse_iso(entry.get("unsaved_at"))
        if unsaved_at is not None:
            when = max(when, unsaved_at + timedelta(hours=UNSAVE_GRACE_HOURS))
        return when

    def set_saved(self, alert_id: str, saved: bool) -> Optional[dict]:
        with self._lock:
            for entry in self._items:
                if entry["id"] == alert_id:
                    if saved:
                        entry["saved"], entry["unsaved_at"] = True, None
                    elif entry.get("saved"):
                        entry["saved"], entry["unsaved_at"] = False, self.clock().isoformat()
                    self._save()
                    return self._view(entry)
        return None

    def purge(self) -> int:
        """Deletes every alert whose time is up. Returns how many went."""
        now = self.clock()
        with self._lock:
            keep = [a for a in self._items if a.get("saved") or self.delete_at(a) > now]
            removed = len(self._items) - len(keep)
            if removed:
                self._items = keep
                self._save()
        return removed

    def _view(self, entry: dict) -> dict:
        view = dict(entry)
        when = self.delete_at(entry)
        view["delete_at"] = when.isoformat() if when else None
        return view

    def list(self, limit: Optional[int] = None) -> list:
        """Newest first, each with its delete_at time."""
        with self._lock:
            items = [self._view(a) for a in reversed(self._items)]
        return items[:limit] if limit else items

    def counts(self) -> dict:
        with self._lock:
            return {"total": len(self._items), "saved": sum(1 for a in self._items if a.get("saved"))}


class Notifier:
    """Queues alerts on disk and keeps retrying until every channel has them."""

    def __init__(self, config: dict, health_dir: Path, senders: Optional[dict] = None, clock: Callable[[], datetime] = utc_now):
        self.config = config
        self.clock = clock
        self.health_dir = health_dir
        self.outbox_path = health_dir / "outbox.json"
        self.log_path = health_dir / "alerts.log"  # older format, imported once into history
        self.history = AlertHistory(
            health_dir / "alerts.json",
            clock,
            legacy_logs=(self.log_path.with_suffix(".log.1"), self.log_path),
        )
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._halt = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.last_send_error: Optional[str] = None
        self.last_sent_at: Optional[str] = None
        self._senders = senders or {
            "push": lambda a: send_ntfy(self.config, a["title"], self._body(a), a["priority"], a["tags"]),
            "email": lambda a: send_email(self.config, a["title"], self._body(a)),
        }
        loaded = _read_json(self.outbox_path)
        self._outbox: list = loaded if isinstance(loaded, list) else []

    # -- queueing ---------------------------------------------------------
    def notify(self, title: str, message: str, priority: str = "default", tags: str = "", kind: str = "info") -> dict:
        alert = {
            "id": secrets.token_hex(6),
            "created_at": self.clock().isoformat(),
            "kind": kind,
            "title": f"{self.config.get('site_name') or 'Parking lot monitor'}: {title}",
            "message": message,
            "priority": priority,
            "tags": tags,
            "delivered": [],
        }
        print(f"[health] {alert['title']} -- {message}", flush=True)
        self.history.add(alert)
        with self._lock:
            if configured_channels(self.config):
                self._outbox.append(alert)
                del self._outbox[:-MAX_OUTBOX]  # never let a long outage grow this forever
                self._save_outbox()
        self._wake.set()
        return alert

    def log(self, title: str, message: str, kind: str = "info") -> dict:
        """Writes to the alert history only: no phone push, no email."""
        alert = {
            "id": secrets.token_hex(6),
            "created_at": self.clock().isoformat(),
            "kind": kind,
            "title": f"{self.config.get('site_name') or 'Parking lot monitor'}: {title}",
            "message": message,
            "priority": "min",
        }
        print(f"[health] {alert['title']} -- {message}", flush=True)
        return self.history.add(alert)

    def pending(self) -> list:
        with self._lock:
            return [dict(a) for a in self._outbox]

    def recent(self, limit: Optional[int] = 20) -> list:
        return self.history.list(limit)

    def _save_outbox(self) -> None:
        try:
            _atomic_write_json(self.outbox_path, self._outbox)
        except OSError:
            pass

    def _body(self, alert: dict) -> str:
        created = parse_iso(alert["created_at"]) or self.clock()
        if (self.clock() - created).total_seconds() > LATE_DELIVERY_SECONDS:
            return f"{alert['message']}\n\n(Sent late. This happened at {fmt_local(created)} while the internet was down.)"
        return alert["message"]

    # -- delivery ---------------------------------------------------------
    def flush(self) -> bool:
        """Tries to deliver everything pending. Returns True if nothing is left."""
        channels = configured_channels(self.config)
        with self._lock:
            batch = list(self._outbox)
        for alert in batch:
            for channel in channels:
                if channel in alert["delivered"]:
                    continue
                try:
                    self._senders[channel](alert)
                except Exception as exc:
                    self.last_send_error = f"{channel}: {exc}"
                    continue
                alert["delivered"].append(channel)
                self.last_sent_at = self.clock().isoformat()
                self.last_send_error = None
        with self._lock:
            done = {a["id"] for a in batch if all(c in a["delivered"] for c in channels)}
            self._outbox = [a for a in self._outbox if a["id"] not in done]
            self._save_outbox()
            return not self._outbox

    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, daemon=True, name="alert-sender")
            self._thread.start()

    def stop(self) -> None:
        self._halt.set()
        self._wake.set()

    def _run(self) -> None:
        while not self._halt.is_set():
            try:
                self.flush()
            except Exception as exc:
                self.last_send_error = f"sender error: {exc}"
            self._wake.wait(SEND_RETRY_SECONDS)
            self._wake.clear()


# ---------------------------------------------------------------------------
# The monitor
# ---------------------------------------------------------------------------

CameraStatusFn = Callable[[], list]


class HealthMonitor:
    """Runs every check on a background thread. ``camera_statuses`` returns one dict
    per live camera: ``{"camera_id", "name", "grabber": live.get_status(...),
    "detection": live_detector.status(...), "has_spaces": bool}``."""

    def __init__(
        self,
        db_path: Path,
        camera_statuses: CameraStatusFn,
        config_error_fn: Callable[[], Optional[str]] = lambda: None,
        config_path: Optional[Path] = None,
        health_dir: Optional[Path] = None,
        backup_dir: Optional[Path] = None,
        senders: Optional[dict] = None,
        internet_check: Optional[Callable[[], bool]] = None,
        clock: Callable[[], datetime] = utc_now,
        code_fingerprints_fn: Callable[[], dict] = code_fingerprints,
    ):
        self.db_path = db_path
        self.code_fingerprints_fn = code_fingerprints_fn
        # Why this run started (set by load_previous_state); see STARTUP_LABELS.
        self.startup: dict = {"reason": "restart", "label": STARTUP_LABELS["restart"], "detail": "",
                              "down_since": None, "started_at": None}
        self.camera_statuses = camera_statuses
        self.camera_config_error = config_error_fn
        self.health_dir = health_dir or HEALTH_DIR
        self.backup_dir = backup_dir or BACKUP_DIR
        self.state_path = self.health_dir / "state.json"
        self.config, self.config_error = load_config(config_path)
        self.notifier = Notifier(self.config, self.health_dir, senders, clock)
        self._internet_check = internet_check or self._default_internet_check
        self.clock = clock
        self.started_at = clock()
        self._halt = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._last_tick_wall: Optional[float] = None
        self._last_heartbeat = 0.0
        self._problems: dict = {}  # key -> {"since", "message"}
        self._camera_check_lock = threading.Lock()
        self._camera_since: dict = {}
        self.internet_online: Optional[bool] = None
        self._offline_since: Optional[datetime] = None
        self.last_check_error: Optional[str] = None
        self._state: dict = {}

    # -- lifecycle --------------------------------------------------------
    def start(self) -> None:
        self.load_previous_state()
        if self.config_error:
            self.notifier.notify("Alert settings problem", f"data/alerts.json: {self.config_error}", "high", "warning", "config")
        channels = configured_channels(self.config)
        if "push" in channels:
            print(f"[health] Phone alerts: install the ntfy app and subscribe to topic '{self.config['ntfy_topic']}'", flush=True)
        self.notifier.start()
        self._thread = threading.Thread(target=self._run, daemon=True, name="health-monitor")
        self._thread.start()
        threading.Thread(target=self._run_camera_checks, daemon=True, name="health-cameras").start()

    def load_previous_state(self) -> None:
        """Reads what the last run left behind, reports any downtime, and picks up
        problems that were already alerted on, so a restart doesn't repeat them."""
        self.health_dir.mkdir(parents=True, exist_ok=True)
        previous = _read_json(self.state_path)
        self._state = previous if isinstance(previous, dict) else {}
        saved = self._state.get("problems")
        if isinstance(saved, dict):
            with self._lock:
                self._problems = {k: v for k, v in saved.items() if isinstance(v, dict) and "since" in v}
        fingerprints = self.code_fingerprints_fn()
        self._report_previous_downtime(previous if isinstance(previous, dict) else None, fingerprints)
        self._state.update(
            started_at=self.started_at.isoformat(), clean_shutdown=False, pid=os.getpid(),
            code_fingerprints=fingerprints, startup=self.startup,
        )
        self._save_state()

    def stop(self) -> None:
        self._halt.set()
        self.notifier.stop()

    def mark_clean_shutdown(self) -> None:
        """Called on a normal stop (Ctrl+C, service stop, dev-server restart)."""
        self._state.update(last_alive_at=self.clock().isoformat(), clean_shutdown=True)
        self._save_state()

    def _save_state(self) -> None:
        with self._lock:
            self._state["problems"] = {k: dict(v) for k, v in self._problems.items()}
        try:
            _atomic_write_json(self.state_path, self._state)
        except OSError as exc:
            self.last_check_error = f"could not save health state: {exc}"

    # -- downtime while we were not running --------------------------------
    def _set_startup(self, reason: str, since: Optional[datetime], detail: str = "") -> None:
        self.startup = {
            "reason": reason,
            "label": STARTUP_LABELS.get(reason, reason),
            "detail": detail,
            "down_since": since.isoformat() if since else None,
            "started_at": self.started_at.isoformat(),
        }

    def _report_previous_downtime(self, previous: Optional[dict], fingerprints: Optional[dict] = None) -> None:
        now = self.clock()
        if previous is None:
            self._set_startup("first_start", None)
            self.notifier.notify(
                "Alerts are on",
                "Monitoring started. This is where alerts will show up if a camera, the internet, "
                "the power or this computer has a problem, and again when it recovers.",
                "default", "white_check_mark", "started",
            )
            return
        last_alive = parse_iso(previous.get("last_alive_at"))
        if last_alive is None:
            self._set_startup("restart", None)
            return
        gap = (now - last_alive).total_seconds()
        clean = bool(previous.get("clean_shutdown"))
        boot = system_boot_time()
        rebooted = boot is not None and last_alive < boot <= now
        old_prints = previous.get("code_fingerprints")
        changed = []
        if isinstance(old_prints, dict) and fingerprints:
            changed = sorted(name for name in set(old_prints) | set(fingerprints)
                             if old_prints.get(name) != fingerprints.get(name))
        updated = bool(changed) and not rebooted
        if rebooted and not clean:
            reason = "power"
            cause = "The computer shut off without warning and restarted by itself. This is most likely a power outage."
        elif rebooted:
            reason = "reboot"
            cause = "The computer was restarted."
        elif updated:
            reason = "update"
            cause = "The program restarted to install a software update" + ("" if clean else ", and didn't shut down cleanly") + "."
        elif not clean:
            reason = "crash"
            cause = "The monitoring program stopped unexpectedly (a crash or forced quit) and has been restarted."
        elif gap < self.config["outage_report_minutes"] * 60:
            reason = "restart"
            cause = "The monitoring program was restarted."
        else:
            reason = "stopped"
            cause = "The monitoring program was stopped, then started again."
        detail = f"Changed: {', '.join(changed)}" if updated else ""
        self._set_startup(reason, last_alive, detail)
        quick_and_clean = clean and gap < self.config["outage_report_minutes"] * 60
        if quick_and_clean:
            # Not worth a phone alert, but written to the alert history so a
            # look back at "how often did it go down" can tell our own
            # restarts (updates) apart from real problems.
            self.notifier.log(
                "Restarted for a software update" if updated else "Program restarted",
                f"Monitoring paused {fmt_span(last_alive, now)} ({fmt_duration(gap)}). {cause}"
                + (f" {detail}." if detail else ""),
                "update" if updated else "restart",
            )
            return
        self.notifier.notify(
            "Back online",
            f"Monitoring was down {fmt_span(last_alive, now)} ({fmt_duration(gap)}). "
            f"{cause} Nothing was counted during that time; the reports show it as a gap."
            + (f" {detail}." if detail else ""),
            "high" if not clean else "default",
            "white_check_mark",
            "back_online",
        )

    # -- main loop ----------------------------------------------------------
    def _run(self) -> None:
        while not self._halt.is_set():
            self.tick()
            self._halt.wait(CHECK_INTERVAL_SECONDS)

    def _run_camera_checks(self) -> None:
        while not self._halt.wait(CAMERA_CHECK_INTERVAL_SECONDS):
            try:
                self._check_cameras()
            except Exception as exc:
                self.last_check_error = f"_check_cameras: {exc}"

    def tick(self) -> None:
        """One round of checks. Public so tests can drive it directly."""
        steps = (
            self._check_sleep,
            self._record_alive,
            self._check_cameras,
            self._check_camera_config,
            self._check_disk,
            self._heartbeat_and_internet,
            self._maybe_backup,
            self._maybe_daily_checkin,
            self.notifier.history.purge,
        )
        for step in steps:
            try:
                step()
            except Exception as exc:  # the monitor must never die
                self.last_check_error = f"{step.__name__}: {exc}"

    def _record_alive(self) -> None:
        self._state["last_alive_at"] = self.clock().isoformat()
        self._save_state()

    def _check_sleep(self) -> None:
        now_wall = time.time()
        previous, self._last_tick_wall = self._last_tick_wall, now_wall
        if previous is None:
            return
        gap = now_wall - previous
        limit = max(self.config["outage_report_minutes"] * 60, CHECK_INTERVAL_SECONDS * 4)
        if gap > limit:
            now = self.clock()
            start = now - timedelta(seconds=gap)
            self.notifier.notify(
                "Monitoring paused",
                f"This computer was asleep or frozen {fmt_span(start, now)} "
                f"({fmt_duration(gap)}). Nothing was counted during that time. Turn off sleep in "
                "the computer's energy settings.",
                "high", "warning", "sleep",
            )

    # -- problems that open and later resolve ------------------------------
    def _open_problem(self, key: str, since: datetime, title: str, message: str, priority: str = "high") -> None:
        with self._lock:
            if key in self._problems:
                return
            self._problems[key] = {"since": since.isoformat(), "title": title, "message": message}
        self._save_state()
        self.notifier.notify(title, message, priority, "warning", key.split(":")[0])

    def _resolve_problem(self, key: str, title: str, message_fn: Callable[[datetime, datetime], str]) -> None:
        with self._lock:
            problem = self._problems.pop(key, None)
        if problem is None:
            return
        self._save_state()
        since = parse_iso(problem["since"]) or self.clock()
        self.notifier.notify(title, message_fn(since, self.clock()), "default", "white_check_mark", key.split(":")[0] + "_ok")

    def problems(self) -> list:
        with self._lock:
            return [dict(v, key=k) for k, v in self._problems.items()]

    # -- cameras -------------------------------------------------------------
    def _check_cameras(self) -> None:
        # Runs from both the main loop and the fast camera loop.
        with self._camera_check_lock:
            self._check_cameras_locked()

    def _check_cameras_locked(self) -> None:
        now = self.clock()
        seen = set()
        for cam in self.camera_statuses():
            camera_id = cam["camera_id"]
            seen.add(camera_id)
            name = cam.get("name") or camera_id
            grabber = cam.get("grabber") or {}
            if not grabber.get("configured"):
                continue
            traffic = cam.get("kind") == "traffic"
            if traffic:
                down_after = self.config["traffic_camera_down_seconds"]
                stall_after = self.config["traffic_detection_stall_seconds"]
                impact = "Vehicles passing now are NOT being counted."
            else:
                down_after = self.config["camera_down_minutes"] * 60
                stall_after = self.config["detection_stall_minutes"] * 60
                impact = "Counting for this camera is paused."
            last_frame = parse_iso(grabber.get("last_frame_at"))
            reference = last_frame or parse_iso(grabber.get("started_at")) or self.started_at
            reference = max(reference, self.started_at)  # don't blame the camera for our own downtime
            key = f"camera:{camera_id}"
            if (now - reference).total_seconds() >= down_after:
                error = grabber.get("last_error") or "no reason given"
                since = last_frame or reference
                self._open_problem(
                    key, since, f"Camera '{name}' is down",
                    f"No pictures from camera '{name}' since {fmt_local(since)}. Last error: {error}. "
                    f"{impact} It will reconnect by itself when the camera is "
                    "back. If it doesn't, check the camera's cable and power.",
                )
            elif last_frame is not None:
                self._resolve_problem(
                    key, f"Camera '{name}' is back",
                    lambda since, now, name=name: f"Camera '{name}' is sending pictures again. It was out "
                    f"{fmt_span(since, now)} ({fmt_duration((now - since).total_seconds())}).",
                )

            # Detection stalled: pictures arrive but nothing gets analysed.
            detect_key = f"detection:{camera_id}"
            if cam.get("paused"):
                # Switched off on purpose from the website: not a problem.
                with self._lock:
                    dropped = self._problems.pop(detect_key, None)
                if dropped is not None:
                    self._save_state()
                continue
            camera_ok = last_frame is not None and (now - last_frame).total_seconds() < down_after
            detection = cam.get("detection") or {}
            last_detect = parse_iso(detection.get("last_frame_at"))
            detect_ref = max(last_detect or self.started_at, self.started_at)
            resumed_at = parse_iso(cam.get("paused_changed_at"))
            if resumed_at is not None:
                detect_ref = max(detect_ref, resumed_at)  # time since it was switched back on
            if camera_ok and (now - detect_ref).total_seconds() >= stall_after:
                error = detection.get("last_error") or "no error reported"
                self._open_problem(
                    detect_key, detect_ref, f"Car detection stopped for '{name}'",
                    f"Camera '{name}' is sending pictures, but none have been analysed since "
                    f"{fmt_local(detect_ref)}. Last error: {error}. Restarting the program usually fixes this.",
                )
            elif last_detect is not None and (now - last_detect).total_seconds() < stall_after:
                self._resolve_problem(
                    detect_key, f"Car detection working again for '{name}'",
                    lambda since, now: f"Pictures are being analysed again (stopped for "
                    f"{fmt_duration((now - since).total_seconds())}).",
                )
        # A camera removed from the site should not leave a problem open forever.
        with self._lock:
            for key in [k for k in self._problems if k.split(":", 1)[0] in ("camera", "detection")]:
                if key.split(":", 1)[1] not in seen:
                    self._problems.pop(key, None)

    def _check_camera_config(self) -> None:
        error = self.camera_config_error()
        if error:
            self._open_problem(
                "camera_config", self.clock(), "Camera settings problem",
                f"data/camera_sources.json has a problem, so no live camera is running: {error}",
            )
        else:
            self._resolve_problem("camera_config", "Camera settings fixed", lambda s, n: "Live cameras are running again.")

    # -- disk ------------------------------------------------------------------
    def disk_usage(self) -> dict:
        usage = shutil.disk_usage(self.db_path.parent if self.db_path.parent.exists() else ROOT)
        db_bytes = self.db_path.stat().st_size if self.db_path.exists() else 0
        return {
            "percent_used": round(100 * usage.used / usage.total, 1),
            "free_gb": round(usage.free / 1e9, 1),
            "database_mb": round(db_bytes / 1e6, 1),
        }

    def _check_disk(self) -> None:
        disk = self.disk_usage()
        warn = self.config["disk_warn_percent"]
        if disk["percent_used"] >= warn:
            self._open_problem(
                "disk", self.clock(), "Disk almost full",
                f"The disk is {disk['percent_used']}% full ({disk['free_gb']} GB free; the database is "
                f"{disk['database_mb']} MB). If it fills up, new data can't be saved.",
            )
        elif disk["percent_used"] < warn - 5:
            self._resolve_problem("disk", "Disk space OK", lambda s, n: f"The disk is back down to {disk['percent_used']}% full.")

    # -- heartbeat + internet --------------------------------------------------
    def _default_internet_check(self) -> bool:
        url = self.config.get("heartbeat_url") or ""
        if not url and self.config.get("ntfy_topic"):
            url = (self.config.get("ntfy_server") or "https://ntfy.sh").rstrip("/") + "/v1/health"
        if not url:
            return True  # nothing configured to talk to, so nothing to judge
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "ParkingLotPOC"}), timeout=SEND_TIMEOUT_SECONDS) as r:
                r.read(1024)
            return True
        except (urllib.error.HTTPError,) as exc:
            return exc.code < 500  # the server answered, so the internet works
        except Exception:
            return False

    def _heartbeat_and_internet(self) -> None:
        now_mono = time.monotonic()
        if self._last_heartbeat and now_mono - self._last_heartbeat < HEARTBEAT_INTERVAL_SECONDS:
            return
        self._last_heartbeat = now_mono
        online = self._internet_check()
        now = self.clock()
        if not online:
            if self._offline_since is None:
                self._offline_since = now
        else:
            if self._offline_since is not None:
                gap = (now - self._offline_since).total_seconds()
                if gap >= self.config["outage_report_minutes"] * 60:
                    self.notifier.notify(
                        "Internet back",
                        f"The internet connection was down about {fmt_span(self._offline_since, now)} "
                        f"({fmt_duration(gap)}). Counting kept going the whole time; only alerts were delayed.",
                        "default", "white_check_mark", "internet_ok",
                    )
                self._offline_since = None
            self.notifier._wake.set()  # push out anything queued while offline
        self.internet_online = online

    # -- backups -----------------------------------------------------------------
    def backups(self) -> list:
        if not self.backup_dir.exists():
            return []
        return sorted(self.backup_dir.glob("parking_lot-*.sqlite"))

    def backup_now(self) -> Path:
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        stamp = self.clock().astimezone().strftime("%Y-%m-%d")
        target = self.backup_dir / f"parking_lot-{stamp}.sqlite"
        temp = target.with_name(target.name + ".tmp")
        if temp.exists():
            temp.unlink()
        source = sqlite3.connect(str(self.db_path), timeout=30)
        try:
            dest = sqlite3.connect(str(temp))
            try:
                source.backup(dest)  # consistent copy even while the server is writing
                check = dest.execute("PRAGMA quick_check").fetchone()[0]
            finally:
                dest.close()
        finally:
            source.close()
        if check != "ok":
            temp.unlink(missing_ok=True)
            raise RuntimeError(f"backup copy failed its integrity check: {check}")
        os.replace(temp, target)
        for old in self.backups()[: -int(self.config["backups_to_keep"])]:
            old.unlink(missing_ok=True)
        return target

    def _maybe_backup(self) -> None:
        if not self.db_path.exists():
            return
        local = self.clock().astimezone()
        if local.hour < self.config["backup_hour"]:
            return
        today = local.strftime("%Y-%m-%d")
        if (self.backup_dir / f"parking_lot-{today}.sqlite").exists() or self._state.get("backup_failed_on") == today:
            return
        try:
            self.backup_now()
        except Exception as exc:
            self._state["backup_failed_on"] = today  # retry tomorrow rather than every 30 seconds
            self.notifier.notify("Backup failed", f"Tonight's database backup failed: {exc}", "high", "warning", "backup")

    # -- daily check-in ------------------------------------------------------------
    def _maybe_daily_checkin(self) -> None:
        hour = self.config.get("daily_checkin_hour")
        if hour is None:
            return
        local = self.clock().astimezone()
        today = local.strftime("%Y-%m-%d")
        if local.hour < hour or self._state.get("last_checkin_on") == today:
            return
        self._state["last_checkin_on"] = today
        self._save_state()
        self.notifier.notify("Daily check-in", self.summary_text(), "low", "clipboard", "checkin")

    def summary_text(self) -> str:
        lines = []
        problems = self.problems()
        lines.append("Problems right now: " + "; ".join(p["title"] for p in problems) + "." if problems else "Everything is working.")
        cams = [c for c in self.camera_statuses() if (c.get("grabber") or {}).get("configured")]
        if cams:
            up = sum(1 for c in cams if f"camera:{c['camera_id']}" not in {p["key"] for p in problems})
            lines.append(f"Cameras sending pictures: {up} of {len(cams)}.")
        disk = self.disk_usage()
        lines.append(f"Disk {disk['percent_used']}% full, database {disk['database_mb']} MB.")
        backups = self.backups()
        lines.append(f"Last backup: {backups[-1].name}." if backups else "No backup yet.")
        lines.append(f"Running since {fmt_local(self.started_at)}.")
        return " ".join(lines)

    # -- API -------------------------------------------------------------------------
    def status(self) -> dict:
        backups = self.backups()
        return {
            "ok": not self.problems(),
            "started_at": self.started_at.isoformat(),
            "startup": self.startup,
            "problems": self.problems(),
            "internet_online": self.internet_online,
            "alert_channels": configured_channels(self.config),
            "ntfy_topic": self.config.get("ntfy_topic") or None,
            "heartbeat_configured": bool(self.config.get("heartbeat_url")),
            "config_error": self.config_error,
            "alerts_waiting_to_send": len(self.notifier.pending()),
            "last_send_error": self.notifier.last_send_error,
            "last_check_error": self.last_check_error,
            "disk": self.disk_usage(),
            "last_backup": backups[-1].name if backups else None,
            "recent_alerts": self.notifier.recent(None),
            "alert_counts": self.notifier.history.counts(),
            "alert_keep_days": HISTORY_KEEP_DAYS,
            "alert_unsave_grace_hours": UNSAVE_GRACE_HOURS,
        }

    def send_test(self) -> dict:
        return self.notifier.notify(
            "Test alert",
            "If you can read this on your phone, alerts are working.",
            "default", "bell", "test",
        )
