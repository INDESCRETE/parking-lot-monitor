"""Database side of traffic counting: counting lines and the crossings
recorded on them, plus the 15-minute count tables built from them.

Tables
------
count_lines     one row per line drawn on a traffic camera. Coordinates are
                fractions of the picture (0-1), so they don't depend on the
                stream's resolution. forward_label/reverse_label are the names
                shown for each direction (e.g. "Northbound"/"Southbound").
line_crossings  one row per vehicle crossing a line: when (UTC), which way,
                and what kind of vehicle.

Times are stored in UTC, like everything else in the app; counts are
grouped into local-time intervals (e.g. 4:00-4:15 PM on the local clock).
"""

from __future__ import annotations

import csv
import io
import math
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Optional

VEHICLE_CLASSES = ("car", "truck", "bus", "motorcycle")
MAX_NAME_LENGTH = 60

SCHEMA = """
CREATE TABLE IF NOT EXISTS count_lines (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    camera_id TEXT NOT NULL,
    name TEXT NOT NULL,
    x1 REAL NOT NULL,
    y1 REAL NOT NULL,
    x2 REAL NOT NULL,
    y2 REAL NOT NULL,
    forward_label TEXT NOT NULL,
    reverse_label TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_count_lines_camera ON count_lines(camera_id);

CREATE TABLE IF NOT EXISTS line_crossings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    line_id INTEGER NOT NULL,
    camera_id TEXT NOT NULL,
    crossed_at TEXT NOT NULL,
    direction TEXT NOT NULL CHECK (direction IN ('forward', 'reverse')),
    vehicle_class TEXT NOT NULL,
    track_id INTEGER,
    FOREIGN KEY(line_id) REFERENCES count_lines(id)
);
CREATE INDEX IF NOT EXISTS idx_line_crossings_camera_time ON line_crossings(camera_id, crossed_at);
CREATE INDEX IF NOT EXISTS idx_line_crossings_line_time ON line_crossings(line_id, crossed_at);
"""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def migrate(conn: sqlite3.Connection) -> None:
    """Idempotent: adds cameras.kind and the two traffic tables if missing."""
    columns = {row[1] for row in conn.execute("PRAGMA table_info(cameras)")}
    if "kind" not in columns:
        conn.execute("ALTER TABLE cameras ADD COLUMN kind TEXT NOT NULL DEFAULT 'parking'")
    conn.executescript(SCHEMA)


# --- Lines ---------------------------------------------------------------------


def _clean_name(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} is required")
    value = " ".join(value.split())
    if len(value) > MAX_NAME_LENGTH:
        raise ValueError(f"{field} must be {MAX_NAME_LENGTH} characters or fewer")
    return value


def _clean_coords(payload: dict) -> tuple:
    try:
        coords = tuple(float(payload[key]) for key in ("x1", "y1", "x2", "y2"))
    except (KeyError, TypeError, ValueError):
        raise ValueError("x1, y1, x2 and y2 are required numbers (fractions of the picture, 0 to 1)") from None
    if not all(math.isfinite(v) and -0.001 <= v <= 1.001 for v in coords):
        raise ValueError("line points must be inside the picture (fractions from 0 to 1)")
    coords = tuple(min(1.0, max(0.0, v)) for v in coords)
    if math.hypot(coords[2] - coords[0], coords[3] - coords[1]) < 0.01:
        raise ValueError("the line is too short: click two points further apart")
    return coords


def _line_row(row: sqlite3.Row) -> dict:
    return dict(row)


def list_lines(conn: sqlite3.Connection, camera_id: str) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM count_lines WHERE camera_id = ? ORDER BY id", (camera_id,)
    ).fetchall()
    return [_line_row(r) for r in rows]


def get_line(conn: sqlite3.Connection, line_id: int) -> Optional[dict]:
    row = conn.execute("SELECT * FROM count_lines WHERE id = ?", (line_id,)).fetchone()
    return _line_row(row) if row else None


