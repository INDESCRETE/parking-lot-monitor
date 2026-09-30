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

    def open_traffic_coverage(self, camera_id, start):
        return traffic_store.open_coverage(self.conn, camera_id, start)

    def extend_traffic_coverage(self, coverage_id, end):
        traffic_store.extend_coverage(self.conn, coverage_id, end)

    def coverage(self):
        return [tuple(r) for r in self.conn.execute("SELECT start_at, end_at FROM traffic_coverage ORDER BY id")]


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

    def test_bad_bin_size(self):
        now = datetime.now(timezone.utc)
        with self.assertRaises(ValueError):
            traffic_store.counts(self.conn, "street", now - timedelta(hours=1), now, 7)


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
