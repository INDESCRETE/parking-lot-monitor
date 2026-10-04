"""Tests for live traffic counting: counting lines, crossings, count tables
(app/traffic_store.py), the live worker (app/live_traffic.py) and the
faster-stream camera settings it needs (app/live.py).

Run from the repo root:  python3 -m unittest tests.test_traffic -v
No camera or model needed: a fake detector stands in for RF-DETR.
"""

from __future__ import annotations

import csv
import io
import sqlite3
import unittest
from datetime import datetime, timedelta, timezone

from PIL import Image

from app import live, live_traffic, traffic_store
from app.detection_core import Detection


def make_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    # The cameras table as it was before traffic counting existed.
    conn.execute("CREATE TABLE cameras (id TEXT PRIMARY KEY, name TEXT NOT NULL, created_at TEXT NOT NULL)")
    conn.execute("INSERT INTO cameras VALUES ('reolink_live', 'Reolink Live', '2026-09-01T00:00:00+00:00')")
    traffic_store.migrate(conn)
    return conn


class FakeDb:
    """The methods TrafficCounter needs, on an in-memory database."""

    def __init__(self) -> None:
        self.conn = make_db()

    def list_count_lines(self, camera_id):
        return traffic_store.list_lines(self.conn, camera_id)

    def add_line_crossings(self, rows):
        traffic_store.add_crossings(self.conn, rows)
        self.conn.commit()

    def open_traffic_coverage(self, camera_id, start, reason=None, detail=None):
        return traffic_store.open_coverage(self.conn, camera_id, start, reason, detail)

    def extend_traffic_coverage(self, coverage_id, end):
        traffic_store.extend_coverage(self.conn, coverage_id, end)

    def coverage(self):
        return [tuple(r) for r in self.conn.execute("SELECT start_at, end_at FROM traffic_coverage ORDER BY id")]

    def reasons(self):
        return [tuple(r) for r in self.conn.execute("SELECT gap_reason, gap_detail FROM traffic_coverage ORDER BY id")]


def jpeg(width=400, height=300) -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (width, height), (90, 90, 90)).save(out, "JPEG")
    return out.getvalue()


