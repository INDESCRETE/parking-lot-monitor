"""Tests for app/tracking.py (vehicle tracker + counting lines).

Run from the repo root:  python3 -m unittest tests.test_tracking -v
No camera or model needed: detections are made up by hand.
"""

from __future__ import annotations

import unittest

from app.tracking import (
    CountLine,
    LineCounter,
    TrackedDetection,
    VehicleTracker,
    crop_region_for_lines,
    parse_line_spec,
    segments_cross,
)


def det(x, y, w=80, h=40, conf=0.9, cls="car"):
    return TrackedDetection((x, y, x + w, y + h), conf, cls)


def run(frames, lines, fps=5.0, **tracker_kwargs):
    """frames: list of per-frame detection lists. Returns (tracker, counter, crossings)."""
    tracker = VehicleTracker(**tracker_kwargs)
    counter = LineCounter(lines)
    crossings = []
    for i, dets in enumerate(frames):
        now = i / fps
        seen = tracker.update(dets, now)
        crossings += counter.update(seen, now)
        counter.forget_missing(t.track_id for t in tracker.tracks)
    return tracker, counter, crossings


# A vertical line at x=500 from y=0 to y=400. Drawn top-to-bottom, so
# "forward" (left side -> right side, looking from p1 to p2) means moving
# LEFT in the picture... check: looking down the picture, your left is +x.
VERTICAL = CountLine("l1", "Gate", (500, 0), (500, 400), "in", "out")
# Horizontal line drawn left-to-right: forward = moving DOWN the picture.
HORIZONTAL = CountLine("l2", "Road", (0, 300), (1000, 300))


class GeometryTests(unittest.TestCase):
    def test_segments_cross(self):
        self.assertTrue(segments_cross((0, 0), (10, 10), (0, 10), (10, 0)))
        self.assertFalse(segments_cross((0, 0), (1, 1), (5, 5), (6, 0)))
        # passes beyond the end of the line: no crossing
        self.assertFalse(segments_cross((600, -50), (600, 50), (0, 0), (500, 0)))

    def test_direction_convention(self):
        self.assertEqual(HORIZONTAL.direction_of((100, 250), (100, 350)), "forward")  # moving down
        self.assertEqual(HORIZONTAL.direction_of((100, 350), (100, 250)), "reverse")
        self.assertIsNone(HORIZONTAL.direction_of((100, 250), (100, 290)))

    def test_parse_line_spec(self):
        line = parse_line_spec("Main St:0,0.5,1,0.5:south/north", (1000, 600))
        self.assertEqual(line.name, "Main St")
        self.assertEqual(line.p1, (0.0, 300.0))
        self.assertEqual(line.p2, (1000.0, 300.0))
        self.assertEqual(line.label_for("forward"), "south")
        pixels = parse_line_spec("10,20,300,20", (1000, 600), index=3)
        self.assertEqual((pixels.name, pixels.p1), ("Line 3", (10.0, 20.0)))
        with self.assertRaises(ValueError):
            parse_line_spec("1,2,3", (100, 100))

    def test_crop_region(self):
        line = CountLine("x", "x", (900, 500), (1100, 500))
        region = crop_region_for_lines([line], (2560, 1920))
        self.assertEqual(region, (740, 340, 1261, 661))
        full = CountLine("y", "y", (0, 960), (2560, 960))
        self.assertIsNone(crop_region_for_lines([full], (2560, 1920)))


