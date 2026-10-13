import json
import os
import random
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from auto_location.accounts import add_account, load_accounts, update_account
from auto_location.batch import run_batch
from auto_location.checkin import CheckinError
from auto_location.cli import write_json, parser


class AccountBatchTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.accounts = self.root / "accounts.json"
        self.output = self.root / "result.json"

    def tearDown(self):
        self.temp.cleanup()

    def args(self, command="batch-checkin"):
        return parser().parse_args([command, "--accounts", str(self.accounts), "--simulate-miniprogram"])

    def test_registration_isolates_profiles_and_rejects_reuse_and_secrets(self):
        add_account(self.accounts, "a")
        add_account(self.accounts, "b")
        accounts = load_accounts(self.accounts)
        self.assertNotEqual(accounts[0]["profile"], accounts[1]["profile"])
        for ident, profile in (("A", None), ("c", accounts[0]["profile"]),
                               ("d", accounts[0]["profile"] / "nested"), ("../bad", None), ("CON", None)):
            with self.assertRaises(CheckinError):
                add_account(self.accounts, ident, profile)
        data = json.loads(self.accounts.read_text(encoding="utf-8"))
        data["accounts"][0]["password"] = "fixture"
        write_json(self.accounts, data)
        with self.assertRaises(CheckinError):
            load_accounts(self.accounts)

    def test_random_order_failure_continuation_and_environment_isolation(self):
        for ident in ("a", "b", "c", "disabled"):
            add_account(self.accounts, ident, area_index=1)
        data = json.loads(self.accounts.read_text(encoding="utf-8"))
        data["accounts"][-1]["enabled"] = False
        write_json(self.accounts, data)
        calls = []
        def runner(argv, env, check):
            ident = argv[argv.index("--account-id") + 1]
            calls.append(ident)
            self.assertNotIn("UESTC_USERNAME", env)
            self.assertNotIn("UESTC_PASSWORD", env)
            self.assertIn("--auto-login", argv)
            self.assertIn("--auto-location", argv)
            self.assertIn("--simulate-miniprogram", argv)
            self.assertEqual(argv[argv.index("--area-index") + 1], "1")
            self.assertEqual(json.loads(self.output.read_text(encoding="utf-8"))["results"][-1]["status"], "running")
            failed = ident == "b"
            write_json(Path(argv[argv.index("--output") + 1]),
                       {"status": "failed" if failed else "verified", "record": {"recordStatusCode": "CHECKED_IN"}})
            return SimpleNamespace(returncode=int(failed))
        with patch.dict(os.environ, {"UESTC_USERNAME": "fixture", "UESTC_PASSWORD": "fixture"}):
            report = run_batch(self.args(), self.output, write_json, runner, random.Random(1))
        self.assertEqual(calls, ["b", "c", "a"])
        self.assertEqual(report["order"], calls)
        self.assertEqual(report["status"], "partial_failure")
        self.assertEqual(report["success_count"], 2)
        self.assertEqual(report["failed_count"], 1)
        self.assertEqual(report["unprocessed"], [])
        self.assertEqual(len({e["output"] for e in report["results"]}), 3)
        self.assertFalse(self.accounts.with_suffix(".batch.lock").exists())

    def test_missing_evidence_is_not_success_and_cancellation_stops(self):
        add_account(self.accounts, "a")
        report = run_batch(self.args(), self.output, write_json,
                           lambda *a, **kw: SimpleNamespace(returncode=0), random.Random(1))
        self.assertEqual(report["failed_count"], 1)
        add_account(self.accounts, "b")
        def cancel(*args, **kwargs):
            raise KeyboardInterrupt()
        report = run_batch(self.args(), self.output, write_json, cancel, random.Random(1))
        self.assertEqual(report["status"], "cancelled")
        self.assertEqual(report["results"][0]["status"], "unknown")
        self.assertEqual(len(report["unprocessed"]), 1)
        self.assertFalse(self.accounts.with_suffix(".batch.lock").exists())

    def test_existing_lock_does_not_run_or_overwrite_report(self):
        add_account(self.accounts, "a")
        self.accounts.with_suffix(".batch.lock").write_text("fixture", encoding="utf-8")
        write_json(self.output, {"status": "running"})
        with self.assertRaises(CheckinError):
            run_batch(self.args(), self.output, write_json, lambda *a, **kw: self.fail("unexpected execution"))
        self.assertEqual(json.loads(self.output.read_text(encoding="utf-8"))["status"], "running")

    def test_auto_login_disabled_reuses_session_and_never_starts_manual_batch_login(self):
        add_account(self.accounts, "a")
        update_account(self.accounts, "a", True, False, None)
        calls = []
        def runner(argv, **kw):
            calls.append(argv)
            self.assertNotIn("--auto-login", argv)
            write_json(Path(argv[argv.index('--output') + 1]), {"status": "verified"})
            return SimpleNamespace(returncode=0)
        result = run_batch(self.args(), self.output, write_json, runner)
        self.assertEqual(result['status'], 'completed')
        args = parser().parse_args(['batch-login', '--accounts', str(self.accounts)])
        result = run_batch(args, self.output, write_json, runner)
        self.assertEqual(len(calls), 1)
        self.assertEqual(result['failed_count'], 1)
