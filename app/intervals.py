"""Rebuilding a parking space's occupied/vacant timeline from its observations.

Lives in its own small module (no AI model, no torch) so that both the detection
script and the web server can use it. The server needs it when an image is
deleted: the timeline for the affected spaces has to be recalculated from the
observations that remain, without loading the detector.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


# A single frame (or a couple of frames, at fast sampling rates) reading a
# space as occupied, flanked by vacant on both sides, is almost always a
# vehicle briefly passing through the polygon rather than actually parking --
# not a real arrival. recompute_space_intervals() below folds any occupied
# run shorter than this back into vacant time by default. Tune per-camera
# with detect_occupancy.py's --min-occupied-seconds if a lot's real traffic
# pattern needs a different cutoff.
DEFAULT_MIN_OCCUPIED_SECONDS = 10.0


def _run_duration_seconds(start_row: sqlite3.Row, end_row: sqlite3.Row) -> float:
    return max(
        0.0,
        (
            datetime.fromisoformat(end_row["captured_at"])
            - datetime.fromisoformat(start_row["captured_at"])
        ).total_seconds(),
    )


def recompute_space_intervals(
    conn: sqlite3.Connection,
    space_id: int,
    min_occupied_seconds: float = DEFAULT_MIN_OCCUPIED_SECONDS,
) -> None:
    """Rebuild the occupied/vacant timeline for one space from scratch.

    Walks every occupancy_observation for the space in chronological (captured_at)
    order and collapses consecutive same-state observations into intervals. This
    is a full rebuild rather than an incremental append so that reruns (which
    redetect and overwrite observations for every image, not just new ones) stay
    correct without risking duplicate or stale intervals.

    An occupied run shorter than `min_occupied_seconds` is treated as noise --
    almost always a vehicle passing through the space rather than parking in
    it -- and merged back into its surrounding vacant time instead of being
    recorded as its own session. The still-open final run is never treated as
    noise even if it's currently short, since it may simply have just started
    and hasn't had a chance to prove itself real or noise yet; it gets
    reconsidered the next time detection runs.
    """
    conn.execute("DELETE FROM space_state_intervals WHERE parking_space_id = ?", (space_id,))
    rows = conn.execute(
        """
        SELECT oo.occupied, oo.image_id, im.captured_at
        FROM occupancy_observations oo
        JOIN images im ON im.id = oo.image_id
        WHERE oo.parking_space_id = ?
        ORDER BY im.captured_at, im.id
        """,
        (space_id,),
    ).fetchall()
    if not rows:
        return

    raw_runs: list[tuple[int, sqlite3.Row, sqlite3.Row]] = []
    run_state = rows[0]["occupied"]
    run_start = rows[0]
    run_last = rows[0]
    for row in rows[1:]:
        if row["occupied"] == run_state:
            run_last = row
            continue
        raw_runs.append((run_state, run_start, run_last))
        run_state = row["occupied"]
        run_start = row
        run_last = row
    raw_runs.append((run_state, run_start, run_last))

    # Fold short occupied blips into vacant time. This can leave two
    # newly-adjacent runs of the same state (e.g. vacant, [noise blip removed],
    # vacant) that need merging into one, so this pass builds `merged`
    # incrementally rather than filtering `raw_runs` in place.
    merged: list[tuple[int, sqlite3.Row, sqlite3.Row]] = []
    last_raw_index = len(raw_runs) - 1
    for index, (occupied, start_row, end_row) in enumerate(raw_runs):
        is_noise = (
            occupied
            and index != last_raw_index
            and _run_duration_seconds(start_row, end_row) < min_occupied_seconds
        )
        effective_state = False if is_noise else occupied
        if merged and merged[-1][0] == effective_state:
            prev_state, prev_start, _prev_end = merged[-1]
            merged[-1] = (prev_state, prev_start, end_row)
        else:
            merged.append((effective_state, start_row, end_row))

    now = utc_now()
    last_index = len(merged) - 1
    for index, (occupied, start_row, end_row) in enumerate(merged):
        duration = _run_duration_seconds(start_row, end_row)
        conn.execute(
            """
            INSERT INTO space_state_intervals
                (parking_space_id, occupied, start_image_id, start_captured_at,
                 end_image_id, end_captured_at, duration_seconds, is_current, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                space_id,
                occupied,
                start_row["image_id"],
                start_row["captured_at"],
                end_row["image_id"],
                end_row["captured_at"],
                duration,
                1 if index == last_index else 0,
                now,
            ),
        )
