"""Database side of traffic counting: counting lines and the crossings
recorded on them, plus the 15-minute count tables built from them.

Tables
------
count_lines     one row per line drawn on a traffic camera. Coordinates are
                fractions of the picture (0-1), so they don't depend on the
                stream's resolution. forward_label/reverse_label are the names
                shown for each direction (e.g. "Northbound"/"Southbound").
                entry_direction marks a lot driveway: "forward" or "reverse"
                is the direction that goes INTO the lot (the other one is out);
                NULL means the line isn't a driveway (e.g. a street).
line_crossings  one row per vehicle crossing a line: when (UTC), which way,
                and what kind of vehicle.
traffic_coverage  the stretches of time a traffic camera was actually being
                watched (pictures analysed). Anything outside them is time
                nothing was counted -- camera down, program off, paused -- so
                counts can say how complete they are instead of silently
                coming out low.

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

CREATE TABLE IF NOT EXISTS traffic_coverage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    camera_id TEXT NOT NULL,
    start_at TEXT NOT NULL,
    end_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_traffic_coverage_camera_time ON traffic_coverage(camera_id, end_at);
"""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def migrate(conn: sqlite3.Connection) -> None:
    """Idempotent: adds cameras.kind and the two traffic tables if missing."""
    columns = {row[1] for row in conn.execute("PRAGMA table_info(cameras)")}
    if "kind" not in columns:
        conn.execute("ALTER TABLE cameras ADD COLUMN kind TEXT NOT NULL DEFAULT 'parking'")
    conn.executescript(SCHEMA)
    coverage_columns = {row[1] for row in conn.execute("PRAGMA table_info(traffic_coverage)")}
    if "gap_reason" not in coverage_columns:
        # Why the gap BEFORE this watched stretch happened (see GAP_LABELS).
        conn.execute("ALTER TABLE traffic_coverage ADD COLUMN gap_reason TEXT")
        conn.execute("ALTER TABLE traffic_coverage ADD COLUMN gap_detail TEXT")
    line_columns = {row[1] for row in conn.execute("PRAGMA table_info(count_lines)")}
    if "entry_direction" not in line_columns:
        conn.execute("ALTER TABLE count_lines ADD COLUMN entry_direction TEXT")


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


def _clean_entry_direction(value: Any) -> Optional[str]:
    if value in (None, "", "none"):
        return None
    if value not in ("forward", "reverse"):
        raise ValueError('entry_direction must be "forward", "reverse" or null')
    return value


