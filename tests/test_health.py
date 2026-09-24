"""Tests for app/health.py: each failure it's meant to catch, simulated.

Run from the repo root:  python3 -m unittest tests.test_health -v
Stdlib only; no camera, internet or model needed.
"""

from __future__ import annotations

import json
import sqlite3
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app import health


class FakeClock:
    def __init__(self):
        self.now = datetime(2026, 9, 24, 18, 0, tzinfo=timezone.utc)

    def __call__(self):
        return self.now

    def advance(self, **kw):
        self.now += timedelta(**kw)


class Harness:
    """A HealthMonitor wired to fake cameras, a fake clock and recording senders."""

    def __init__(self, tmp: Path, clock: FakeClock, config: dict | None = None, online=True, fail_send=False):
        self.tmp = tmp
        self.clock = clock
        self.sent: list = []  # every alert except the one-time first-run hello
        self.hello: list = []
        self.fail_send = fail_send
        self.online = online
        self.cameras: list = []
        self.config_path = tmp / "alerts.json"
        if config is not None:
            self.config_path.write_text(json.dumps(config))
        self.db_path = tmp / "db.sqlite"
        if not self.db_path.exists():
            conn = sqlite3.connect(self.db_path)
            conn.execute("CREATE TABLE t (x)")
            conn.execute("INSERT INTO t VALUES (1)")
            conn.commit()
            conn.close()

        def push(alert):
            if self.fail_send:
                raise OSError("network unreachable")
            record = self.hello if alert["title"].endswith("Alerts are on") else self.sent
            record.append((alert["title"], self.monitor.notifier._body(alert)))

        self.monitor = health.HealthMonitor(
            self.db_path,
            camera_statuses=lambda: self.cameras,
            config_path=self.config_path,
            health_dir=tmp / "health",
            backup_dir=tmp / "backups",
            senders={"push": push},
            internet_check=lambda: self.online,
            clock=clock,
        )
        self.monitor.notifier.flush  # noqa -- delivery is driven by hand below

    def start(self):
        # Don't launch threads in tests; do what start() does, then drive tick() by hand.
        m = self.monitor
        m.health_dir.mkdir(parents=True, exist_ok=True)
        prev = health._read_json(m.state_path)
        m._state = prev if isinstance(prev, dict) else {}
        m._report_previous_downtime(prev if isinstance(prev, dict) else None)
        m._state.update(started_at=m.started_at.isoformat(), clean_shutdown=False)
        m._save_state()

    def tick(self):
        self.monitor._last_heartbeat = 0.0
        self.monitor.tick()
        self.monitor.notifier.flush()

    def titles(self):
        """Titles sent so far, leaving out the one-time first-run hello."""
        return [t for t, _ in self.sent]

    def camera(self, last_frame_ago_s=None, started_ago_s=600, detect_ago_s=None, error=None):
        now = self.clock()
        grabber = {
            "configured": True,
            "started_at": (now - timedelta(seconds=started_ago_s)).isoformat(),
            "last_frame_at": None if last_frame_ago_s is None else (now - timedelta(seconds=last_frame_ago_s)).isoformat(),
            "last_error": error,
        }
        detection = {
            "last_frame_at": None if detect_ago_s is None else (now - timedelta(seconds=detect_ago_s)).isoformat(),
            "last_error": None,
        }
        self.cameras = [{"camera_id": "reolink_live", "name": "Front lot", "grabber": grabber, "detection": detection}]


BASE_CONFIG = {"site_name": "Test Lot", "ntfy_topic": "parking-test", "daily_checkin_hour": None, "backup_hour": 23}


class HealthTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.clock = FakeClock()
        self._boot = health.system_boot_time
        health.system_boot_time = lambda: None

    def tearDown(self):
        health.system_boot_time = self._boot
        self._tmp.cleanup()

    def restart(self, h: Harness, **kw) -> Harness:
        return Harness(self.tmp, self.clock, **kw)

    # -- startup / outages --------------------------------------------------
    def test_first_run_creates_config_with_private_topic_and_says_hello(self):
        h = Harness(self.tmp, self.clock)
        cfg = json.loads(h.config_path.read_text())
        self.assertRegex(cfg["ntfy_topic"], r"^parking-[0-9a-f]{12}$")
        h.start(); h.tick()
        self.assertEqual([t for t, _ in h.hello], ["Parking lot monitor: Alerts are on"])

    def test_quick_clean_restart_is_silent(self):
        h = Harness(self.tmp, self.clock, BASE_CONFIG); h.start(); h.tick()
        h.monitor.mark_clean_shutdown()
        self.clock.advance(seconds=20)
        h2 = self.restart(h); h2.start(); h2.tick()
        self.assertEqual(h2.titles(), [])

    def test_crash_reports_downtime_window(self):
        h = Harness(self.tmp, self.clock, BASE_CONFIG); h.start(); h.tick()
        self.clock.advance(minutes=12)  # killed without a clean shutdown
        h2 = self.restart(h); h2.start(); h2.tick()
        self.assertEqual(h2.titles(), ["Test Lot: Back online"])
        body = h2.sent[0][1]
        self.assertIn("(12 min)", body)
        self.assertIn("stopped unexpectedly", body)

    def test_power_outage_detected_from_reboot(self):
        h = Harness(self.tmp, self.clock, BASE_CONFIG); h.start(); h.tick()
        lost_power = self.clock()
        self.clock.advance(hours=2, minutes=5)
        health.system_boot_time = lambda: lost_power + timedelta(hours=2)
        h2 = self.restart(h); h2.start(); h2.tick()
        self.assertIn("power outage", h2.sent[0][1])
        self.assertIn("(2 h 5 min)", h2.sent[0][1])

    def test_long_clean_stop_still_reported(self):
        h = Harness(self.tmp, self.clock, BASE_CONFIG); h.start(); h.tick()
        h.monitor.mark_clean_shutdown()
        self.clock.advance(minutes=30)
        h2 = self.restart(h); h2.start(); h2.tick()
        self.assertIn("was stopped, then started again", h2.sent[0][1])

    def test_sleep_detected(self):
        h = Harness(self.tmp, self.clock, BASE_CONFIG); h.start(); h.tick()
        h.monitor._last_tick_wall = time.time() - 900
        h.tick()
        self.assertIn("Test Lot: Monitoring paused", h.titles())

    # -- cameras ----------------------------------------------------------------
    def test_camera_down_alerts_once_then_recovers(self):
        h = Harness(self.tmp, self.clock, BASE_CONFIG); h.start()
        h.monitor.started_at = self.clock() - timedelta(hours=1)
        h.camera(last_frame_ago_s=20, detect_ago_s=20); h.tick()
        self.assertEqual(h.titles(), [])
        h.camera(last_frame_ago_s=6 * 60, detect_ago_s=6 * 60, error="no answer from 192.168.139.18")
        h.tick(); h.tick(); h.tick()
        self.assertEqual(h.titles(), ["Test Lot: Camera 'Front lot' is down"])
        self.assertIn("no answer from 192.168.139.18", h.sent[0][1])
        self.clock.advance(minutes=20)
        h.camera(last_frame_ago_s=3, detect_ago_s=3); h.tick()
        self.assertEqual(h.titles()[-1], "Test Lot: Camera 'Front lot' is back")
        self.assertIn("(26 min)", h.sent[-1][1])
        h.tick()
        self.assertEqual(len(h.sent), 2)

    def test_camera_that_never_connects_is_reported(self):
        h = Harness(self.tmp, self.clock, BASE_CONFIG); h.start()
        h.monitor.started_at = self.clock() - timedelta(minutes=10)
        h.camera(last_frame_ago_s=None, started_ago_s=600, error="wrong password"); h.tick()
        self.assertEqual(h.titles(), ["Test Lot: Camera 'Front lot' is down"])

    def test_camera_not_blamed_for_our_own_downtime(self):
        h = Harness(self.tmp, self.clock, BASE_CONFIG); h.start()
        # Server just started; the grabber's last frame is from before we were down.
        h.camera(last_frame_ago_s=3600, started_ago_s=5); h.tick()
        self.assertEqual([t for t in h.titles() if "Camera" in t], [])

    def test_detection_stall(self):
        h = Harness(self.tmp, self.clock, BASE_CONFIG); h.start()
        h.monitor.started_at = self.clock() - timedelta(hours=1)
        h.camera(last_frame_ago_s=2, detect_ago_s=15 * 60); h.tick()
        self.assertEqual(h.titles(), ["Test Lot: Car detection stopped for 'Front lot'"])
        h.camera(last_frame_ago_s=2, detect_ago_s=2); h.tick()
        self.assertEqual(h.titles()[-1], "Test Lot: Car detection working again for 'Front lot'")

    # -- internet / outbox ----------------------------------------------------------
    def test_alerts_queue_while_offline_and_survive_restart(self):
        h = Harness(self.tmp, self.clock, BASE_CONFIG, online=False, fail_send=True); h.start()
        h.monitor.started_at = self.clock() - timedelta(hours=1)
        h.camera(last_frame_ago_s=6 * 60); h.tick()
        self.assertEqual(h.sent, [])
        pending = [a for a in h.monitor.notifier.pending() if a["kind"] != "started"]
        self.assertEqual(len(pending), 1)
        # Power blip while offline: the queued alert must survive on disk.
        self.clock.advance(minutes=30)
        h2 = self.restart(h, online=True, fail_send=False)
        self.assertEqual(len(h2.monitor.notifier.pending()), 2)  # the hello + the camera alert
        h2.start(); h2.tick()
        self.assertIn("Test Lot: Camera 'Front lot' is down", h2.titles())
        late = dict(h2.sent)["Test Lot: Camera 'Front lot' is down"]
        self.assertIn("Sent late", late)
        self.assertEqual(h2.monitor.notifier.pending(), [])

    def test_internet_outage_reported_when_back(self):
        h = Harness(self.tmp, self.clock, BASE_CONFIG); h.start(); h.tick()
        h.online = False; h.fail_send = True
        h.tick()
        self.clock.advance(minutes=45); h.tick()
        self.assertTrue(h.monitor.internet_online is False)
        h.online = True; h.fail_send = False
        self.clock.advance(minutes=1); h.tick()
        self.assertIn("Test Lot: Internet back", h.titles())
        self.assertIn("(46 min)", dict(h.sent)["Test Lot: Internet back"])

    def test_brief_internet_blip_is_silent(self):
        h = Harness(self.tmp, self.clock, BASE_CONFIG); h.start(); h.tick()
        h.online = False; h.tick()
        self.clock.advance(seconds=60); h.online = True; h.tick()
        self.assertNotIn("Test Lot: Internet back", h.titles())

    # -- disk / backups / check-in / config -------------------------------------------
    def test_disk_warning(self):
        h = Harness(self.tmp, self.clock, BASE_CONFIG); h.start()
        h.monitor.disk_usage = lambda: {"percent_used": 91.0, "free_gb": 4.0, "database_mb": 120.0}
        h.tick(); h.tick()
        self.assertEqual(h.titles(), ["Test Lot: Disk almost full"])
        h.monitor.disk_usage = lambda: {"percent_used": 60.0, "free_gb": 40.0, "database_mb": 120.0}
        h.tick()
        self.assertEqual(h.titles()[-1], "Test Lot: Disk space OK")

    def test_nightly_backup_is_valid_and_pruned(self):
        cfg = dict(BASE_CONFIG, backup_hour=0, backups_to_keep=3)
        h = Harness(self.tmp, self.clock, cfg); h.start()
        for _ in range(5):
            h.tick(); h.tick()  # second tick the same day must not make another copy
            self.clock.advance(days=1)
        backups = h.monitor.backups()
        self.assertEqual(len(backups), 3)
        conn = sqlite3.connect(backups[-1])
        self.assertEqual(conn.execute("SELECT x FROM t").fetchone()[0], 1)
        conn.close()

    def test_daily_checkin_once_per_day(self):
        cfg = dict(BASE_CONFIG, daily_checkin_hour=0)
        h = Harness(self.tmp, self.clock, cfg); h.start()
        h.tick(); h.tick()
        self.assertEqual(h.titles().count("Test Lot: Daily check-in"), 1)
        self.assertIn("Everything is working.", dict(h.sent)["Test Lot: Daily check-in"])
        self.clock.advance(days=1); h.tick()
        self.assertEqual(h.titles().count("Test Lot: Daily check-in"), 2)

    def test_broken_config_is_reported_not_overwritten(self):
        self.tmp.joinpath("alerts.json").write_text("{ not json")
        h = Harness(self.tmp, self.clock)
        self.assertIn("not valid JSON", h.monitor.config_error)
        self.assertEqual(h.config_path.read_text(), "{ not json")

    def test_a_crashing_check_does_not_stop_the_others(self):
        h = Harness(self.tmp, self.clock, BASE_CONFIG); h.start()

        def boom():
            raise RuntimeError("camera list exploded")

        h.monitor.camera_statuses = boom
        h.monitor.disk_usage = lambda: {"percent_used": 95.0, "free_gb": 1.0, "database_mb": 1.0}
        h.tick()
        self.assertIn("Test Lot: Disk almost full", h.titles())
        self.assertIn("camera list exploded", h.monitor.last_check_error)


if __name__ == "__main__":
    unittest.main()