class TrackerTests(unittest.TestCase):
    def test_one_car_keeps_one_id(self):
        frames = [[det(100 + 40 * i, 280)] for i in range(12)]
        tracker, counter, crossings = run(frames, [])
        self.assertEqual({t.track_id for t in tracker.tracks}, {1})

    def test_counts_one_car_once_with_direction(self):
        # moving down across the horizontal line (ground point = bottom of box)
        frames = [[det(400, 150 + 30 * i)] for i in range(10)]
        _, counter, crossings = run(frames, [HORIZONTAL])
        self.assertEqual(len(crossings), 1)
        self.assertEqual(crossings[0].direction, "forward")
        self.assertEqual(counter.totals, {("l2", "forward"): 1})

    def test_two_cars_opposite_directions(self):
        frames = [
            [det(100 + 50 * i, 100, w=90), det(900 - 50 * i, 250, w=90)]
            for i in range(16)
        ]
        _, counter, crossings = run(frames, [VERTICAL])
        self.assertEqual(len(crossings), 2)
        self.assertEqual(sorted(c.direction for c in crossings), ["forward", "reverse"])
        # moving right (+x) is "reverse" for a line drawn downward -> "out"
        right_mover = [c for c in crossings if c.track_id == 1][0]
        self.assertEqual(right_mover.direction_label, "out")

    def test_car_sitting_on_line_counted_once(self):
        # drives up to the line, jitters back and forth over it, then leaves
        ys = [200, 230, 255, 262, 258, 263, 257, 262, 290, 320, 350]
        frames = [[det(400, y)] for y in ys]  # bottom = y+40, line at 300
        _, counter, crossings = run(frames, [HORIZONTAL])
        self.assertEqual(len(crossings), 1)

    def test_single_frame_false_detection_not_counted(self):
        frames = [[], [det(400, 255)], [], [], [det(400, 265)], [], [], [], [], []]
        _, _, crossings = run(frames, [HORIZONTAL])
        self.assertEqual(crossings, [])

    def test_missed_frames_keep_same_track(self):
        # detector misses the car for 3 frames right at the line
        frames = []
        for i in range(14):
            frames.append([] if 5 <= i <= 7 else [det(400, 120 + 25 * i)])
        tracker, _, crossings = run(frames, [HORIZONTAL])
        self.assertEqual(len(crossings), 1)
        self.assertEqual(crossings[0].track_id, 1)

    def test_weak_detection_keeps_track_alive(self):
        frames = [[det(100 + 40 * i, 280, conf=0.9 if i < 4 or i > 8 else 0.3)] for i in range(14)]
        tracker, _, _ = run(frames, [], max_lost_seconds=0.3)
        ids = {t.track_id for t in tracker.tracks}
        self.assertEqual(ids, {1})

    def test_weak_detection_cannot_start_track(self):
        frames = [[det(100 + 40 * i, 280, conf=0.3)] for i in range(10)]
        tracker, _, _ = run(frames, [])
        self.assertEqual(tracker.tracks, [])

    def test_fast_car_low_fps(self):
        # moves 1.5x its own width per frame: boxes never overlap
        frames = [[det(50 + 120 * i, 100)] for i in range(8)]
        tracker, _, crossings = run(frames, [VERTICAL], fps=2.0)
        self.assertEqual(len(crossings), 1)
        self.assertEqual({t.track_id for t in tracker.tracks}, {1})

    def test_side_by_side_cars_stay_separate(self):
        # two cars in adjacent lanes moving together
        frames = [[det(100 + 40 * i, 100), det(100 + 40 * i, 150)] for i in range(15)]
        tracker, _, crossings = run(frames, [VERTICAL])
        self.assertEqual(len(crossings), 2)
        self.assertEqual(len({c.track_id for c in crossings}), 2)

    def test_vehicle_class_vote(self):
        frames = [[det(100 + 40 * i, 100, w=160, h=70, cls="truck" if i % 3 else "car")] for i in range(15)]
        _, _, crossings = run(frames, [VERTICAL])
        self.assertEqual(crossings[0].vehicle_class, "truck")

    def test_duplicate_box_does_not_start_second_track(self):
        frames = [[det(100 + 40 * i, 100), det(102 + 40 * i, 101, conf=0.6)] for i in range(12)]
        tracker, _, crossings = run(frames, [VERTICAL])
        self.assertEqual(len(crossings), 1)


class FollowingCarsTest(unittest.TestCase):
    def test_close_following_cars_counted_separately(self):
        # two cars in one lane, 1.5 car lengths apart
        frames = [[det(100 + 40 * i, 100), det(-20 + 40 * i, 100)] for i in range(20)]
        _, _, crossings = run(frames, [VERTICAL])
        self.assertEqual(len(crossings), 2)



