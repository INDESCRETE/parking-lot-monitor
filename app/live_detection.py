"""Continuous (streaming) vehicle detection for the live camera feed.

Turns snapshots from app/live.py's frame grabbers into occupied/vacant space
state, without ever writing a database row per frame. Live occupancy history
lives entirely in space_state_intervals -- the exact same table the batch
pipeline (scripts/detect_occupancy.py) writes to -- so the rest of the app
(current-status display on the main page, dwell/turnover analytics, exported
reports) sees live data automatically, with no separate code path to build.

Design
------
* The model is loaded ONCE, lazily, in a background worker thread -- unlike
  the batch script, which is a short-lived subprocess that pays that cost
  every run. See scripts/benchmark_detection_speed.py for why this matters:
  loading takes real time, but a single detection is fast (~0.1s measured on
  Rob's Mac), so one worker easily keeps up with every camera's frames even
  sharing one queue.
* Frames are handed off through a small bounded queue so a slow model run
  never blocks a camera grabber thread. If frames somehow pile up faster
  than they can be processed, the oldest queued one is dropped in favor of
  the newest rather than falling further and further behind.
* Per space, a small state machine tracks a "confirmed" state (what's
  written to the database as the live is_current interval) and a
  "candidate" state (a differing raw reading that hasn't persisted long
  enough yet to be believed). A candidate only becomes confirmed -- closing
  the open interval and starting a new one -- once it has been read
  consistently for min_occupied_seconds. This is the same debounce idea as
  the batch pipeline's noise filter (app/intervals.py), just applied live
  instead of after the fact, and applied symmetrically: an occupied->vacant
  candidate is debounced exactly like a vacant->occupied one, rather than
  only ever folding away short occupied runs after they've already
  happened.
* A big gap since the last frame actually processed for a space (server was
  down, camera was offline, or this is the very first live frame picking up
  an old interval left by the batch pipeline from uploaded footage days
  ago) is deliberately NOT bridged: the open interval is closed at its own
  last known timestamp and a fresh one starts at the new frame's timestamp,
  rather than stretching a duration across time nothing was actually
  observed.

Known gap (not handled here, flagged rather than silently ignored): running
the batch "Run Detection" pipeline against a camera that also has the live
feed running will wipe out that camera's live-collected intervals for any
space it touches -- recompute_space_intervals() always fully deletes and
rebuilds a space's interval history from occupancy_observations alone, which
the live path never writes to. Fine for a camera that's purely live, or
purely batch, but the two shouldn't both be driving the same space's history
at once yet.
"""

from __future__ import annotations

import queue
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

from app import detection_core
from app.intervals import DEFAULT_MIN_OCCUPIED_SECONDS

MAX_QUEUE_SIZE = 4  # a handful of frames' grace; more than this means something's stuck
CONFIDENCE = 0.25
ANCHOR_Y_RATIO = 0.9
FALLBACK_OVERLAP_THRESHOLD = 0.7

# If we haven't successfully processed a frame for a space in longer than
# this, don't stretch its open interval across the gap (see module docstring).
# 120s is generous relative to the 3s default capture interval and the
# grabber's own error backoff (which maxes out at 60s), so ordinary hiccups
# don't cause a false reset, while a real outage or a stale batch-derived
# interval does.
GAP_RESET_SECONDS = 120.0


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value)


@dataclass
class _SpaceState:
    camera_id: str
    space_id: int
    confirmed_occupied: Optional[bool] = None
    confirmed_since: Optional[str] = None
    last_seen_at: Optional[str] = None
    candidate_occupied: Optional[bool] = None
    candidate_since: Optional[str] = None


