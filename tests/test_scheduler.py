import json
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import Mock

from auto_location.scheduler import DailyScheduler, ZONE, validate


class SchedulerTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.path = Path(self.temp.name) / "schedule.json"
        self.now = datetime(2026, 10, 13, 20, 0, tzinfo=ZONE)
        self.code = None
        self.calls = 0
        self.busy = False
        self.fail = False
        self.process = SimpleNamespace(poll=lambda: self.code)
        self.controller = SimpleNamespace(start_scheduled=self.launch)
        self.rng = Mock()
        self.rng.randrange.return_value = 60
        self.scheduler = self.new()

    def new(self):
        return DailyScheduler(self.path, self.controller, clock=lambda: self.now, rng=self.rng)

    def launch(self, claim):
        if self.busy:
            raise RuntimeError("busy")
        claim()
        # The durable claim must precede process creation.
        record = json.loads(self.path.read_text())["days"][self.now.date().isoformat()]
        self.assertEqual(record["status"], "claimed")
        self.calls += 1
        if self.fail:
            raise RuntimeError("launch failed")
        return self.process

    def enable(self):
        self.scheduler.configure({"enabled": True, "start": "21:00", "end": "23:00"})

    def today(self):
        return self.scheduler.snapshot()["today"]

    def tearDown(self):
        self.scheduler.close()
        self.temp.cleanup()

    def test_disabled_and_invalid_config(self):
        self.scheduler.tick()
        self.assertEqual(self.calls, 0)
        for value in ({"enabled": 1, "start": "21:00", "end": "23:00"},
                      {"enabled": True, "start": "23:00", "end": "01:00"},
                      {"enabled": True, "start": "24:00", "end": "25:00"},
                      {"enabled": True, "start": "21:00", "end": "21:00"}):
            with self.assertRaises(ValueError):
                validate(value)

    def test_persistent_plan_and_once_even_after_config_change(self):
        self.enable()
        plan = self.today()["planned_at"]
        self.scheduler = self.new()
        self.assertEqual(self.today()["planned_at"], plan)
        self.assertEqual(self.rng.randrange.call_count, 1)
        self.now = datetime.fromisoformat(plan)
        self.scheduler.tick()
        self.assertEqual(self.calls, 1)
        self.code = 0
        self.scheduler.tick()
        self.assertEqual(self.today()["status"], "completed")
        self.scheduler.configure({"enabled": True, "start": "21:00", "end": "23:30"})
        self.scheduler = self.new()
        self.scheduler.tick()
        self.assertEqual(self.calls, 1)

    def test_busy_deferred_and_window_expires(self):
        self.enable()
        self.now = datetime.fromisoformat(self.today()["planned_at"])
        self.busy = True
        self.scheduler.tick()
        self.assertEqual(self.today()["status"], "waiting")
        self.now = self.now.replace(hour=23, minute=0)
        self.busy = False
        self.scheduler.tick()
        self.assertEqual(self.today()["status"], "missed")
        self.assertEqual(self.calls, 0)
        self.assertTrue(self.scheduler.snapshot()["next_run"].startswith("2026-10-14"))

    def test_busy_then_available_and_exit_failure_not_retried(self):
        self.enable()
        self.now = datetime.fromisoformat(self.today()["planned_at"])
        self.busy = True
        self.scheduler.tick()
        self.busy = False
        self.scheduler.tick()
        self.code = 1
        self.scheduler.tick()
        self.scheduler.tick()
        self.assertEqual(self.calls, 1)
        self.assertEqual(self.today()["status"], "failed")

    def test_restart_running_is_interrupted(self):
        self.enable()
        self.now = datetime.fromisoformat(self.today()["planned_at"])
        self.scheduler.tick()
        self.scheduler = self.new()
        self.scheduler.tick()
        self.assertEqual(self.today()["status"], "interrupted")
        self.assertEqual(self.calls, 1)

    def test_restart_catchup_and_disable_cancels(self):
        self.enable()
        self.now = self.now.replace(hour=22)
        self.scheduler = self.new()
        self.scheduler.tick()
        self.assertEqual(self.calls, 1)
        self.scheduler.configure({"enabled": False, "start": "21:00", "end": "23:00"})
        self.code = 0
        self.scheduler.tick()
        self.assertEqual(self.today()["status"], "completed")
        self.now += timedelta(days=1)
        self.scheduler.tick()
        self.assertEqual(self.calls, 1)

    def test_bounds_and_shanghai_day(self):
        self.rng.randrange.return_value = 7199
        self.enable()
        self.assertIn("22:59:59+08:00", self.today()["planned_at"])
        self.now = self.now.astimezone(__import__("datetime").timezone.utc)
        self.assertEqual(self.today()["date"], "2026-10-13")

    def test_corrupt_state_not_reset(self):
        self.path.write_text("invalid")
        with self.assertRaises(ValueError):
            self.new()

    def test_launch_failure_not_retried(self):
        self.enable()
        self.fail = True
        self.now = datetime.fromisoformat(self.today()["planned_at"])
        self.scheduler.tick()
        self.scheduler.tick()
        self.assertEqual(self.today()["status"], "failed")
        self.assertEqual(self.calls, 1)

    def test_daemon_wakes_without_manual_request(self):
        import threading
        event = threading.Event()
        original = self.controller.start_scheduled
        def launched(claim):
            process = original(claim)
            event.set()
            return process
        self.controller.start_scheduled = launched
        self.enable()
        self.now = datetime.fromisoformat(self.today()["planned_at"])
        self.scheduler.start()
        self.assertTrue(event.wait(3))
        self.scheduler.close()
        self.assertEqual(self.calls, 1)

    def test_persist_failure_prevents_launch(self):
        from unittest.mock import patch
        self.enable()
        self.now = datetime.fromisoformat(self.today()["planned_at"])
        with patch.object(self.scheduler, "_save", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.scheduler.tick()
        self.assertEqual(self.calls, 0)

    @unittest.skipIf(__import__("os").name == "nt", "Linux Docker leader lock")
    def test_second_daemon_rejected(self):
        self.scheduler.start()
        other = self.new()
        try:
            with self.assertRaises(BlockingIOError):
                other.start()
        finally:
            other.close()
