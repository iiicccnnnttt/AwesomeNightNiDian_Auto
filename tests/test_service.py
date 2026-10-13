import json
import os
import io
from contextlib import redirect_stdout
import http.client
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
from types import SimpleNamespace
from unittest.mock import patch
from urllib.request import Request, urlopen
from urllib.error import HTTPError

from auto_location.accounts import add_account, load_accounts
from auto_location.checkin import CheckinError
from auto_location.service import JobController, handler_for
from auto_location.vault import CredentialVault


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.path = Path(self.temp.name) / "accounts.json"
        add_account(self.path, "fixture")
        key = Path(self.temp.name) / 'secrets' / 'vault.key'
        key.parent.mkdir()
        key.write_bytes(os.urandom(32))
        self.controller = JobController(self.path, vault=CredentialVault(Path(self.temp.name) / 'credentials', key))
        self.code = None
        self.process = SimpleNamespace(poll=lambda: self.code, stdin=io.BytesIO())

    def tearDown(self):
        self.code = 0
        self.controller.close()
        self.temp.cleanup()

    def test_runs_whitelisted_headless_batch_once_and_preserves_report(self):
        self.controller.output.write_text('{"status":"completed"}', encoding="utf-8")
        with patch("auto_location.service.subprocess.Popen", return_value=self.process) as start:
            self.controller.start("checkin")
            argv = start.call_args.args[0]
            self.assertIn("batch-checkin", argv)
            self.assertIn("--headless", argv)
            self.assertIn("--simulate-miniprogram", argv)
            self.assertFalse(self.controller.output.exists())
            with self.assertRaises(RuntimeError):
                self.controller.start("inspect")
            self.assertEqual(start.call_count, 1)
            self.assertTrue(self.controller.status()["running"])
            self.controller.output.write_text('{"status":"partial_failure"}', encoding="utf-8")
            self.code = 1
            status = self.controller.status()
            self.assertFalse(status["running"])
            self.assertEqual(status["exit_code"], 1)
            self.assertEqual(status["report"]["status"], "partial_failure")

    def test_lock_and_empty_accounts_do_not_start_process(self):
        lock = self.path.with_suffix(".batch.lock")
        lock.write_text("fixture", encoding="utf-8")
        with patch("auto_location.service.subprocess.Popen") as start:
            with self.assertRaises(RuntimeError):
                self.controller.start("checkin")
            lock.unlink()
            self.path.unlink()
            with self.assertRaises(ValueError):
                self.controller.start("checkin")
            start.assert_not_called()

    def test_delete_account_cleans_credentials_and_managed_profile(self):
        profile = load_accounts(self.path)[0]['profile']
        profile.mkdir(parents=True)
        (profile / 'cookies.sqlite').write_text('fixture-session')
        self.controller.vault.save('fixture', 'fixture-user', 'fixture-password')
        add_account(self.path, 'keep')
        retained = load_accounts(self.path)[1]['profile']
        retained.mkdir(parents=True)
        (retained / 'keep.txt').write_text('preserved')
        result = self.controller.delete_account({'id': 'FIXTURE'})
        self.assertEqual(result['status'], 'account_deleted')
        self.assertFalse(result['cleanup_pending'])
        self.assertFalse(result['profile_preserved'])
        self.assertEqual([a['id'] for a in load_accounts(self.path)], ['keep'])
        self.assertFalse(profile.exists())
        self.assertFalse(self.controller.vault.configured('fixture'))
        self.assertTrue((retained / 'keep.txt').exists())
        self.assertFalse(self.path.with_suffix('.batch.lock').exists())
        add_account(self.path, 'fixture')
        self.assertFalse(profile.exists())
        self.assertFalse(self.controller.vault.configured('fixture'))

    def test_delete_preserves_external_profile_and_rolls_back_on_failure(self):
        profile = load_accounts(self.path)[0]['profile']
        profile.mkdir(parents=True)
        (profile / 'cookies.sqlite').write_text('fixture-session')
        self.controller.vault.save('fixture', 'fixture-user', 'fixture-password')
        with patch('auto_location.service.remove_account', side_effect=OSError('fixture failure')):
            with self.assertRaises(OSError):
                self.controller.delete_account({'id': 'fixture'})
        self.assertEqual(len(load_accounts(self.path)), 1)
        self.assertTrue(profile.exists())
        self.assertTrue(self.controller.vault.configured('fixture'))
        self.assertFalse(self.path.with_suffix('.batch.lock').exists())
        data = json.loads(self.path.read_text(encoding='utf-8'))
        external = Path(self.temp.name) / 'external-profile'
        external.mkdir()
        (external / 'cookies.sqlite').write_text('external-session')
        data['accounts'][0]['profile'] = str(external)
        self.path.write_text(json.dumps(data), encoding='utf-8')
        result = self.controller.delete_account({'id': 'fixture'})
        self.assertTrue(result['profile_preserved'])
        self.assertTrue((external / 'cookies.sqlite').exists())
        self.assertFalse(self.controller.vault.configured('fixture'))

    def test_delete_rejects_busy_invalid_and_missing_accounts(self):
        self.path.with_suffix('.batch.lock').write_text('fixture')
        with self.assertRaises(RuntimeError):
            self.controller.delete_account({'id': 'fixture'})
        self.path.with_suffix('.batch.lock').unlink()
        for value in ({'id': '../escape'}, {'id': 'missing'}, {'id': 'fixture', 'profile': '/'}):
            with self.assertRaises((CheckinError, ValueError)):
                self.controller.delete_account(value)
        self.controller.process = self.process
        with self.assertRaises(RuntimeError):
            self.controller.delete_account({'id': 'fixture'})
        self.assertEqual(len(load_accounts(self.path)), 1)

    def test_delete_never_follows_managed_profile_symlink(self):
        profile = load_accounts(self.path)[0]['profile']
        profile.parent.mkdir(parents=True)
        external = Path(self.temp.name) / 'outside-profile'
        external.mkdir()
        marker = external / 'keep.txt'
        marker.write_text('external data')
        try:
            profile.symlink_to(external, target_is_directory=True)
        except OSError:
            self.skipTest('Directory symlinks unavailable on this host')
        result = self.controller.delete_account({'id': 'fixture'})
        self.assertTrue(result['profile_preserved'])
        self.assertTrue(marker.exists())
        self.assertFalse(profile.exists())

    def test_visible_jobs_and_private_login_are_exclusive(self):
        self.controller.monitor_available = True
        with patch("auto_location.service.subprocess.Popen", return_value=self.process) as start:
            self.controller.setup("fixture", "test-login")
            argv = start.call_args.args[0]
            self.assertIn("login", argv)
            self.assertNotIn("--headless", argv)
            self.assertIn("--web-auth", argv)
            self.assertNotIn("--force-login", argv)
            self.assertFalse(self.controller.status()["awaiting_finish"])
            self.assertTrue(self.controller.status()["monitored"])
            self.assertTrue(self.path.with_suffix('.batch.lock').exists())
            self.assertEqual(self.controller.job_output.name, "setup-result.json")
            with self.assertRaises(RuntimeError):
                self.controller.configure({"id": "other"})
            with self.assertRaises(RuntimeError):
                self.controller.start("checkin", monitor=True)
            self.controller.auth_action({'action': 'control', 'id': 'fixture-code', 'value': '123456'})
            self.assertIn(b'123456', self.process.stdin.getvalue())
            with self.assertRaises(ValueError):
                self.controller.auth_action({'action': 'javascript', 'value': 'unsafe'})
            self.code = 0
            self.controller.status()
            self.assertFalse(self.path.with_suffix('.batch.lock').exists())
            self.controller.start("checkin", monitor=True)
            self.assertNotIn("--headless", start.call_args.args[0])
            self.assertTrue(self.controller.status()["monitored"])

    def test_credentials_write_only_and_legacy_store_removed_after_save(self):
        profile = load_accounts(self.path)[0]['profile']
        profile.mkdir(parents=True)
        (profile / 'logins.json').write_text('legacy')
        (profile / 'key4.db').write_text('preserve unrelated browser keys')
        (profile / 'cookies.sqlite').write_text('preserve session')
        self.controller.save_credentials({'id':'fixture','username':'secret-user','password':'secret-password'})
        self.assertFalse((profile / 'logins.json').exists())
        self.assertTrue((profile / 'cookies.sqlite').exists())
        status = json.dumps(self.controller.status())
        self.assertNotIn('secret-user', status)
        self.assertNotIn('secret-password', status)
        self.assertEqual(self.controller.status()['accounts'][0]['saved_credentials'], 1)
        self.assertEqual(self.controller.vault.read('fixture'), ('secret-user','secret-password'))
        with patch('auto_location.service.subprocess.Popen', return_value=self.process):
            self.controller.start('inspect')
            with self.assertRaises(RuntimeError):
                self.controller.save_credentials({'id':'fixture','username':'other','password':'replacement'})
        for operation in ('credentials','manual-login'):
            with self.assertRaises(ValueError):
                self.controller.setup('fixture',operation)

    def test_cli_reports_vault_configuration_without_returning_credentials(self):
        from auto_location.cli import main
        self.controller.save_credentials({'id':'fixture','username':'secret-user','password':'secret-password'})
        output = self.path.parent / 'account-list.json'
        with patch.dict(os.environ, {'AUTO_LOCATION_VAULT_KEY_FILE':str(self.controller.vault.key_path)}), redirect_stdout(io.StringIO()):
            code = main(['account-list','--accounts',str(self.path),'--output',str(output)])
        self.assertEqual(code,0)
        report=output.read_text()
        self.assertEqual(json.loads(report)['accounts'][0]['saved_credentials'],1)
        self.assertNotIn('secret-user',report)
        self.assertNotIn('secret-password',report)
        with patch.dict(os.environ, {}, clear=True), redirect_stdout(io.StringIO()):
            code = main(['account-list','--accounts',str(self.path),'--output',str(output)])
            from auto_location.cli import parser, uses_vault
            self.assertTrue(uses_vault(parser().parse_args(['login','--account-id','fixture','--accounts',str(self.path)])))
        self.assertEqual(code,0)
        self.assertEqual(json.loads(output.read_text())['accounts'][0]['saved_credentials'],1)

    def test_web_account_settings_reject_secrets_and_preserve_profiles(self):
        old_profile = load_accounts(self.path)[0]["profile"]
        self.controller.configure({"id": "new-user"})
        self.controller.configure({"id": "fixture", "enabled": False, "auto_login": False, "area_index": 2})
        accounts = load_accounts(self.path)
        self.assertEqual(accounts[0]["profile"], old_profile)
        self.assertFalse(accounts[0]["auto_login"])
        self.assertFalse(accounts[0]["enabled"])
        self.assertEqual(accounts[0]["area_index"], 2)
        for invalid in ({"id": "../escape"}, {"id": "other", "password": "never-store"},
                        {"id": "fixture", "enabled": True, "auto_login": "false", "area_index": None}):
            with self.assertRaises((ValueError, CheckinError)):
                self.controller.configure(invalid)
        self.assertNotIn("never-store", self.path.read_text())
        self.assertNotIn("profile", self.controller.status()["accounts"][0])

    def test_monitor_unavailable_and_login_test_never_submit_checkin(self):
        with self.assertRaises(ValueError):
            self.controller.start("inspect", monitor=True)
        with self.assertRaises(ValueError):
            self.controller.setup("fixture", "credentials")
        self.controller.monitor_available = True
        with patch("auto_location.service.subprocess.Popen", return_value=self.process) as start:
            self.controller.setup("fixture", "test-login", monitor=False)
            argv = start.call_args.args[0]
            self.assertIn("--auto-login", argv)
            self.assertIn("--headless", argv)
            self.assertNotIn("--force-login", argv)
            self.assertNotIn("--simulate-miniprogram", argv)
            self.assertNotIn("checkin", argv)
            self.assertFalse(self.controller.status()["awaiting_finish"])

    def test_monitor_cookie_required_and_same_origin_proxy_assets(self):
        from http.server import BaseHTTPRequestHandler
        class Backend(BaseHTTPRequestHandler):
            def do_GET(self):
                if self.headers.get('Upgrade') == 'websocket':
                    self.send_response(101)
                    self.send_header('Upgrade', 'websocket')
                    self.send_header('Connection', 'Upgrade')
                    self.end_headers()
                    self.close_connection = True
                    return
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.end_headers()
                self.wfile.write(b"monitor asset")
            def log_message(self, *args):
                pass
        backend = ThreadingHTTPServer(("127.0.0.1", 0), Backend)
        backend_thread = Thread(target=backend.serve_forever, daemon=True)
        backend_thread.start()
        self.controller.monitor_available = True
        self.controller.monitor_port = backend.server_port
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler_for(self.controller))
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_port}"
        try:
            with self.assertRaises(HTTPError) as denied:
                urlopen(base + "/monitor/vnc.html")
            self.assertEqual(denied.exception.code, 403)
            denied.exception.close()
            with urlopen(base + "/") as response:
                cookie = response.headers['Set-Cookie']
                self.assertIn("HttpOnly", cookie)
                self.assertIn("SameSite=Strict", cookie)
                self.assertNotIn("Path=", cookie)
            with urlopen(Request(base + "/monitor/vnc.html", headers={"Cookie": cookie.split(';')[0]})) as response:
                self.assertEqual(response.read(), b"monitor asset")
                self.assertEqual(response.headers['X-Frame-Options'], 'SAMEORIGIN')
            connection = http.client.HTTPConnection('127.0.0.1', server.server_port)
            connection.request('GET', '/monitor/websockify', headers={"Cookie": cookie.split(';')[0], "Upgrade": "websocket", "Origin": "http://evil.example"})
            response = connection.getresponse()
            self.assertEqual(response.status, 403)
            response.read()
            connection.close()
            connection = http.client.HTTPConnection('127.0.0.1', server.server_port)
            connection.request('GET', '/monitor/websockify', headers={
                'Cookie': cookie.split(';')[0], 'Upgrade': 'websocket',
                'Origin': base, 'Sec-WebSocket-Key': 'fixture', 'Sec-WebSocket-Version': '13'})
            response = connection.getresponse()
            self.assertEqual(response.status, 101)
            connection.close()
        finally:
            for item in (server, backend):
                item.shutdown()
                item.server_close()
            thread.join(timeout=3)
            backend_thread.join(timeout=3)

    def test_http_dashboard_token_validation_and_duplicate_rejection(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler_for(self.controller))
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_port}"
        def post(operation, token=None):
            headers = {"Content-Type": "application/json"}
            if token:
                headers["X-Run-Token"] = token
            req = Request(base + "/api/run", data=json.dumps({"operation": operation}).encode(), headers=headers)
            try:
                with urlopen(req) as response:
                    return response.status
            except HTTPError as error:
                error.close()
                return error.code
        try:
            with patch("auto_location.service.subprocess.Popen", return_value=self.process) as start:
                with urlopen(base + "/") as response:
                    self.assertIn(self.controller.token, response.read().decode())
                self.assertEqual(post("checkin"), 403)
                self.assertEqual(post("invalid", self.controller.token), 400)
                self.assertEqual(post("inspect", self.controller.token), 202)
                self.assertNotIn("--simulate-miniprogram", start.call_args.args[0])
                self.assertEqual(post("checkin", self.controller.token), 409)
                self.assertEqual(start.call_count, 1)
                with urlopen(base + "/api/status") as response:
                    self.assertTrue(json.load(response)["running"])
                with urlopen(base + "/healthz") as response:
                    self.assertEqual(response.status, 200)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)

    def test_schedule_http_config_auth_and_persistence(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler_for(self.controller))
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_port}"
        def post(value, token=None):
            headers = {"Content-Type": "application/json"}
            if token:
                headers["X-Run-Token"] = token
            try:
                with urlopen(Request(base + "/api/schedule", data=json.dumps(value).encode(), headers=headers)) as response:
                    return response.status
            except HTTPError as error:
                error.close()
                return error.code
        try:
            value = {"enabled": False, "start": "21:10", "end": "22:20"}
            self.assertEqual(post(value), 403)
            self.assertEqual(post({**value, "extra": 1}, self.controller.token), 400)
            self.assertEqual(post(value, self.controller.token), 200)
            with urlopen(base + "/api/status") as response:
                status = json.load(response)
                self.assertEqual(status["schedule"]["config"], value)
                self.assertEqual(status["schedule"]["timezone"], "Asia/Shanghai")
            from auto_location.scheduler import DailyScheduler
            restored = DailyScheduler(self.path.parent / "schedule.json", self.controller)
            self.assertEqual(restored.snapshot()["config"], value)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)