def create_line(conn: sqlite3.Connection, camera_id: str, payload: dict, default_labels) -> dict:
    x1, y1, x2, y2 = _clean_coords(payload)
    name = _clean_name(payload.get("name"), "name")
    fwd_default, rev_default = default_labels(x1, y1, x2, y2)
    forward = _clean_name(payload.get("forward_label") or fwd_default, "forward_label")
    reverse = _clean_name(payload.get("reverse_label") or rev_default, "reverse_label")
    now = utc_now()
    cursor = conn.execute(
        """INSERT INTO count_lines (camera_id, name, x1, y1, x2, y2, forward_label, reverse_label, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (camera_id, name, x1, y1, x2, y2, forward, reverse, now, now),
    )
    return get_line(conn, cursor.lastrowid)


def update_line(conn: sqlite3.Connection, line_id: int, payload: dict) -> Optional[dict]:
    """Changes any of: name, forward_label, reverse_label, or the points.
    Moving a line keeps its recorded crossings."""
    line = get_line(conn, line_id)
    if line is None:
        return None
    fields: dict = {}
    if "name" in payload:
        fields["name"] = _clean_name(payload["name"], "name")
    if "forward_label" in payload:
        fields["forward_label"] = _clean_name(payload["forward_label"], "forward_label")
    if "reverse_label" in payload:
        fields["reverse_label"] = _clean_name(payload["reverse_label"], "reverse_label")
    if any(key in payload for key in ("x1", "y1", "x2", "y2")):
        merged = {key: payload.get(key, line[key]) for key in ("x1", "y1", "x2", "y2")}
        fields.update(zip(("x1", "y1", "x2", "y2"), _clean_coords(merged)))
    if not fields:
        raise ValueError("nothing to change")
    fields["updated_at"] = utc_now()
    assignments = ", ".join(f"{key} = ?" for key in fields)
    conn.execute(f"UPDATE count_lines SET {assignments} WHERE id = ?", (*fields.values(), line_id))
    return get_line(conn, line_id)


def delete_line(conn: sqlite3.Connection, line_id: int) -> Optional[dict]:
    line = get_line(conn, line_id)
    if line is None:
        return None
    removed = conn.execute("DELETE FROM line_crossings WHERE line_id = ?", (line_id,)).rowcount
    conn.execute("DELETE FROM count_lines WHERE id = ?", (line_id,))
    return {"deleted": True, "line_id": line_id, "camera_id": line["camera_id"], "crossings_deleted": removed}


def delete_for_camera(conn: sqlite3.Connection, camera_id: str) -> int:
    conn.execute("DELETE FROM line_crossings WHERE camera_id = ?", (camera_id,))
    return conn.execute("DELETE FROM count_lines WHERE camera_id = ?", (camera_id,)).rowcount


# --- Crossings -------------------------------------------------------------------


def add_crossings(conn: sqlite3.Connection, rows: Iterable[dict]) -> None:
    conn.executemany(
        """INSERT INTO line_crossings (line_id, camera_id, crossed_at, direction, vehicle_class, track_id)
           SELECT :line_id, :camera_id, :crossed_at, :direction, :vehicle_class, :track_id
           WHERE EXISTS (SELECT 1 FROM count_lines WHERE id = :line_id)""",
        list(rows),
    )


def _utc_text(dt: datetime) -> str:
    # Stored values are datetime.isoformat() in UTC; compare like with like.
    return dt.astimezone(timezone.utc).isoformat()


def _floor_local(dt: datetime, bin_minutes: int) -> datetime:
    local = dt.astimezone()
    midnight = local.replace(hour=0, minute=0, second=0, microsecond=0)
    minutes = (local - midnight).total_seconds() // 60
    return (midnight + timedelta(minutes=(minutes // bin_minutes) * bin_minutes)).astimezone()


def counts(
    conn: sqlite3.Connection,
    camera_id: str,
    start: datetime,
    end: datetime,
    bin_minutes: int = 15,
) -> dict:
    """Crossings per line and direction, grouped into local-time intervals.

    Every interval in the range is listed, including empty ones, so a
    quiet 15 minutes shows as 0 rather than disappearing."""
    if bin_minutes not in (5, 15, 30, 60):
        raise ValueError("bin_minutes must be 5, 15, 30 or 60")
    lines = list_lines(conn, camera_id)
    rows = conn.execute(
        """SELECT line_id, crossed_at, direction, vehicle_class FROM line_crossings
           WHERE camera_id = ? AND crossed_at >= ? AND crossed_at < ?
           ORDER BY crossed_at""",
        (camera_id, _utc_text(start), _utc_text(end)),
    ).fetchall()

    step = timedelta(minutes=bin_minutes)
    first = _floor_local(start, bin_minutes)
    bins: list[dict] = []
    index: dict = {}
    cursor = first
    # Walk in UTC so a daylight-saving change can't make intervals overlap.
    while cursor < end and len(bins) < 20000:
        key = cursor.astimezone(timezone.utc)
        index[key] = len(bins)
        bins.append({"start": cursor.astimezone().isoformat(), "end": (cursor + step).astimezone().isoformat(), "counts": {}})
        cursor = (cursor.astimezone(timezone.utc) + step).astimezone()

    line_ids = {line["id"] for line in lines}
    totals: dict = {
        line["id"]: {"forward": 0, "reverse": 0, "by_class": {"forward": {}, "reverse": {}}} for line in lines
    }
    for row in rows:
        if row["line_id"] not in line_ids:
            continue
        crossed = datetime.fromisoformat(row["crossed_at"])
        b = index.get(_floor_local(crossed, bin_minutes).astimezone(timezone.utc))
        if b is None:
            continue
        per_line = bins[b]["counts"].setdefault(str(row["line_id"]), {"forward": 0, "reverse": 0})
        per_line[row["direction"]] += 1
        total = totals[row["line_id"]]
        total[row["direction"]] += 1
        by_class = total["by_class"][row["direction"]]
        by_class[row["vehicle_class"]] = by_class.get(row["vehicle_class"], 0) + 1

    # The busiest single hour (four consecutive 15-min intervals etc.), the
    # number traffic studies usually ask for.
    per_bin_total = [
        sum(v["forward"] + v["reverse"] for v in b["counts"].values()) for b in bins
    ]
    window = max(1, 60 // bin_minutes)
    peak = None
    for i in range(0, max(0, len(bins) - window + 1)):
        volume = sum(per_bin_total[i:i + window])
        if volume > 0 and (peak is None or volume > peak["volume"]):
            peak = {"start": bins[i]["start"], "end": bins[i + window - 1]["end"], "volume": volume}

    return {
        "camera_id": camera_id,
        "start": start.astimezone().isoformat(),
        "end": end.astimezone().isoformat(),
        "bin_minutes": bin_minutes,
        "lines": [
            {k: line[k] for k in ("id", "name", "forward_label", "reverse_label")} | {"totals": totals[line["id"]]}
            for line in lines
        ],
        "bins": bins,
        "total": sum(per_bin_total),
        "peak_hour": peak,
    }


def counts_csv(report: dict) -> str:
    """One row per interval, one column per line and direction (the layout
    traffic engineers expect from a volume count), plus a total."""
    out = io.StringIO()
    writer = csv.writer(out)
    header = ["Date", "Start", "End"]
    columns = []
    for line in report["lines"]:
        for direction, label in (("forward", line["forward_label"]), ("reverse", line["reverse_label"])):
            header.append(f"{line['name']} - {label}")
            columns.append((str(line["id"]), direction))
    header.append("Total")
    writer.writerow(header)
    for b in report["bins"]:
        start = datetime.fromisoformat(b["start"])
        end = datetime.fromisoformat(b["end"])
        values = [b["counts"].get(line_id, {}).get(direction, 0) for line_id, direction in columns]
        writer.writerow([start.strftime("%Y-%m-%d"), start.strftime("%H:%M"), end.strftime("%H:%M"), *values, sum(values)])
    totals = []
    for line in report["lines"]:
        totals += [line["totals"]["forward"], line["totals"]["reverse"]]
    writer.writerow(["Total", "", "", *totals, sum(totals)])
    return out.getvalue()