def _ffmpeg_ok() -> bool:
    import shutil
    return bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))


def _bright_box_detector(bands):
    """Stand-in for the model: finds white rectangles inside each horizontal band."""
    import numpy as np
    from app.detection_core import Detection

    def detect(image, region):
        pixels = np.asarray(image.convert("L"))
        found = []
        for top, bottom in bands:
            cols = (pixels[top:bottom] > 200).any(axis=0)
            rows = (pixels[top:bottom] > 200).any(axis=1)
            if not cols.any():
                continue
            xs = np.flatnonzero(cols)
            # split into separate runs of bright columns
            splits = np.flatnonzero(np.diff(xs) > 1) + 1
            ys = np.flatnonzero(rows)
            for run in np.split(xs, splits):
                if len(run) < 10:
                    continue
                found.append(Detection(3, "car", 0.9, (float(run[0]), float(top + ys[0]), float(run[-1] + 1), float(top + ys[-1] + 1))))
        return found

    return detect


@unittest.skipUnless(_ffmpeg_ok(), "needs ffmpeg")
class VideoEndToEndTest(unittest.TestCase):
    """Runs scripts/count_traffic.py on a generated video of white boxes
    driving across the picture, with a fake detector instead of the model."""

    def test_counts_generated_video(self):
        import csv
        import subprocess
        import tempfile
        from pathlib import Path
        from unittest import mock

        from scripts import count_traffic

        with tempfile.TemporaryDirectory() as tmp:
            video = Path(tmp) / "road.mp4"
            # Lane 1 (y 200-260) drives right at 200 px/s; lane 2 (y 350-410) drives left at 150 px/s.
            # overlay (not drawbox) so it also works on older ffmpeg, where
            # drawbox can't move over time.
            graph = (
                "color=c=0x303030:s=1280x720:r=30:d=30[bg];"
                "color=c=white:s=120x60:r=30:d=30[a];color=c=white:s=120x60:r=30:d=30[b];"
                "[bg][a]overlay=x='mod(t*200,1400)-100':y=200[ab];"
                "[ab][b]overlay=x='1300-mod(t*150,1400)':y=350"
            )
            subprocess.run(
                ["ffmpeg", "-v", "error", "-y", "-filter_complex", graph, "-t", "30",
                 "-pix_fmt", "yuv420p", str(video)],
                check=True,
            )
            out = Path(tmp) / "run"
            fake = _bright_box_detector([(150, 280), (300, 450)])
            with mock.patch.object(count_traffic, "make_rfdetr_detector", lambda *a, **k: fake):
                code = count_traffic.main([
                    str(video), "--line", "Gate:0.5,0.1,0.5,0.7:westbound/eastbound",
                    "--start", "2026-09-27 08:00", "--bin-minutes", "0.25", "--out", str(out),
                ])
            self.assertEqual(code, 0)
            with open(out / "crossings.csv") as f:
                crossings = list(csv.DictReader(f))
            directions = sorted(c["direction"] for c in crossings)
            # lane 1 center passes x=640 at t=3.4, 10.4, 17.4, 24.4; lane 2 at t=4.8, 14.1, 23.5
            self.assertEqual(directions, ["eastbound"] * 4 + ["westbound"] * 3, crossings)
            times = sorted(float(c["seconds_into_video"]) for c in crossings)
            for got, want in zip(times, [3.4, 4.8, 10.4, 14.1, 17.4, 23.5, 24.4]):
                self.assertAlmostEqual(got, want, delta=0.5)
            with open(out / "counts.csv") as f:
                rows = list(csv.DictReader(f))
            self.assertEqual(sum(int(r["total"]) for r in rows), 7)
            self.assertEqual(rows[0]["period"], "08:00:00-08:00:15")  # 15-second blocks
            self.assertTrue((out / "preview.mp4").stat().st_size > 1000)
            self.assertIn("Gate: 7 vehicles", (out / "summary.txt").read_text())


if __name__ == "__main__":
    unittest.main()
