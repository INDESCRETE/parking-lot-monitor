"""Reporting/analytics aggregation over space_state_intervals for one lot.

Everything here is derived from space_state_intervals (built by
scripts/detect_occupancy.py's recompute_space_intervals), which already
models each space's occupied/vacant history as a set of non-overlapping,
contiguous time intervals. That means every metric below is just a
different way of slicing the same interval history — no new raw-event
table is needed.

Two different "what counts as an event" rules are used, deliberately:

- Occupancy rate and peak/off-peak hours need continuous-time coverage, so
  intervals are clipped to the requested [start, end) window and then
  chopped at every hour boundary (an interval spanning many hours must be
  split so each slice belongs to exactly one calendar day and one
  hour-of-day bucket).
- Turnover and average dwell time count discrete "occupancy sessions" (one
  parking event = one occupied interval). A session is attributed to
  whichever window its *start* falls in, so a multi-day session is counted
  once, not once per day it happens to touch.
"""

from __future__ import annotations

import sqlite3
from collections import defaultdict
from datetime import date, datetime, timedelta
from typing import Any


def _parse(ts: str) -> datetime:
    return datetime.fromisoformat(ts)


def _date_range(start: datetime, end: datetime) -> list[str]:
    """Every calendar date touched by the half-open range [start, end)."""
    if end <= start:
        return [start.date().isoformat()]
    days = []
    cursor = start.date()
    last_day = (end - timedelta(microseconds=1)).date()
    while cursor <= last_day:
        days.append(cursor.isoformat())
        cursor += timedelta(days=1)
    return days


def _hour_slices(start: datetime, end: datetime):
    """Split [start, end) into chunks that each lie within a single hour,
    so every chunk belongs to exactly one calendar day and one hour-of-day."""
    cursor = start
    while cursor < end:
        hour_start = cursor.replace(minute=0, second=0, microsecond=0)
        next_hour = hour_start + timedelta(hours=1)
        slice_end = min(end, next_hour)
        yield cursor, slice_end
        cursor = slice_end


def _lot_spaces(
    conn: sqlite3.Connection, lot_id: int, camera_id: str | None = None
) -> list[dict[str, Any]]:
    """Every space belonging to this lot, or -- when camera_id is given --
    only the spaces on that one camera within the lot. A lot can be covered
    by several camera angles that all count toward the same lot's numbers
    by default; camera_id narrows a report down to just one of them."""
    query = """
        SELECT parking_spaces.id AS space_id, parking_spaces.label AS label
        FROM parking_spaces
        JOIN cameras ON cameras.id = parking_spaces.camera_id
        WHERE cameras.lot_id = ?
    """
    params: list[Any] = [lot_id]
    if camera_id is not None:
        query += " AND cameras.id = ?"
        params.append(camera_id)
    query += " ORDER BY parking_spaces.label"
    rows = conn.execute(query, params).fetchall()
    return [{"space_id": row["space_id"], "label": row["label"]} for row in rows]


def _clipped_intervals(
    conn: sqlite3.Connection, space_ids: list[int], start: datetime, end: datetime
) -> list[dict[str, Any]]:
    """Every space_state_intervals row for these spaces that overlaps
    [start, end), clipped to that window."""
    if not space_ids:
        return []
    placeholders = ",".join("?" for _ in space_ids)
    rows = conn.execute(
        f"""
        SELECT parking_space_id, occupied, start_captured_at, end_captured_at
        FROM space_state_intervals
        WHERE parking_space_id IN ({placeholders})
          AND start_captured_at < ?
          AND end_captured_at > ?
        ORDER BY start_captured_at
        """,
        (*space_ids, end.isoformat(), start.isoformat()),
    ).fetchall()
    clipped = []
    for row in rows:
        clip_start = max(_parse(row["start_captured_at"]), start)
        clip_end = min(_parse(row["end_captured_at"]), end)
        if clip_end <= clip_start:
            continue
        clipped.append(
            {
                "space_id": row["parking_space_id"],
                "occupied": bool(row["occupied"]),
                "start": clip_start,
                "end": clip_end,
            }
        )
    return clipped


def _occupied_sessions(
    conn: sqlite3.Connection, space_ids: list[int], start: datetime, end: datetime
) -> list[dict[str, Any]]:
    """Occupied intervals whose session started within [start, end) — one
    row per parking event (arrival), used by both turnover and dwell time
    so the two metrics always agree on which sessions count."""
    if not space_ids:
        return []
    placeholders = ",".join("?" for _ in space_ids)
    rows = conn.execute(
        f"""
        SELECT parking_space_id, start_captured_at, end_captured_at
        FROM space_state_intervals
        WHERE parking_space_id IN ({placeholders})
          AND occupied = 1
          AND start_captured_at >= ? AND start_captured_at < ?
        ORDER BY start_captured_at
        """,
        (*space_ids, start.isoformat(), end.isoformat()),
    ).fetchall()
    return [
        {
            "space_id": row["parking_space_id"],
            "start": _parse(row["start_captured_at"]),
            "end": _parse(row["end_captured_at"]),
        }
        for row in rows
    ]