def iso(seconds: float) -> str:
    return (datetime(2026, 9, 29, 20, 0, tzinfo=timezone.utc) + timedelta(seconds=seconds)).isoformat()


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()

    def test_migration_is_idempotent_and_existing_cameras_are_parking(self):
        traffic_store.migrate(self.conn)
        row = self.conn.execute("SELECT kind FROM cameras WHERE id = 'reolink_live'").fetchone()
        self.assertEqual(row["kind"], "parking")

    def test_create_line_with_default_direction_names(self):
        # Drawn top to bottom: the "forward" side is to the left on screen.
        line = traffic_store.create_line(
            self.conn, "street", {"name": " Main  St ", "x1": 0.5, "y1": 0.1, "x2": 0.5, "y2": 0.9},
            live_traffic.default_direction_labels,
        )
        self.assertEqual(line["name"], "Main St")
        self.assertEqual((line["forward_label"], line["reverse_label"]), ("Right to left", "Left to right"))

    def test_default_labels_for_each_drawing_direction(self):
        f = live_traffic.default_direction_labels
        self.assertEqual(f(0.5, 0.9, 0.5, 0.1)[0], "Left to right")
        self.assertEqual(f(0.1, 0.5, 0.9, 0.5)[0], "Top to bottom")
        self.assertEqual(f(0.9, 0.5, 0.1, 0.5)[0], "Bottom to top")

    def test_bad_lines_are_rejected(self):
        labels = live_traffic.default_direction_labels
        for payload in (
            {"name": "x", "x1": 0.5, "y1": 0.5, "x2": 0.501, "y2": 0.5},  # too short
            {"name": "x", "x1": 0.5, "y1": 0.5, "x2": 1.5, "y2": 0.5},  # off the picture
            {"name": "", "x1": 0.1, "y1": 0.1, "x2": 0.9, "y2": 0.9},  # no name
            {"name": "x", "x1": "a", "y1": 0.1, "x2": 0.9, "y2": 0.9},
        ):
            with self.assertRaises(ValueError):
                traffic_store.create_line(self.conn, "street", payload, labels)

    def test_update_and_delete_line(self):
        line = traffic_store.create_line(
            self.conn, "street", {"name": "A", "x1": 0.1, "y1": 0.5, "x2": 0.9, "y2": 0.5},
            live_traffic.default_direction_labels,
        )
        updated = traffic_store.update_line(self.conn, line["id"], {"forward_label": "Southbound", "x2": 0.8})
        self.assertEqual(updated["forward_label"], "Southbound")
        self.assertAlmostEqual(updated["x2"], 0.8)
        self.assertEqual(updated["x1"], 0.1)
        with self.assertRaises(ValueError):
            traffic_store.update_line(self.conn, line["id"], {})
        traffic_store.add_crossings(self.conn, [
            {"line_id": line["id"], "camera_id": "street", "crossed_at": iso(0), "direction": "forward",
             "vehicle_class": "car", "track_id": 1},
        ])
        result = traffic_store.delete_line(self.conn, line["id"])
        self.assertEqual(result["crossings_deleted"], 1)
        self.assertIsNone(traffic_store.get_line(self.conn, line["id"]))
        self.assertIsNone(traffic_store.update_line(self.conn, line["id"], {"name": "B"}))

    def test_crossings_for_a_deleted_line_are_ignored(self):
        traffic_store.add_crossings(self.conn, [
            {"line_id": 999, "camera_id": "street", "crossed_at": iso(0), "direction": "forward",
             "vehicle_class": "car", "track_id": 1},
        ])
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM line_crossings").fetchone()[0], 0)

    def test_counts_bins_peak_hour_and_csv(self):
        line = traffic_store.create_line(
            self.conn, "street", {"name": "Main St", "x1": 0.5, "y1": 0.1, "x2": 0.5, "y2": 0.9,
                                  "forward_label": "Westbound", "reverse_label": "Eastbound"},
            live_traffic.default_direction_labels,
        )
        start = datetime(2026, 9, 29, 20, 0, tzinfo=timezone.utc)
        rows = []
        # 3 westbound in the first 15 min, 1 eastbound car + 1 truck at 20:40.
        for minute, direction, cls in ((1, "forward", "car"), (5, "forward", "car"), (14, "forward", "truck"),
                                       (40, "reverse", "car"), (41, "reverse", "truck")):
            rows.append({"line_id": line["id"], "camera_id": "street",
                         "crossed_at": (start + timedelta(minutes=minute)).isoformat(),
                         "direction": direction, "vehicle_class": cls, "track_id": minute})
        # Outside the range: must not be counted.
        rows.append({"line_id": line["id"], "camera_id": "street",
                     "crossed_at": (start + timedelta(hours=3)).isoformat(),
                     "direction": "forward", "vehicle_class": "car", "track_id": 99})
        traffic_store.add_crossings(self.conn, rows)

        report = traffic_store.counts(self.conn, "street", start, start + timedelta(hours=2), 15)
        self.assertEqual(len(report["bins"]), 8)  # every interval listed, empty ones too
        key = str(line["id"])
        self.assertEqual(report["bins"][0]["counts"][key], {"forward": 3, "reverse": 0})
        self.assertEqual(report["bins"][2]["counts"][key], {"forward": 0, "reverse": 2})
        self.assertEqual(report["bins"][1]["counts"], {})
        self.assertEqual(report["total"], 5)
        totals = report["lines"][0]["totals"]
        self.assertEqual((totals["forward"], totals["reverse"]), (3, 2))
        self.assertEqual(totals["by_class"]["reverse"], {"car": 1, "truck": 1})
        self.assertEqual(report["peak_hour"]["volume"], 5)

        table = list(csv.reader(io.StringIO(traffic_store.counts_csv(report))))
        self.assertEqual(table[0], ["Date", "Start", "End", "Main St - Westbound", "Main St - Eastbound", "Total",
                                    "Minutes monitored"])
        self.assertEqual(table[1][3:], ["3", "0", "3", "0.0"])
        self.assertEqual(table[-1], ["Total", "", "", "3", "2", "5", "0.0"])
        self.assertEqual(len(table), 1 + 8 + 1)

    def test_counts_report_monitored_time_and_gaps(self):
        start = datetime(2026, 9, 29, 20, 0, tzinfo=timezone.utc)
        # Watched 20:00-20:10 and 20:13-20:30; a 3-minute outage in between.
        a = traffic_store.open_coverage(self.conn, "street", start.isoformat())
        traffic_store.extend_coverage(self.conn, a, (start + timedelta(minutes=10)).isoformat())
        b = traffic_store.open_coverage(self.conn, "street", (start + timedelta(minutes=13)).isoformat())
        traffic_store.extend_coverage(self.conn, b, (start + timedelta(minutes=30)).isoformat())
        report = traffic_store.counts(self.conn, "street", start, start + timedelta(hours=1), 15)
        self.assertEqual([bin_["monitored_seconds"] for bin_ in report["bins"]], [720.0, 900.0, 0.0, 0.0])
        self.assertEqual(report["monitored_seconds"], 27 * 60)
        self.assertEqual(len(report["gaps"]), 1)
        self.assertEqual(report["gaps"][0]["seconds"], 180)
        self.assertFalse(report["gaps"][0]["ongoing"])
        table = list(csv.reader(io.StringIO(traffic_store.counts_csv(report))))
        self.assertEqual([row[-1] for row in table[1:]], ["12.0", "15.0", "0.0", "0.0", "27.0"])

    def test_overlapping_coverage_stretches_are_merged(self):
        start = datetime(2026, 9, 29, 20, 0, tzinfo=timezone.utc)
        t = lambda sec: (start + timedelta(seconds=sec)).isoformat()
        for a_, z in ((0, 100), (99, 101), (98.5, 1200), (1215, 1800)):  # out-of-order pictures overlap
            c = traffic_store.open_coverage(self.conn, "street", t(a_), "no_pictures")
            traffic_store.extend_coverage(self.conn, c, t(z))
        report = traffic_store.counts(self.conn, "street", start, start + timedelta(hours=1), 15)
        self.assertEqual([g["seconds"] for g in report["gaps"]], [15])
        self.assertEqual(report["monitored_seconds"], 1200 + 585)

    def _stretch(self, start, a_min, z_min, reason=None):
        c = traffic_store.open_coverage(self.conn, "street", (start + timedelta(minutes=a_min)).isoformat(), reason)
        traffic_store.extend_coverage(self.conn, c, (start + timedelta(minutes=z_min)).isoformat())

    def test_uptime_leaves_out_pauses_but_counts_everything_else(self):
        start = datetime(2026, 9, 29, 20, 0, tzinfo=timezone.utc)
        self._stretch(start, 0, 20)
        self._stretch(start, 30, 40, "paused")      # 10 min paused: not down
        self._stretch(start, 42, 50, "update")      # 2 min update: down
        self._stretch(start, 51, 60, "camera")      # 1 min camera drop: down
        report = traffic_store.counts(self.conn, "street", start, start + timedelta(hours=1), 15)
        u = report["uptime"]
        self.assertEqual(u["paused_seconds"], 600)
        self.assertEqual(u["down_seconds"], 180)
        self.assertAlmostEqual(u["percent"], round(100 * 47 / 50, 2))
        self.assertEqual(u["target_percent"], 99.0)

    def test_time_off_before_the_first_stretch_counts_when_watched_before(self):
        start = datetime(2026, 9, 29, 20, 0, tzinfo=timezone.utc)
        self._stretch(start, -30, -20)             # watched yesterday-ish
        self._stretch(start, 10, 60, "crash")      # program crashed, back at 20:10
        report = traffic_store.counts(self.conn, "street", start, start + timedelta(hours=1), 15)
        self.assertEqual([(g["reason"], g["seconds"]) for g in report["gaps"]], [("crash", 600)])
        self.assertAlmostEqual(report["uptime"]["percent"], round(100 * 50 / 60, 2))

    def test_brand_new_camera_has_no_leading_gap(self):
        start = datetime(2026, 9, 29, 20, 0, tzinfo=timezone.utc)
        self._stretch(start, 10, 60, "first_start")
        report = traffic_store.counts(self.conn, "street", start, start + timedelta(hours=1), 15)
        self.assertEqual(report["gaps"], [])
        self.assertEqual(report["uptime"]["percent"], 100.0)

    def test_open_gap_while_paused_is_a_pause(self):
        now = datetime.now(timezone.utc)
        self._stretch(now, -30, -10)
        self.conn.execute("ALTER TABLE cameras ADD COLUMN paused INTEGER NOT NULL DEFAULT 0")
        self.conn.execute("INSERT INTO cameras (id, name, created_at, paused) VALUES ('street', 'Street', '2026-09-01', 1)")
        report = traffic_store.counts(self.conn, "street", now - timedelta(hours=1), now + timedelta(hours=1), 15)
        last = report["gaps"][-1]
        self.assertTrue(last["ongoing"])
        self.assertEqual(last["reason"], "paused")
        self.assertTrue(last["planned"])
        self.assertEqual(report["uptime"]["percent"], 100.0)
        self.assertEqual(report["problem_seconds"], 0)

    def test_open_gap_when_not_paused_is_down(self):
        now = datetime.now(timezone.utc)
        self._stretch(now, -30, -10)
        report = traffic_store.counts(self.conn, "street", now - timedelta(hours=1), now + timedelta(hours=1), 15)
        self.assertEqual(report["gaps"][-1]["reason"], "ongoing")
        self.assertLess(report["uptime"]["percent"], 100.0)

    def test_bad_bin_size(self):
        now = datetime.now(timezone.utc)
        with self.assertRaises(ValueError):
            traffic_store.counts(self.conn, "street", now - timedelta(hours=1), now, 7)


