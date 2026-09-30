"""Live traffic counting: follows vehicles on a traffic camera's stream and
records every time one crosses a counting line.

This is the live version of scripts/count_traffic.py. The pieces are the
same (RF-DETR finds vehicles in each picture, app/tracking.py's
VehicleTracker follows them from picture to picture, LineCounter spots line
crossings), but the pictures come from app/live.py's grabber for a camera
whose type is "traffic", and every crossing is saved to the database
(line_crossings) the moment it happens.

Design
------
* One worker thread and one model for every traffic camera, separate from
  the parking detector (app/live_detection.py), so a busy street never
  slows down parking and the two never share a model between threads.
* Pictures arrive several times a second, in small bursts. The queue holds
  about two seconds' worth: if the model falls behind for longer, the oldest
  waiting picture is dropped and the tracker simply sees a bigger time step (it works in real seconds, not
  frame numbers). processing_fps in the status shows whether it keeps up.
* Lines are stored as fractions of the picture (0-1), so they stay put if
  the stream's resolution changes. They're turned into pixels per picture.
* The last analysed picture and what was found in it are kept in memory,
  so the traffic page can show the picture and the boxes that belong to
  exactly that picture (the newest camera picture may be a frame ahead).
"""

from __future__ import annotations

import queue
import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from app import detection_core
from app.tracking import CountLine, LineCounter, VehicleTracker, ground_point

# Pictures arrive from the network in bursts -- after a Wi-Fi hiccup, ten
# seconds' worth can arrive at once -- and the model takes a while to load at
# startup, so the queue holds ~30 s (5 per second) and works through the
# backlog a little late instead of dropping pictures. Only if the computer is
# genuinely too slow for longer than that is the oldest waiting one dropped.
MAX_QUEUE_SIZE = 150
CONFIDENCE = 0.25  # same as scripts/count_traffic.py (validated on real video)
RECENT_CROSSINGS_KEPT = 30
# If no picture was analysed for this long, start the tracker fresh (old
# tracks would otherwise be matched to unrelated cars after an outage).
TRACKER_RESET_GAP_SECONDS = 5.0
FPS_WINDOW_SECONDS = 10.0
# Coverage (the stretches of time the camera was really watched, see
# traffic_store): a new stretch starts after a gap this long, and the open
# stretch's end is saved at least this often.
COVERAGE_GAP_SECONDS = 5.0
COVERAGE_SAVE_SECONDS = 5.0


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _epoch(iso_value: str) -> float:
    return datetime.fromisoformat(iso_value).timestamp()


def _iso(epoch_seconds: float) -> str:
    return datetime.fromtimestamp(epoch_seconds, timezone.utc).isoformat()


def default_direction_labels(x1: float, y1: float, x2: float, y2: float) -> tuple[str, str]:
    """Plain names for a line's two directions, from how it's drawn.

    "forward" means crossing from the left side of p1->p2 to its right side
    (see app/tracking.py). In picture coordinates (y grows downward) that is
    movement along (-dy, dx)."""
    dx, dy = x2 - x1, y2 - y1
    nx, ny = -dy, dx
    if abs(nx) >= abs(ny):
        return ("Left to right", "Right to left") if nx > 0 else ("Right to left", "Left to right")
    return ("Top to bottom", "Bottom to top") if ny > 0 else ("Bottom to top", "Top to bottom")


class _CameraState:
    def __init__(self, lines_version: int) -> None:
        self.tracker = VehicleTracker()
        self.counter: Optional[LineCounter] = None
        self.lines_version = lines_version
        self.frame_size: Optional[tuple] = None
        self.last_epoch: Optional[float] = None
        self.focus: Optional[tuple] = None