def _occupancy_by_day_and_hour(intervals: list[dict[str, Any]]):
    """One hour-slicing pass producing both day-level and hour-of-day-level
    occupied/total second totals, so the two views can never disagree."""
    occupied_by_day: dict[str, float] = defaultdict(float)
    total_by_day: dict[str, float] = defaultdict(float)
    occupied_by_hour: dict[int, float] = defaultdict(float)
    total_by_hour: dict[int, float] = defaultdict(float)
    for interval in intervals:
        for slice_start, slice_end in _hour_slices(interval["start"], interval["end"]):
            seconds = (slice_end - slice_start).total_seconds()
            day = slice_start.date().isoformat()
            hour = slice_start.hour
            total_by_day[day] += seconds
            total_by_hour[hour] += seconds
            if interval["occupied"]:
                occupied_by_day[day] += seconds
                occupied_by_hour[hour] += seconds
    return occupied_by_day, total_by_day, occupied_by_hour, total_by_hour


def compute_lot_report(
    conn: sqlite3.Connection,
    lot_id: int,
    start: datetime,
    end: datetime,
    camera_id: str | None = None,
) -> dict[str, Any]:
    """The full metric bundle for one lot over [start, end): occupancy rate
    by day, average dwell time by space, turnover by day, and a peak/
    off-peak hour-of-day breakdown, plus a rolled-up summary. This is the
    one entry point the dashboard and the report export both call, so they
    can never show different numbers for the same question.

    By default this combines every camera assigned to the lot (the normal
    case: several camera angles covering one physical lot). Passing
    camera_id narrows everything below to just that one camera's spaces,
    for telling cameras apart within a lot rather than a full lot rollup.
    """
    spaces = _lot_spaces(conn, lot_id, camera_id)
    space_ids = [s["space_id"] for s in spaces]

    clipped = _clipped_intervals(conn, space_ids, start, end)
    occupied_by_day, total_by_day, occupied_by_hour, total_by_hour = _occupancy_by_day_and_hour(
        clipped
    )
    occupancy_by_day = [
        {
            "date": day,
            "occupied_seconds": occupied_by_day.get(day, 0.0),
            "total_seconds": total_by_day[day],
            "rate": (occupied_by_day.get(day, 0.0) / total_by_day[day]) if total_by_day[day] else None,
        }
        for day in _date_range(start, end)
        if day in total_by_day
    ]
    peak_hours = [
        {
            "hour": hour,
            "occupied_seconds": occupied_by_hour.get(hour, 0.0),
            "total_seconds": total_by_hour.get(hour, 0.0),
            "rate": (occupied_by_hour.get(hour, 0.0) / total_by_hour[hour]) if total_by_hour.get(hour) else None,
        }
        for hour in range(24)
    ]

    sessions = _occupied_sessions(conn, space_ids, start, end)
    durations_by_space: dict[int, list[float]] = defaultdict(list)
    turnover_counts: dict[str, int] = defaultdict(int)
    for session in sessions:
        durations_by_space[session["space_id"]].append(
            (session["end"] - session["start"]).total_seconds()
        )
        turnover_counts[session["start"].date().isoformat()] += 1
    dwell_by_space = [
        {
            "space_id": s["space_id"],
            "label": s["label"],
            "sessions": len(durations_by_space.get(s["space_id"], [])),
            "average_seconds": (
                sum(durations_by_space[s["space_id"]]) / len(durations_by_space[s["space_id"]])
                if durations_by_space.get(s["space_id"])
                else None
            ),
        }
        for s in spaces
    ]
    turnover_by_day = [
        {"date": day, "arrivals": turnover_counts.get(day, 0)} for day in _date_range(start, end)
    ]

    total_occupied = sum(d["occupied_seconds"] for d in occupancy_by_day)
    total_observed = sum(d["total_seconds"] for d in occupancy_by_day)
    dwell_values = [d["average_seconds"] for d in dwell_by_space if d["average_seconds"] is not None]
    hours_with_data = [h for h in peak_hours if h["rate"] is not None]
    peak = max(hours_with_data, key=lambda h: h["rate"], default=None)
    off_peak = min(hours_with_data, key=lambda h: h["rate"], default=None)

    summary = {
        "space_count": len(spaces),
        "overall_occupancy_rate": (total_occupied / total_observed) if total_observed else None,
        "total_arrivals": sum(turnover_counts.values()),
        "overall_average_dwell_seconds": (
            sum(dwell_values) / len(dwell_values) if dwell_values else None
        ),
        "peak_hour": peak["hour"] if peak else None,
        "peak_hour_rate": peak["rate"] if peak else None,
        "off_peak_hour": off_peak["hour"] if off_peak else None,
        "off_peak_hour_rate": off_peak["rate"] if off_peak else None,
    }

    return {
        "lot_id": lot_id,
        "camera_id": camera_id,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "spaces": spaces,
        "occupancy_by_day": occupancy_by_day,
        "dwell_by_space": dwell_by_space,
        "turnover_by_day": turnover_by_day,
        "peak_hours": peak_hours,
        "summary": summary,
    }