class LotFlowTests(unittest.TestCase):
    """Entries/exits for a lot, from driveway lines on its traffic cameras."""

    def setUp(self):
        self.conn = make_db()
        self.conn.execute("ALTER TABLE cameras ADD COLUMN lot_id INTEGER")
        self.conn.execute("INSERT INTO cameras (id, name, created_at, lot_id, kind) VALUES ('gate', 'Gate', 'x', 7, 'traffic')")
        self.conn.execute("INSERT INTO cameras (id, name, created_at, lot_id, kind) VALUES ('road', 'Road', 'x', 8, 'traffic')")
        labels = live_traffic.default_direction_labels
        self.gate = traffic_store.create_line(
            self.conn, "gate", {"name": "Main gate", "x1": 0.5, "y1": 0, "x2": 0.5, "y2": 1,
                                "forward_label": "Leaving", "reverse_label": "Arriving", "entry_direction": "reverse"},
            labels)
        self.street = traffic_store.create_line(
            self.conn, "gate", {"name": "Street", "x1": 0.1, "y1": 0.5, "x2": 0.9, "y2": 0.5}, labels)
        self.day = datetime(2026, 9, 29, 0, 0).astimezone()  # local midnight

    def cross(self, line, direction, minutes):
        traffic_store.add_crossings(self.conn, [{
            # Stored in UTC, like the live counter does.
            "line_id": line["id"], "camera_id": "gate",
            "crossed_at": (self.day + timedelta(minutes=minutes)).astimezone(timezone.utc).isoformat(),
            "direction": direction, "vehicle_class": "car", "track_id": minutes}])

    def watch(self, from_min, to_min):
        utc = lambda m: (self.day + timedelta(minutes=m)).astimezone(timezone.utc).isoformat()
        c = traffic_store.open_coverage(self.conn, "gate", utc(from_min))
        traffic_store.extend_coverage(self.conn, c, utc(to_min))

    def test_no_driveway_lines_means_no_section(self):
        self.assertIsNone(traffic_store.lot_flow(self.conn, 8, self.day, self.day + timedelta(days=1)))

    def test_entries_exits_by_hour_with_coverage(self):
        self.watch(8 * 60, 10 * 60 + 10)  # 8:00-10:10 watched; 10:00 hour only 10 min
        for m in (8 * 60 + 5, 8 * 60 + 20, 9 * 60 + 1):
            self.cross(self.gate, "reverse", m)  # arriving = in
        self.cross(self.gate, "forward", 9 * 60 + 30)  # leaving = out
        self.cross(self.gate, "forward", 10 * 60 + 5)
        self.cross(self.street, "forward", 8 * 60 + 30)  # not a driveway: ignored
        flow = traffic_store.lot_flow(self.conn, 7, self.day, self.day + timedelta(days=1))
        self.assertEqual(flow["totals"], {"in": 3, "out": 2})
        self.assertEqual(flow["lines"][0]["in_label"], "Arriving")
        hours = {h["hour"]: h for h in flow["hourly_by_day"][0]["hours"]}
        self.assertEqual((hours[8]["in"], hours[8]["out"], hours[8]["coverage"]), (2, 0, 1.0))
        self.assertEqual((hours[9]["in"], hours[9]["out"]), (1, 1))
        self.assertLess(hours[10]["coverage"], 0.5)
        by_hour = {h["hour"]: h for h in flow["by_hour"]}
        self.assertEqual(by_hour[8]["in"], 2)
        self.assertIsNone(by_hour[10]["in"], "a mostly-missed hour is left out of the average")
        self.assertEqual(flow["busiest_hour"]["hour"], 8)
        self.assertEqual(flow["monitored_seconds"], 130 * 60)
        self.assertEqual(flow["by_day"][0]["in"], 3)

    def test_vehicles_in_lot_from_starting_count(self):
        at = lambda m: self.day + timedelta(minutes=m)
        self.assertEqual(traffic_store.lot_occupancy(self.conn, 7, now=at(60))["started"], False)
        self.assertIsNone(traffic_store.lot_occupancy(self.conn, 8, now=at(60)), "no entrance/exit lines")
        self.cross(self.gate, "reverse", 5)  # before the starting count: ignored
        traffic_store.set_lot_count(self.conn, 7, 10, at(10))
        self.watch(10, 50)
        self.watch(55, 120)  # 5-minute gap 50-55
        for m in (20, 30, 40):
            self.cross(self.gate, "reverse", m)  # in
        self.cross(self.gate, "forward", 45)  # out
        self.cross(self.street, "forward", 46)  # street line: ignored
        occ = traffic_store.lot_occupancy(self.conn, 7, now=at(60))
        self.assertEqual((occ["current"], occ["entered"], occ["left"], occ["starting_count"]), (12, 3, 1, 10))
        self.assertEqual([g["seconds"] for g in occ["gaps"]], [300])
        with self.assertRaises(ValueError):
            traffic_store.set_lot_count(self.conn, 7, -1)
        # A new starting count replaces the old one from then on.
        traffic_store.set_lot_count(self.conn, 7, 4, at(50))
        self.assertEqual(traffic_store.lot_occupancy(self.conn, 7, now=at(60))["current"], 4)

    def test_accumulation_by_hour(self):
        at = lambda m: self.day + timedelta(minutes=m)
        self.watch(0, 24 * 60)
        traffic_store.set_lot_count(self.conn, 7, 2, at(8 * 60))
        for m in (8 * 60 + 10, 8 * 60 + 20, 8 * 60 + 30):
            self.cross(self.gate, "reverse", m)  # 5 inside by 8:30
        self.cross(self.gate, "forward", 8 * 60 + 50)  # 4 at the end of 8:00-9:00
        self.cross(self.gate, "forward", 9 * 60 + 5)  # 3
        flow = traffic_store.lot_flow(self.conn, 7, self.day, self.day + timedelta(hours=11))
        acc = flow["accumulation"]
        by_hour = {datetime.fromisoformat(h["start"]).hour: h for h in acc["hours"]}
        self.assertIsNone(by_hour[7]["peak"], "no starting count yet")
        self.assertEqual((by_hour[8]["peak"], by_hour[8]["end"]), (5, 4))
        self.assertEqual((by_hour[9]["peak"], by_hour[9]["end"]), (4, 3))
        self.assertEqual((by_hour[10]["peak"], by_hour[10]["end"]), (3, 3))
        self.assertEqual(acc["peak"]["count"], 5)
        self.assertFalse(acc["went_negative"])

    def test_street_traffic_uses_only_non_driveway_lines(self):
        self.assertIsNone(traffic_store.lot_street_traffic(self.conn, 8, self.day, self.day + timedelta(days=1)))
        self.watch(8 * 60, 10 * 60 + 10)  # 10:00 hour only 10 min watched
        self.cross(self.street, "forward", 8 * 60 + 5)
        self.cross(self.street, "forward", 8 * 60 + 40)
        self.cross(self.street, "reverse", 9 * 60 + 15)
        self.cross(self.street, "reverse", 10 * 60 + 2)
        self.cross(self.gate, "reverse", 8 * 60 + 30)  # driveway: not street traffic
        street = traffic_store.lot_street_traffic(self.conn, 7, self.day, self.day + timedelta(days=1))
        self.assertEqual([l["name"] for l in street["lines"]], ["Street"])
        line = street["lines"][0]
        self.assertEqual(line["totals"], {"forward": 2, "reverse": 2})
        self.assertEqual(street["total"], 4)
        by_hour = {h["hour"]: h for h in line["by_hour"]}
        self.assertEqual((by_hour[8]["forward"], by_hour[8]["reverse"]), (2, 0))
        self.assertEqual((by_hour[9]["forward"], by_hour[9]["reverse"]), (0, 1))
        self.assertIsNone(by_hour[10]["forward"], "a mostly-missed hour is left out of the average")
        self.assertEqual(line["busiest_hour"]["hour"], 8)
        self.assertEqual(line["monitored_seconds"], 130 * 60)
        self.assertEqual(line["by_day"][0]["reverse"], 2)
        # Marking it as an entrance moves it out of street traffic.
        traffic_store.update_line(self.conn, self.street["id"], {"entry_direction": "forward"})
        self.assertIsNone(traffic_store.lot_street_traffic(self.conn, 7, self.day, self.day + timedelta(days=1)))

    def test_entry_direction_can_be_changed_and_cleared(self):
        line = traffic_store.update_line(self.conn, self.street["id"], {"entry_direction": "forward"})
        self.assertEqual(line["entry_direction"], "forward")
        line = traffic_store.update_line(self.conn, self.street["id"], {"entry_direction": None})
        self.assertIsNone(line["entry_direction"])
        with self.assertRaises(ValueError):
            traffic_store.update_line(self.conn, self.street["id"], {"entry_direction": "sideways"})