class TrafficCounter:
    """One instance runs for the whole server and handles every traffic camera.

    ``db`` needs list_count_lines(camera_id) and add_line_crossings(rows).
    ``detector`` (tests) replaces the model: detector(rgb_image) -> list of
    detection_core.Detection."""

    def __init__(self, db: Any, model_size: str = "medium", detector: Optional[Callable] = None,
                 startup_fn: Optional[Callable[[], dict]] = None,
                 camera_error_fn: Optional[Callable[[str], tuple]] = None,
                 focus_fn: Optional[Callable[[str], Optional[tuple]]] = None):
        self._db = db
        # camera_id -> the zoom area (x, y, w, h fractions of the full picture)
        # the pictures were cut to, or None. Lines are stored against the full
        # picture and moved into the zoomed picture's pixels here.
        self._focus_fn = focus_fn or (lambda camera_id: None)
        self._reconfigured: set = set()
        # Why the program last started ({"reason", "detail"}, from the health
        # monitor) -- the cause of the gap before each camera's first stretch.
        self._startup_fn = startup_fn or (lambda: {"reason": "restart", "detail": ""})
        # camera_id -> (last_error, last_error_at iso) from the camera grabber.
        self._camera_error_fn = camera_error_fn or (lambda camera_id: (None, None))
        self._started_cameras: set = set()
        self._resumed: set = set()
        self._model_size = model_size
        self._detector = detector
        self._model: Any = None
        self._queue: "queue.Queue[tuple[str, bytes, str]]" = queue.Queue(maxsize=MAX_QUEUE_SIZE)
        self._worker: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._cameras: dict[str, _CameraState] = {}
        self._lines_version: dict[str, int] = {}
        self._status: dict[str, dict[str, Any]] = {}
        self._last_view: dict[str, dict[str, Any]] = {}
        self._last_jpeg: dict[str, bytes] = {}
        self._recent: dict[str, list] = {}
        self._frame_times: dict[str, list] = {}
        self._paused: set = set()
        # camera_id -> {"id", "end", "saved_end"} for the open coverage stretch
        self._coverage: dict = {}

    # -- lifecycle -------------------------------------------------------------
    def start(self) -> None:
        if self._worker is not None:
            return
        self._worker = threading.Thread(target=self._run, daemon=True, name="traffic-counter")
        self._worker.start()

    def submit_frame(self, camera_id: str, jpeg: bytes, captured_at: str) -> None:
        """on_frame callback for traffic cameras. Never blocks the grabber."""
        try:
            self._queue.put_nowait((camera_id, jpeg, captured_at))
        except queue.Full:
            try:
                self._queue.get_nowait()
            except queue.Empty:
                pass
            try:
                self._queue.put_nowait((camera_id, jpeg, captured_at))
            except queue.Full:
                pass
            with self._lock:
                entry = self._status_entry(camera_id)
                entry["frames_dropped"] += 1

    def lines_changed(self, camera_id: str) -> None:
        """Call after a line is added, moved, renamed or deleted."""
        with self._lock:
            self._lines_version[camera_id] = self._lines_version.get(camera_id, 0) + 1

    def set_paused(self, camera_id: str, paused: bool) -> None:
        """Paused cameras' pictures are ignored, including one already
        queued or being analysed when the pause happened."""
        with self._lock:
            if paused:
                self._paused.add(camera_id)
            else:
                self._paused.discard(camera_id)
                self._resumed.add(camera_id)
        if paused:
            self._close_coverage(camera_id)
            self.reset_camera(camera_id)

    def view_changed(self, camera_id: str) -> None:
        """The camera's zoom area or quality changed (it reconnects): start
        the tracker fresh, and record the short gap as a planned one."""
        with self._lock:
            self._reconfigured.add(camera_id)
        self._close_coverage(camera_id)
        self.reset_camera(camera_id)

    def reset_camera(self, camera_id: str) -> None:
        """Forget the tracker, last picture and boxes (used when paused). The
        counters in status() and the recent crossings list are kept."""
        with self._lock:
            for store in (self._cameras, self._last_view, self._last_jpeg, self._frame_times):
                store.pop(camera_id, None)

    def forget_camera(self, camera_id: str) -> None:
        with self._lock:
            self._coverage.pop(camera_id, None)
            self._paused.discard(camera_id)
            self._reconfigured.discard(camera_id)
            for store in (self._cameras, self._lines_version, self._status, self._last_view,
                          self._last_jpeg, self._recent, self._frame_times):
                store.pop(camera_id, None)

    # -- read side (for the web page and the health monitor) --------------------
    def _status_entry(self, camera_id: str) -> dict[str, Any]:
        return self._status.setdefault(
            camera_id,
            {"frames_processed": 0, "frames_dropped": 0, "last_frame_at": None,
             "last_error": None, "last_error_at": None, "model_loading": False},
        )

    def status(self, camera_id: str) -> dict[str, Any]:
        with self._lock:
            entry = dict(self._status_entry(camera_id))
            times = self._frame_times.get(camera_id) or []
        if len(times) >= 2 and times[-1] > times[0]:
            entry["processing_fps"] = round((len(times) - 1) / (times[-1] - times[0]), 2)
        else:
            entry["processing_fps"] = None
        return entry

    def view(self, camera_id: str) -> dict[str, Any]:
        """What the traffic page draws: the vehicles found in the last analysed
        picture, and the most recent crossings."""
        with self._lock:
            view = dict(self._last_view.get(camera_id) or {})
            view["recent_crossings"] = list(reversed(self._recent.get(camera_id, [])))
        view["status"] = self.status(camera_id)
        return view

    def last_jpeg(self, camera_id: str) -> Optional[bytes]:
        with self._lock:
            return self._last_jpeg.get(camera_id)

    # -- worker --------------------------------------------------------------
    def _detect(self, rgb_image: Any) -> list:
        if self._detector is not None:
            return self._detector(rgb_image)
        if self._model is None:
            from rfdetr import RFDETRLarge, RFDETRMedium, RFDETRNano, RFDETRSmall

            sizes = {"nano": RFDETRNano, "small": RFDETRSmall, "medium": RFDETRMedium, "large": RFDETRLarge}
            self._model = sizes[self._model_size]()
        found = detection_core.run_detector(self._model, rgb_image, CONFIDENCE)
        return detection_core.remove_duplicate_detections(found)

    def _run(self) -> None:
        while True:
            camera_id, jpeg, captured_at = self._queue.get()
            try:
                self.process_frame(camera_id, jpeg, captured_at)
            except Exception as exc:  # never let one bad picture kill the worker
                with self._lock:
                    entry = self._status_entry(camera_id)
                    entry["last_error"] = str(exc)
                    entry["last_error_at"] = utc_now()

    # -- coverage ---------------------------------------------------------------
    def _note_coverage(self, camera_id: str, now: float) -> None:
        """Records that this camera was watched up to `now`. Called from the
        worker thread only; database writes stay small (a few per minute)."""
        if not hasattr(self._db, "open_traffic_coverage"):
            return
        with self._lock:
            open_stretch = self._coverage.get(camera_id)
        if open_stretch is not None and open_stretch["end"] - COVERAGE_GAP_SECONDS <= now <= open_stretch["end"]:
            return  # a picture slightly out of order: already covered
        if open_stretch is None or now - open_stretch["end"] > COVERAGE_GAP_SECONDS or now < open_stretch["end"]:
            if open_stretch is not None:
                self._save_coverage_end(open_stretch)
            reason, detail = self._gap_cause(camera_id, open_stretch["end"] if open_stretch else None, now)
            coverage_id = self._db.open_traffic_coverage(camera_id, _iso(now), reason, detail)
            open_stretch = {"id": coverage_id, "end": now, "saved_end": now}
            with self._lock:
                self._coverage[camera_id] = open_stretch
            return
        open_stretch["end"] = now
        if now - open_stretch["saved_end"] >= COVERAGE_SAVE_SECONDS:
            self._save_coverage_end(open_stretch)

    def _gap_cause(self, camera_id: str, gap_start: Optional[float], now: float) -> tuple:
        """Why the camera wasn't being watched just before `now`."""
        with self._lock:
            first = camera_id not in self._started_cameras
            self._started_cameras.add(camera_id)
            resumed = camera_id in self._resumed
            self._resumed.discard(camera_id)
        if first:
            startup = self._startup_fn() or {}
            return startup.get("reason") or "restart", startup.get("detail") or ""
        if resumed:
            return "paused", ""
        with self._lock:
            reconfigured = camera_id in self._reconfigured
            self._reconfigured.discard(camera_id)
        if reconfigured:
            return "reconfigured", ""
        error, error_at = self._camera_error_fn(camera_id)
        if error and error_at and gap_start is not None:
            try:
                at = datetime.fromisoformat(error_at).timestamp()
            except ValueError:
                at = None
            if at is not None and gap_start - 1 <= at <= now:
                return "camera", str(error)[:300]
        return "no_pictures", ""

    def _save_coverage_end(self, open_stretch: dict) -> None:
        if open_stretch["end"] > open_stretch["saved_end"]:
            self._db.extend_traffic_coverage(open_stretch["id"], _iso(open_stretch["end"]))
            open_stretch["saved_end"] = open_stretch["end"]

    def _close_coverage(self, camera_id: str) -> None:
        with self._lock:
            open_stretch = self._coverage.pop(camera_id, None)
        if open_stretch is not None and hasattr(self._db, "extend_traffic_coverage"):
            self._save_coverage_end(open_stretch)

    def _lines_for(self, camera_id: str, frame_size: tuple, focus: Optional[tuple] = None) -> list[CountLine]:
        width, height = frame_size
        fx, fy, fw, fh = focus or (0.0, 0.0, 1.0, 1.0)

        def to_pixels(x: float, y: float) -> tuple:
            return ((x - fx) / fw * width, (y - fy) / fh * height)

        lines = []
        for row in self._db.list_count_lines(camera_id):
            lines.append(
                CountLine(
                    line_id=str(row["id"]),
                    name=row["name"],
                    p1=to_pixels(row["x1"], row["y1"]),
                    p2=to_pixels(row["x2"], row["y2"]),
                    forward_label=row["forward_label"],
                    reverse_label=row["reverse_label"],
                )
            )
        return lines

    def process_frame(self, camera_id: str, jpeg: bytes, captured_at: str) -> list:
        """Analyses one picture. Public so tests can drive it directly.
        Returns the crossings saved for this picture."""
        if self._model is None and self._detector is None:
            with self._lock:
                self._status_entry(camera_id)["model_loading"] = True
        with self._lock:
            if camera_id in self._paused:
                return []
        rgb_image = detection_core.rgb_image_from_bytes(jpeg)
        detections = self._detect(rgb_image)
        now = _epoch(captured_at)
        frame_size = rgb_image.size

        with self._lock:
            self._status_entry(camera_id)["model_loading"] = False
            if camera_id in self._paused:
                return []  # paused while this picture was being analysed
            version = self._lines_version.get(camera_id, 0)
            state = self._cameras.get(camera_id)
            if state is None:
                state = self._cameras[camera_id] = _CameraState(-1)
            focus = self._focus_fn(camera_id)
            if focus != state.focus:
                # A different zoom area: same cars, different pixels.
                state.tracker = VehicleTracker()
                state.counter = None
                state.focus = focus
            gap = None if state.last_epoch is None else now - state.last_epoch
            if gap is not None and (gap < 0 or gap > TRACKER_RESET_GAP_SECONDS):
                # Outage (or a clock jump): start fresh rather than link old
                # tracks to whatever car happens to be there now.
                state.tracker = VehicleTracker()
                state.counter = None
            if state.counter is None or state.lines_version != version or state.frame_size != frame_size:
                lines = self._lines_for(camera_id, frame_size, state.focus)
                old = state.counter
                state.counter = LineCounter(lines)
                if old is not None:
                    # Keep "already counted" memory so a car sitting on a line
                    # isn't counted again just because a line was renamed.
                    state.counter._counted = {k for k in old._counted if any(l.line_id == k[1] for l in lines)}
                    state.counter._last_point = dict(old._last_point)
                state.lines_version = version
                state.frame_size = frame_size
            state.last_epoch = now

            seen = state.tracker.update(detections, now)
            crossings = state.counter.update(seen, now)
            state.counter.forget_missing(t.track_id for t in state.tracker.tracks)

            counted_ids = {key[0] for key in state.counter._counted}
            tracks = [
                {
                    "id": t.track_id,
                    "box": [round(v, 1) for v in t.box],
                    "class": t.vehicle_class,
                    "confidence": round(t.confidence, 3),
                    "confirmed": t.confirmed,
                    "counted": t.track_id in counted_ids,
                    "seen_now": t.last_seen == now,
                    "trail": [[round(p[0], 1), round(p[1], 1)] for _, p in t.path[-12:]],
                }
                for t in state.tracker.tracks
            ]

        rows = [
            {
                "line_id": int(c.line_id),
                "camera_id": camera_id,
                "crossed_at": _iso(c.time),
                "direction": c.direction,
                "vehicle_class": c.vehicle_class,
                "track_id": c.track_id,
            }
            for c in crossings
        ]
        if rows:
            self._db.add_line_crossings(rows)
        self._note_coverage(camera_id, now)

        with self._lock:
            entry = self._status_entry(camera_id)
            entry["frames_processed"] += 1
            entry["last_frame_at"] = captured_at
            entry["last_error"] = None
            times = self._frame_times.setdefault(camera_id, [])
            times.append(time.monotonic())
            cutoff = times[-1] - FPS_WINDOW_SECONDS
            while times and times[0] < cutoff:
                times.pop(0)
            self._last_jpeg[camera_id] = jpeg
            self._last_view[camera_id] = {
                "captured_at": captured_at,
                "frame_size": list(frame_size),
                # The zoom area this picture was cut to (fractions of the full picture).
                "focus": None if state.focus is None else dict(zip(("x", "y", "w", "h"), state.focus)),
                "detections": [
                    {"box": [round(v, 1) for v in d.box], "class": d.class_name, "confidence": round(d.confidence, 3)}
                    for d in detections
                ],
                "tracks": tracks,
                "frame_seq": entry["frames_processed"],
            }
            recent = self._recent.setdefault(camera_id, [])
            for c in crossings:
                recent.append(
                    {
                        "line_id": int(c.line_id),
                        "line_name": c.line_name,
                        "direction": c.direction,
                        "direction_label": c.direction_label,
                        "vehicle_class": c.vehicle_class,
                        "track_id": c.track_id,
                        "crossed_at": _iso(c.time),
                        "point": [round(v, 1) for v in ground_point(
                            next((t["box"] for t in tracks if t["id"] == c.track_id), (0, 0, 0, 0))
                        )],
                    }
                )
            del recent[:-RECENT_CROSSINGS_KEPT]
        return rows