class LiveDetector:
    """One instance runs for the whole server and handles every live camera."""

    def __init__(self, db: Any, model_size: str = "medium"):
        self._db = db
        self._model_size = model_size
        self._frame_queue: "queue.Queue[tuple[str, bytes, str]]" = queue.Queue(maxsize=MAX_QUEUE_SIZE)
        self._model: Any = None
        self._model_lock = threading.Lock()
        self._states: dict[int, _SpaceState] = {}
        self._states_lock = threading.Lock()
        self._worker: Optional[threading.Thread] = None
        self._status_lock = threading.Lock()
        self._status: dict[str, dict[str, Any]] = {}
        # What the detector saw on each camera's most recent frame (boxes,
        # confidence, which space each box was matched to). Memory only, for
        # troubleshooting via GET /api/cameras/<id>/live-debug.
        self._last_debug: dict[str, dict[str, Any]] = {}

    def start(self) -> None:
        """Starts the background worker thread. Safe to call even if no live
        camera is configured yet -- the thread just idles on an empty queue,
        and the model isn't loaded until the first real frame arrives."""
        if self._worker is not None:
            return
        self._worker = threading.Thread(target=self._run, daemon=True, name="live-detector")
        self._worker.start()

    def submit_frame(self, camera_id: str, jpeg: bytes, captured_at: str) -> None:
        """The on_frame callback app/live.py's grabbers call after every
        successful snapshot. Never blocks the grabber thread."""
        try:
            self._frame_queue.put_nowait((camera_id, jpeg, captured_at))
        except queue.Full:
            try:
                self._frame_queue.get_nowait()  # drop the oldest queued frame
            except queue.Empty:
                pass
            try:
                self._frame_queue.put_nowait((camera_id, jpeg, captured_at))
            except queue.Full:
                pass  # extremely unlikely race; skip this one frame

    def forget_camera(self, camera_id: str) -> None:
        """Called when a camera is deleted, so its in-memory space state
        can't resurrect an interval if a new camera later reuses spaces."""
        with self._states_lock:
            stale = [space_id for space_id, state in self._states.items() if state.camera_id == camera_id]
            for space_id in stale:
                del self._states[space_id]
        with self._status_lock:
            self._status.pop(camera_id, None)

    def status(self, camera_id: str) -> dict[str, Any]:
        with self._status_lock:
            entry = self._status.get(camera_id)
            return dict(entry) if entry else {"frames_processed": 0, "last_frame_at": None, "last_error": None}

    def last_debug(self, camera_id: str) -> Optional[dict[str, Any]]:
        with self._status_lock:
            entry = self._last_debug.get(camera_id)
            return dict(entry) if entry else None

    def _update_status(self, camera_id: str, **fields: Any) -> None:
        with self._status_lock:
            entry = self._status.setdefault(
                camera_id, {"frames_processed": 0, "last_frame_at": None, "last_error": None}
            )
            entry.update(fields)

    def _get_model(self) -> Any:
        with self._model_lock:
            if self._model is None:
                # Imported here, not at module load time: this import alone
                # takes real time (loading torch + the checkpoint), and
                # would otherwise slow down every server start even for
                # installs with no live camera configured at all.
                from rfdetr import RFDETRLarge, RFDETRMedium, RFDETRNano, RFDETRSmall

                sizes = {
                    "nano": RFDETRNano,
                    "small": RFDETRSmall,
                    "medium": RFDETRMedium,
                    "large": RFDETRLarge,
                }
                self._model = sizes[self._model_size]()
            return self._model

    def _run(self) -> None:
        while True:
            camera_id, jpeg, captured_at = self._frame_queue.get()
            try:
                self._process_frame(camera_id, jpeg, captured_at)
            except Exception as exc:  # a bad frame or a bug must not kill the worker thread
                self._update_status(camera_id, last_error=str(exc), last_error_at=utc_now())

    def _process_frame(self, camera_id: str, jpeg: bytes, captured_at: str) -> None:
        camera = self._db.get_camera(camera_id)
        if camera is None:
            return  # camera was deleted between the grab and now
        spaces = self._db.list_spaces(camera_id)
        if not spaces:
            self._update_status(camera_id, last_frame_at=captured_at)
            return  # nothing marked yet -- nothing to track

        model = self._get_model()
        rgb_image = detection_core.rgb_image_from_bytes(jpeg)
        # Scale each space's polygon to this frame's actual size before
        # matching -- the live camera's snapshots can be (and for Rob's
        # reolink_live camera, were) a different resolution than whatever
        # photo was on screen when the space was marked (e.g. a small
        # screenshot used to mark spaces vs. the camera's full-resolution
        # live photos). A no-op for a space with no recorded reference size
        # or one that already matches this frame -- see scale_polygon's own
        # docstring in app/detection_core.py.
        space_dicts = [
            {
                "id": space["id"],
                "polygon": detection_core.scale_polygon(
                    space["polygon"],
                    (space.get("reference_width"), space.get("reference_height")),
                    rgb_image.size,
                ),
            }
            for space in spaces
        ]
        # Crops to the marked spaces and drops duplicate boxes -- see
        # detect_vehicles in app/detection_core.py.
        detections = detection_core.detect_vehicles(
            model, rgb_image, CONFIDENCE, [space["polygon"] for space in space_dicts]
        )
        # assign_detections_to_spaces expects (detection_id, Detection) pairs
        # to hand back which one won a space, for storing in occupancy_
        # observations. Live mode never stores detections, so a throwaway
        # per-frame index stands in for a real detection id -- it's only
        # used to look the winning detection back up within this one call.
        indexed_detections = list(enumerate(detections))
        occupied_by_space_id = detection_core.assign_detections_to_spaces(
            space_dicts, indexed_detections, ANCHOR_Y_RATIO, FALLBACK_OVERLAP_THRESHOLD
        )

        winner_space_by_index = {
            int(candidate["detection_id"]): space_id for space_id, candidate in occupied_by_space_id.items()
        }
        best_overlap_by_index = {}
        for index, detection in indexed_detections:
            scores = [
                (detection_core.occupancy_overlap_score(space["polygon"], detection.box), space["id"])
                for space in space_dicts
            ]
            best_overlap_by_index[index] = max(scores) if scores else (0.0, None)
        label_by_id = {space["id"]: space.get("label") for space in spaces}
        debug = {
            "captured_at": captured_at,
            "frame_size": list(rgb_image.size),
            "crop_region": detection_core.crop_region_for_polygons(
                [space["polygon"] for space in space_dicts], rgb_image.size
            ),
            "confidence_threshold": CONFIDENCE,
            "detections": [
                {
                    "box": [round(v, 1) for v in detection.box],
                    "confidence": round(detection.confidence, 3),
                    "class_name": detection.class_name,
                    "best_overlap": round(best_overlap_by_index[index][0], 3),
                    "best_overlap_space": label_by_id.get(best_overlap_by_index[index][1]),
                    "assigned_space": label_by_id.get(winner_space_by_index.get(index)),
                }
                for index, detection in indexed_detections
            ],
            "occupied_spaces": sorted(label_by_id[sid] for sid in occupied_by_space_id),
            "space_polygons": {space["label"]: sd["polygon"] for space, sd in zip(spaces, space_dicts)},
        }
        with self._status_lock:
            self._last_debug[camera_id] = debug

        min_occupied_seconds = camera.get("min_occupied_seconds")
        if min_occupied_seconds is None:
            min_occupied_seconds = DEFAULT_MIN_OCCUPIED_SECONDS

        with self._db.connect() as conn:
            for space in spaces:
                raw_occupied = space["id"] in occupied_by_space_id
                self._apply_reading(conn, camera_id, space["id"], raw_occupied, captured_at, min_occupied_seconds)

        with self._status_lock:
            frames_processed = self._status.get(camera_id, {}).get("frames_processed", 0) + 1
        self._update_status(camera_id, frames_processed=frames_processed, last_frame_at=captured_at, last_error=None)

    def _apply_reading(
        self,
        conn: sqlite3.Connection,
        camera_id: str,
        space_id: int,
        raw_occupied: bool,
        captured_at: str,
        min_occupied_seconds: float,
    ) -> None:
        with self._states_lock:
            state = self._states.get(space_id)
        if state is None:
            state = self._load_state(conn, camera_id, space_id)
            with self._states_lock:
                self._states[space_id] = state

        needs_fresh_start = (
            state.confirmed_since is None
            or state.last_seen_at is None
            or (_parse_iso(captured_at) - _parse_iso(state.last_seen_at)).total_seconds() > GAP_RESET_SECONDS
        )
        if needs_fresh_start:
            self._start_fresh_interval(conn, space_id, state, raw_occupied, captured_at)
            return

        state.last_seen_at = captured_at

        if raw_occupied == state.confirmed_occupied:
            # Matches the confirmed state: no real change. Any pending
            # candidate evaporates (that differing reading didn't hold up),
            # and the open interval's end time just moves forward.
            state.candidate_occupied = None
            state.candidate_since = None
            self._extend_current_interval(conn, space_id, captured_at)
            return

        if state.candidate_occupied != raw_occupied:
            # A new, different reading -- start (or restart) the debounce clock.
            state.candidate_occupied = raw_occupied
            state.candidate_since = captured_at
            return

        # The candidate has now been seen more than once. Confirm it once it
        # has persisted for min_occupied_seconds.
        elapsed = (_parse_iso(captured_at) - _parse_iso(state.candidate_since)).total_seconds()
        if elapsed < min_occupied_seconds:
            return
        self._confirm_change(conn, space_id, state, captured_at)

    def _load_state(self, conn: sqlite3.Connection, camera_id: str, space_id: int) -> _SpaceState:
        """First frame seen for this space since the server started. Picks up
        any existing open (is_current) interval -- whether left by the live
        detector before a restart or by the batch pipeline from uploaded
        footage -- so a restart doesn't fabricate a new interval and lose
        the real start time. _apply_reading's gap check decides separately
        whether that picked-up interval is recent enough to keep extending."""
        row = conn.execute(
            """
            SELECT occupied, start_captured_at, end_captured_at
            FROM space_state_intervals
            WHERE parking_space_id = ? AND is_current = 1
            """,
            (space_id,),
        ).fetchone()
        if row is None:
            return _SpaceState(camera_id=camera_id, space_id=space_id)
        return _SpaceState(
            camera_id=camera_id,
            space_id=space_id,
            confirmed_occupied=bool(row["occupied"]),
            confirmed_since=row["start_captured_at"],
            last_seen_at=row["end_captured_at"],
        )

    def _start_fresh_interval(
        self,
        conn: sqlite3.Connection,
        space_id: int,
        state: _SpaceState,
        raw_occupied: bool,
        captured_at: str,
    ) -> None:
        if state.confirmed_since is not None:
            # Close out whatever was open. Deliberately NOT stretched to
            # captured_at -- the gap in between wasn't actually observed.
            conn.execute(
                "UPDATE space_state_intervals SET is_current = 0 WHERE parking_space_id = ? AND is_current = 1",
                (space_id,),
            )
        now = utc_now()
        conn.execute(
            """
            INSERT INTO space_state_intervals
                (parking_space_id, occupied, start_image_id, start_captured_at,
                 end_image_id, end_captured_at, duration_seconds, is_current, created_at)
            VALUES (?, ?, NULL, ?, NULL, ?, 0, 1, ?)
            """,
            (space_id, int(raw_occupied), captured_at, captured_at, now),
        )
        state.confirmed_occupied = raw_occupied
        state.confirmed_since = captured_at
        state.last_seen_at = captured_at
        state.candidate_occupied = None
        state.candidate_since = None

    def _extend_current_interval(self, conn: sqlite3.Connection, space_id: int, captured_at: str) -> None:
        row = conn.execute(
            "SELECT start_captured_at FROM space_state_intervals WHERE parking_space_id = ? AND is_current = 1",
            (space_id,),
        ).fetchone()
        if row is None:
            return  # shouldn't happen, but a missing row here shouldn't crash the worker
        duration = max(0.0, (_parse_iso(captured_at) - _parse_iso(row["start_captured_at"])).total_seconds())
        conn.execute(
            "UPDATE space_state_intervals SET end_captured_at = ?, duration_seconds = ? "
            "WHERE parking_space_id = ? AND is_current = 1",
            (captured_at, duration, space_id),
        )

    def _confirm_change(
        self, conn: sqlite3.Connection, space_id: int, state: _SpaceState, captured_at: str
    ) -> None:
        change_at = state.candidate_since
        assert change_at is not None and state.confirmed_since is not None
        closed_duration = max(0.0, (_parse_iso(change_at) - _parse_iso(state.confirmed_since)).total_seconds())
        conn.execute(
            "UPDATE space_state_intervals SET is_current = 0, end_captured_at = ?, duration_seconds = ? "
            "WHERE parking_space_id = ? AND is_current = 1",
            (change_at, closed_duration, space_id),
        )
        new_duration = max(0.0, (_parse_iso(captured_at) - _parse_iso(change_at)).total_seconds())
        now = utc_now()
        conn.execute(
            """
            INSERT INTO space_state_intervals
                (parking_space_id, occupied, start_image_id, start_captured_at,
                 end_image_id, end_captured_at, duration_seconds, is_current, created_at)
            VALUES (?, ?, NULL, ?, NULL, ?, ?, 1, ?)
            """,
            (space_id, int(state.candidate_occupied), change_at, captured_at, new_duration, now),
        )
        state.confirmed_occupied = state.candidate_occupied
        state.confirmed_since = change_at
        state.last_seen_at = captured_at
        state.candidate_occupied = None
        state.candidate_since = None