class CounterTests(unittest.TestCase):
    """A car box moving left to right across a vertical line in the middle."""

    def setUp(self):
        self.db = FakeDb()
        self.line = traffic_store.create_line(
            self.db.conn, "street", {"name": "Middle", "x1": 0.5, "y1": 0.0, "x2": 0.5, "y2": 1.0},
            live_traffic.default_direction_labels,
        )
        self.boxes: list = []
        self.counter = live_traffic.TrafficCounter(self.db, detector=lambda image: list(self.boxes))
        self.frame = jpeg()

    def drive(self, xs, start_t=0.0, step=0.2, camera="street"):
        saved = []
        for i, x in enumerate(xs):
            self.boxes = [] if x is None else [Detection(3, "car", 0.8, (x, 150, x + 40, 180))]
            saved += self.counter.process_frame(camera, self.frame, iso(start_t + i * step))
        return saved

    def test_one_car_crossing_is_counted_once_with_direction(self):
        # Ground point = box center x + 20. Crosses x=200 between 170 and 200.
        saved = self.drive([100, 130, 160, 190, 220, 250, 280, 280, 280])
        self.assertEqual(len(saved), 1)
        self.assertEqual(saved[0]["direction"], "reverse")  # this line was drawn top to bottom
        self.assertEqual(self.line["reverse_label"], "Left to right")
        stored = self.db.conn.execute("SELECT direction, vehicle_class FROM line_crossings").fetchall()
        self.assertEqual([tuple(r) for r in stored], [("reverse", "car")])
        view = self.counter.view("street")
        self.assertEqual(view["recent_crossings"][0]["direction_label"], "Left to right")
        self.assertEqual(view["frame_size"], [400, 300])
        self.assertEqual(view["status"]["frames_processed"], 9)
        self.assertTrue(view["tracks"][0]["counted"])
        self.assertIsNotNone(self.counter.last_jpeg("street"))

    def test_car_that_stops_short_of_the_line_is_not_counted(self):
        self.assertEqual(self.drive([60, 80, 100, 120, 140, 140, 140]), [])

    def test_line_added_while_running_is_used(self):
        self.drive([20, 40, 60])
        traffic_store.create_line(
            self.db.conn, "street", {"name": "Early", "x1": 0.3, "y1": 0.0, "x2": 0.3, "y2": 1.0},
            live_traffic.default_direction_labels,
        )
        self.counter.lines_changed("street")
        saved = self.drive([80, 100, 120, 140, 160, 180, 200, 220], start_t=0.6)
        self.assertEqual(sorted(r["line_id"] for r in saved), sorted([self.line["id"], self.line["id"] + 1]))

    def test_outage_starts_the_tracker_fresh(self):
        self.drive([100, 130, 160])
        # 30 s later a car appears on the other side: must not be linked to
        # the old track and counted as a crossing.
        saved = self.drive([300, 310, 320], start_t=30.0)
        self.assertEqual(saved, [])

    def test_frames_without_detections_still_update_status(self):
        self.drive([None, None])
        status = self.counter.status("street")
        self.assertEqual(status["frames_processed"], 2)
        self.assertEqual(status["last_frame_at"], iso(0.2))

    def test_reset_camera_drops_picture_and_tracks_but_keeps_counters(self):
        self.drive([100, 130, 160, 190, 220, 250])
        self.counter.reset_camera("street")
        self.assertIsNone(self.counter.last_jpeg("street"))
        view = self.counter.view("street")
        self.assertNotIn("tracks", view)
        self.assertEqual(len(view["recent_crossings"]), 1)
        self.assertEqual(view["status"]["frames_processed"], 6)
        # Starts over with a fresh tracker: the same car isn't counted twice.
        self.assertEqual(self.drive([280, 290, 300], start_t=2.0), [])

    def test_paused_camera_frames_are_ignored(self):
        self.drive([100, 130])
        self.counter.set_paused("street", True)
        self.assertEqual(self.drive([160, 190, 220, 250], start_t=0.4), [])
        self.assertIsNone(self.counter.last_jpeg("street"))
        self.counter.set_paused("street", False)
        self.drive([260, 270], start_t=1.2)
        self.assertIsNotNone(self.counter.last_jpeg("street"))

    def test_coverage_records_watched_time_and_splits_at_outages(self):
        self.drive([None] * 40)  # 0 .. 7.8 s
        self.drive([None] * 10, start_t=30.0)  # outage 7.8 -> 30 s
        self.counter.set_paused("street", True)  # saves the open stretch's end
        stretches = self.db.coverage()
        self.assertEqual(stretches, [(iso(0), iso(7.8)), (iso(30.0), iso(31.8))])

    def test_gap_causes_are_recorded(self):
        errors = {"now": (None, None)}
        self.counter = live_traffic.TrafficCounter(
            self.db, detector=lambda image: list(self.boxes),
            startup_fn=lambda: {"reason": "update", "detail": "Changed: server.py"},
            camera_error_fn=lambda cam: errors["now"],
        )
        self.drive([None] * 5)  # first stretch after start -> the startup reason
        self.drive([None] * 5, start_t=20.0)  # gap, no camera error -> no_pictures
        errors["now"] = ("no answer from the camera", iso(30.0))
        self.drive([None] * 5, start_t=40.0)  # gap with a camera error in it -> camera
        self.counter.set_paused("street", True)
        self.counter.set_paused("street", False)
        self.drive([None] * 5, start_t=90.0)  # after a pause -> paused
        self.assertEqual(self.db.reasons(), [
            ("update", "Changed: server.py"),
            ("no_pictures", ""),
            ("camera", "no answer from the camera"),
            ("paused", ""),
        ])
        start = datetime(2026, 9, 29, 20, 0, tzinfo=timezone.utc)
        report = traffic_store.counts(self.db.conn, "street", start, start + timedelta(hours=1), 15)
        self.assertEqual([g["reason"] for g in report["gaps"]], ["no_pictures", "camera", "paused"])
        self.assertEqual([g["planned"] for g in report["gaps"]], [False, False, True])
        self.assertEqual(report["gaps"][1]["label"], "Camera stopped sending pictures")

    def test_lines_follow_the_zoom_area(self):
        # The picture is only the right half of the camera's view, so the
        # middle line (x = 0.5 of the full view) sits at the zoomed picture's
        # left edge, and a line at x = 0.75 sits in its middle.
        traffic_store.create_line(
            self.db.conn, "street", {"name": "Right", "x1": 0.75, "y1": 0.0, "x2": 0.75, "y2": 1.0},
            live_traffic.default_direction_labels,
        )
        focus = {"street": (0.5, 0.0, 0.5, 1.0)}
        self.counter = live_traffic.TrafficCounter(self.db, detector=lambda image: list(self.boxes),
                                                   focus_fn=lambda cam: focus.get(cam))
        saved = self.drive([100, 130, 160, 190, 220, 250, 280])  # crosses x = 200 of 400
        self.assertEqual([r["line_id"] for r in saved], [self.line["id"] + 1])
        self.assertEqual(self.counter.view("street")["focus"], {"x": 0.5, "y": 0.0, "w": 0.5, "h": 1.0})

    def test_view_change_is_a_planned_gap(self):
        self.drive([None] * 5)
        self.counter.view_changed("street")
        self.drive([None] * 5, start_t=10.0)
        self.assertEqual([r[0] for r in self.db.reasons()], ["restart", "reconfigured"])

    def test_resume_after_restarts_is_a_pause_not_an_update(self):
        # Paused before the program restarted; resumed afterwards.
        counter = live_traffic.TrafficCounter(
            self.db, detector=lambda image: list(self.boxes),
            startup_fn=lambda: {"reason": "update", "detail": "Changed: server.py"},
        )
        counter.set_paused("street", True)
        counter.set_paused("street", False)
        self.counter = counter
        self.drive([None] * 3, start_t=100.0)
        self.assertEqual(self.db.reasons(), [("paused", "")])

    def test_forget_camera(self):
        self.drive([100, 130])
        self.counter.forget_camera("street")
        self.assertIsNone(self.counter.last_jpeg("street"))
        self.assertEqual(self.counter.status("street")["frames_processed"], 0)