def create_line(conn: sqlite3.Connection, camera_id: str, payload: dict, default_labels) -> dict:
    x1, y1, x2, y2 = _clean_coords(payload)
    entry_direction = _clean_entry_direction(payload.get("entry_direction"))
    name = _clean_name(payload.get("name"), "name")
    fwd_default, rev_default = default_labels(x1, y1, x2, y2)
    forward = _clean_name(payload.get("forward_label") or fwd_default, "forward_label")
    reverse = _clean_name(payload.get("reverse_label") or rev_default, "reverse_label")
    now = utc_now()
    cursor = conn.execute(
        """INSERT INTO count_lines (camera_id, name, x1, y1, x2, y2, forward_label, reverse_label,
                                      entry_direction, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (camera_id, name, x1, y1, x2, y2, forward, reverse, entry_direction, now, now),
    )
    return get_line(conn, cursor.lastrowid)


def update_line(conn: sqlite3.Connection, line_id: int, payload: dict) -> Optional[dict]:
    """Changes any of: name, forward_label, reverse_label, entry_direction,
    or the points. Moving a line keeps its recorded crossings."""
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
    if "entry_direction" in payload:
        fields["entry_direction"] = _clean_entry_direction(payload["entry_direction"])
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
    conn.execute("DELETE FROM traffic_coverage WHERE camera_id = ?", (camera_id,))
    return conn.execute("DELETE FROM count_lines WHERE camera_id = ?", (camera_id,)).rowcount


# --- Crossings -------------------------------------------------------------------


def add_crossings(conn: sqlite3.Connection, rows: Iterable[dict]) -> None:
    conn.executemany(
        """INSERT INTO line_crossings (line_id, camera_id, crossed_at, direction, vehicle_class, track_id)
           SELECT :line_id, :camera_id, :crossed_at, :direction, :vehicle_class, :track_id
           WHERE EXISTS (SELECT 1 FROM count_lines WHERE id = :line_id)""",
        list(rows),
    )


# --- Coverage (when the camera was actually being watched) -----------------------

# A gap shorter than this between analysed pictures isn't reported as missed
# time (pictures come every 0.2 s; a few seconds' hiccup is noise).
MIN_REPORTED_GAP_SECONDS = 5.0
# Coverage is saved to the database every few seconds, so "not watched right
# now" is only claimed once the last saved stretch is older than this.
ONGOING_GAP_SECONDS = 30.0


# Why nothing was counted during a gap. "planned" gaps are ones we caused on
# purpose (installing an update, pausing), as opposed to something failing.
GAP_LABELS = {
    "update": ("Software update", True),
    "restart": ("Program restarted", True),
    "stopped": ("Program was stopped", True),
    "first_start": ("Monitoring started", True),
    "paused": ("Paused", True),
    "crash": ("Program crashed", False),
    "reboot": ("Computer restarted", False),
    "power": ("Power outage (computer restarted by itself)", False),
    "camera": ("Camera stopped sending pictures", False),
    "no_pictures": ("No pictures arrived (computer asleep or busy?)", False),
    "ongoing": ("Not counting right now", False),
}


def gap_label(reason: Optional[str]) -> tuple:
    return GAP_LABELS.get(reason or "", ("Unknown (before causes were recorded)", False))


def open_coverage(conn: sqlite3.Connection, camera_id: str, start: str,
                  gap_reason: Optional[str] = None, gap_detail: Optional[str] = None) -> int:
    cursor = conn.execute(
        """INSERT INTO traffic_coverage (camera_id, start_at, end_at, gap_reason, gap_detail)
           VALUES (?, ?, ?, ?, ?)""",
        (camera_id, start, start, gap_reason, gap_detail),
    )
    return int(cursor.lastrowid)


def extend_coverage(conn: sqlite3.Connection, coverage_id: int, end: str) -> None:
    conn.execute("UPDATE traffic_coverage SET end_at = ? WHERE id = ? AND end_at < ?", (end, coverage_id, end))


def _coverage_intervals(conn: sqlite3.Connection, camera_id: str, start: datetime, end: datetime) -> list:
    """Watched stretches clipped to the range: (start, end, gap_reason,
    gap_detail), the reason being why the gap BEFORE the stretch happened."""
    rows = conn.execute(
        """SELECT start_at, end_at, gap_reason, gap_detail FROM traffic_coverage
           WHERE camera_id = ? AND end_at > ? AND start_at < ? ORDER BY start_at""",
        (camera_id, _utc_text(start), _utc_text(end)),
    ).fetchall()
    intervals = []
    for row in rows:
        a = max(datetime.fromisoformat(row["start_at"]), start)
        b = min(datetime.fromisoformat(row["end_at"]), end)
        if b >= a:  # a just-opened stretch has no length yet but still ends a gap
            intervals.append((a, b, row["gap_reason"], row["gap_detail"]))
    return intervals


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

    # How much of each interval was actually watched.
    intervals = _coverage_intervals(conn, camera_id, start, end)
    starts_utc = [datetime.fromisoformat(b["start"]) for b in bins]
    for b in bins:
        b["monitored_seconds"] = 0.0
    if bins:
        origin = starts_utc[0]
        for a, z, *_ in intervals:
            i = max(0, int((a - origin) / step))
            while i < len(bins):
                b_start = starts_utc[i]
                b_end = b_start + step
                if b_start >= z:
                    break
                overlap = (min(z, b_end) - max(a, b_start)).total_seconds()
                if overlap > 0:
                    bins[i]["monitored_seconds"] += overlap
                i += 1
    for b in bins:
        b["monitored_seconds"] = round(b["monitored_seconds"], 1)

    # Missed stretches between watched ones (and a still-open one at the end
    # when the camera isn't being watched right now).
    def gap(a: datetime, z: datetime, reason: Optional[str], detail: Optional[str], ongoing: bool) -> dict:
        label, planned = gap_label(reason)
        return {"start": a.astimezone().isoformat(), "end": z.astimezone().isoformat(),
                "seconds": round((z - a).total_seconds()), "ongoing": ongoing,
                "reason": reason, "label": label, "planned": planned, "detail": detail or ""}

    gaps = []
    for (_a1, z1, *_), (a2, _z2, reason, detail) in zip(intervals, intervals[1:]):
        if (a2 - z1).total_seconds() >= MIN_REPORTED_GAP_SECONDS:
            gaps.append(gap(z1, a2, reason, detail, False))
    now = datetime.now(timezone.utc)
    if intervals and end > now:
        last_end = intervals[-1][1]
        if (now - last_end).total_seconds() >= ONGOING_GAP_SECONDS:
            gaps.append(gap(last_end, now, "ongoing", "", True))
    monitored = sum((z - a).total_seconds() for a, z, *_ in intervals)

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
            {k: line.get(k) for k in ("id", "name", "forward_label", "reverse_label", "entry_direction")}
            | {"totals": totals[line["id"]]}
            for line in lines
        ],
        "bins": bins,
        "total": sum(per_bin_total),
        "peak_hour": peak,
        "monitored_seconds": round(monitored),
        "monitoring_started": intervals[0][0].astimezone().isoformat() if intervals else None,
        "gaps": gaps,
        "missed_seconds": sum(g["seconds"] for g in gaps),
        "missed_planned_seconds": sum(g["seconds"] for g in gaps if g["planned"]),
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
    header += ["Total", "Minutes monitored"]
    writer.writerow(header)
    for b in report["bins"]:
        start = datetime.fromisoformat(b["start"])
        end = datetime.fromisoformat(b["end"])
        values = [b["counts"].get(line_id, {}).get(direction, 0) for line_id, direction in columns]
        minutes = round(b.get("monitored_seconds", 0) / 60, 1)
        writer.writerow([start.strftime("%Y-%m-%d"), start.strftime("%H:%M"), end.strftime("%H:%M"), *values, sum(values), minutes])
    totals = []
    for line in report["lines"]:
        totals += [line["totals"]["forward"], line["totals"]["reverse"]]
    writer.writerow(["Total", "", "", *totals, sum(totals), round(report.get("monitored_seconds", 0) / 60, 1)])
    return out.getvalue()


# --- Lot entries and exits ----------------------------------------------------------

# An hour counts toward "average by hour of day" only when every driveway
# camera in the lot was watched for at least this share of it; a half-missed
# hour would otherwise drag the average down.
MIN_HOUR_COVERAGE = 0.5


def lot_flow(conn: sqlite3.Connection, lot_id: int, start: datetime, end: datetime) -> Optional[dict]:
    """Vehicles entering and leaving a lot, from the traffic cameras assigned to
    it and their lines marked as driveways. None when the lot has no driveway
    lines (so pages can leave the section out).

    Hours and days are local time. Coverage per hour is the lowest of the
    driveway cameras' (a lot is only fully counted when every entrance is)."""
    lines = conn.execute(
        """SELECT count_lines.* FROM count_lines
           JOIN cameras ON cameras.id = count_lines.camera_id
           WHERE cameras.lot_id = ? AND cameras.kind = 'traffic' AND count_lines.entry_direction IS NOT NULL
           ORDER BY count_lines.id""",
        (lot_id,),
    ).fetchall()
    if not lines:
        return None
    entry_by_line = {row["id"]: row["entry_direction"] for row in lines}
    camera_ids = sorted({row["camera_id"] for row in lines})

    now = datetime.now(timezone.utc)
    stop = min(end, now)
    # Local hour slots covering the range.
    hour = timedelta(hours=1)
    first = start.astimezone().replace(minute=0, second=0, microsecond=0)
    slots: list = []
    cursor = first
    while cursor < stop and len(slots) < 24 * 400:
        slots.append(cursor)
        cursor = (cursor.astimezone(timezone.utc) + hour).astimezone()
    index = {s.astimezone(timezone.utc): i for i, s in enumerate(slots)}
    ins = [0] * len(slots)
    outs = [0] * len(slots)

    marks = ",".join("?" for _ in entry_by_line)
    rows = conn.execute(
        f"""SELECT line_id, crossed_at, direction FROM line_crossings
            WHERE line_id IN ({marks}) AND crossed_at >= ? AND crossed_at < ?""",
        (*entry_by_line, _utc_text(start), _utc_text(end)),
    ).fetchall()
    for row in rows:
        crossed = datetime.fromisoformat(row["crossed_at"]).astimezone()
        slot = index.get(crossed.replace(minute=0, second=0, microsecond=0).astimezone(timezone.utc))
        if slot is None:
            continue
        if row["direction"] == entry_by_line[row["line_id"]]:
            ins[slot] += 1
        else:
            outs[slot] += 1

    # Seconds watched per slot: the minimum over the driveway cameras.
    watched = [None] * len(slots)
    for camera_id in camera_ids:
        per_slot = [0.0] * len(slots)
        for a, z, *_ in _coverage_intervals(conn, camera_id, start, stop):
            i = max(0, int((a - slots[0]) / hour)) if slots else 0
            while i < len(slots):
                s0 = slots[i]
                s1 = s0 + hour
                if s0 >= z:
                    break
                overlap = (min(z, s1) - max(a, s0)).total_seconds()
                if overlap > 0:
                    per_slot[i] += overlap
                i += 1
        watched = [v if w is None else min(w, v) for w, v in zip(watched, per_slot)]
    watched = [w or 0.0 for w in watched]

    def slot_length(i: int) -> float:
        s0 = slots[i]
        return (min(s0 + hour, stop) - max(s0, start)).total_seconds()

    days: dict = {}
    for i, s in enumerate(slots):
        day = days.setdefault(s.date().isoformat(), {"date": s.date().isoformat(), "in": 0, "out": 0,
                                                     "monitored_seconds": 0.0, "hours": []})
        length = max(1.0, slot_length(i))
        coverage = min(1.0, watched[i] / length)
        day["in"] += ins[i]
        day["out"] += outs[i]
        day["monitored_seconds"] += watched[i]
        day["hours"].append({"hour": s.hour, "in": ins[i], "out": outs[i], "coverage": round(coverage, 3)})

    by_hour = []
    for h in range(24):
        cells = [c for d in days.values() for c in d["hours"] if c["hour"] == h and c["coverage"] >= MIN_HOUR_COVERAGE]
        n = len(cells)
        by_hour.append({
            "hour": h,
            "days": n,
            "in": round(sum(c["in"] for c in cells) / n, 2) if n else None,
            "out": round(sum(c["out"] for c in cells) / n, 2) if n else None,
        })
    busiest = max((b for b in by_hour if b["in"] is not None), key=lambda b: b["in"] + b["out"], default=None)
    total_seconds = sum(slot_length(i) for i in range(len(slots)))
    monitored = sum(watched)
    return {
        "lines": [
            {"id": row["id"], "camera_id": row["camera_id"], "name": row["name"],
             "in_label": row["forward_label"] if row["entry_direction"] == "forward" else row["reverse_label"],
             "out_label": row["reverse_label"] if row["entry_direction"] == "forward" else row["forward_label"]}
            for row in lines
        ],
        "totals": {"in": sum(ins), "out": sum(outs)},
        "by_day": [{k: d[k] for k in ("date", "in", "out")} | {"monitored_seconds": round(d["monitored_seconds"])}
                   for d in days.values()],
        "hourly_by_day": [{"date": d["date"], "hours": d["hours"]} for d in days.values()],
        "by_hour": by_hour,
        "busiest_hour": None if busiest is None or (busiest["in"] + busiest["out"]) == 0 else busiest,
        "monitored_seconds": round(monitored),
        "range_seconds": round(total_seconds),
    }