class StreamConfigTests(unittest.TestCase):
    BASE = {"type": "rtsp", "rtsp_url": "rtsp://192.168.1.50:554/Preview_01_sub", "username": "admin", "password": "pw"}

    def test_fast_interval_allowed_for_full_decode_stream(self):
        src = live.parse_config({"t": dict(self.BASE, rtsp_decode="all", interval_seconds=0.2, max_width=1280)})["t"]
        self.assertEqual(src.interval_seconds, 0.2)
        command = live.build_rtsp_frame_command(src, src.detection_rtsp_url())
        vf = command[command.index("-vf") + 1]
        self.assertEqual(vf, "fps=5,scale='min(1280,iw)':-2")

    def test_fast_interval_refused_for_snapshots_and_keyframes(self):
        with self.assertRaises(live.LiveConfigError):
            live.parse_config({"t": dict(self.BASE, interval_seconds=0.2)})
        with self.assertRaises(live.LiveConfigError):
            live.parse_config({"r": {"host": "192.168.1.50", "username": "admin", "password": "pw", "interval_seconds": 0.5}})

    def test_enabled_flag(self):
        sources = live.parse_config({"t": dict(self.BASE, enabled=False)})
        self.assertFalse(sources["t"].enabled)
        with self.assertRaises(live.LiveConfigError):
            live.parse_config({"t": dict(self.BASE, enabled="no")})


if __name__ == "__main__":
    unittest.main()
