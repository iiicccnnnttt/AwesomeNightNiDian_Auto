import json
import math
import unittest
from unittest.mock import patch
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from tempfile import TemporaryDirectory
from pathlib import Path
from contextlib import contextmanager

from camoufox.sync_api import Camoufox

from auto_location.checkin import CheckinError, CheckinPage, Location, SHANGHAI, SUBMIT, TODAY, REVERSE, checked_record, location_preferences
from auto_location.auth import PortalLogin, LOGIN_URL, AUTH_HOST
from auto_location.cli import main as cli_main

BASE = "http://localhost:18089"

# Offline reproduction of the observed site's interaction contract. No school API requests.
HTML = """<!doctype html><meta charset='utf-8'><body><div id='app'></div>
<script>
async function load() {
 const r = await (await fetch('TODAY')).json();
 if(r.code !== 0) return;
 const d=r.data, app=document.querySelector('#app');
 await fetch('REVERSE');
 if(d.recordStatusCode === 'CHECKED_IN') {
  app.innerHTML = `<span class='location-pill success'>有效打卡（${d.businessDate}）</span>`;
 } else {
  app.innerHTML=`<div class='check-in-circle'>请打卡</div><span>已进入打卡范围：测试宿舍</span>`;
  app.querySelector('.check-in-circle').onclick=()=> {
   app.insertAdjacentHTML('beforeend', `<div class='wd-message-box__container'>确认打卡<p>是否确认打卡？</p><button class='wd-message-box__actions-btn'><span style='pointer-events:none'>确定</span></button></div>`);
   app.querySelector('button').onclick=async()=> {
    await fetch('SUBMIT', {method:'POST'});
    document.querySelector('.wd-message-box__container').remove();
    await load();
   };
  };
 }
}
load();
</script>""".replace("TODAY", TODAY).replace("SUBMIT", SUBMIT).replace("REVERSE", REVERSE)


class LogicTests(unittest.TestCase):
    def test_auth_wait_tolerates_navigation_without_resubmitting(self):
        from types import SimpleNamespace
        from playwright.sync_api import Error
        page=SimpleNamespace(set_default_timeout=lambda _: None,wait_for_timeout=lambda _: None)
        login=PortalLogin(page,timeout=1)
        states=iter([Error('Execution context was destroyed, most likely because of a navigation'),True])
        def predicate():
            value=next(states)
            if isinstance(value,Exception):
                raise value
            return value
        login._wait(predicate,'navigation failed')
        self.assertEqual(login.login_attempts,0)

    def test_location_validation(self):
        for values in ({"latitude": math.nan, "longitude": 104},
                       {"latitude": 30, "longitude": 181},
                       {"latitude": 30, "longitude": 104, "accuracy": -1},
                       {"latitude": True, "longitude": 104},
                       {"latitude": 30, "longitude": 104, "coordinate_system": "GCJ02"}):
            with self.subTest(values=values), self.assertRaises(ValueError):
                Location(**values)

    def test_valid_record_requires_date_status_and_time(self):
        date = "2026-10-10"
        self.assertTrue(checked_record({"businessDate": date, "recordStatusCode": "CHECKED_IN", "checkinTime": date + " 22:00:00"}, date))
        for status in ("PENDING", "EXEMPT", "STUDENT_REPORTED", "INSTRUCTOR_REPORTED"):
            self.assertFalse(checked_record({"businessDate": date, "recordStatusCode": status, "checkinTime": "22:00"}, date))
        self.assertFalse(checked_record({"businessDate": "2026-10-09", "recordStatusCode": "CHECKED_IN", "checkinTime": "22:00"}, date))
        self.assertFalse(checked_record({"businessDate": date, "recordStatusCode": "CHECKED_IN"}, date))


class BrowserTests(unittest.TestCase):
    def test_discovery_and_route_position_never_submit(self):
        from auto_location.areas import AVAILABLE
        self.context.route(BASE + AVAILABLE, lambda route: route.fulfill(json={"code": 0, "data": [
            {"latitude": 30.7, "longitude": 104.1, "radiusMeter": 120, "groupName": "测试宿舍"}]}))
        self.html = self.html.replace("const r =", "await fetch('" + AVAILABLE + "'); const r =")
        self.bot.open()
        result = self.bot.use_area()
        self.assertTrue(result["page_in_range"])
        self.assertFalse(result["device_location"])
        from urllib.parse import parse_qs, urlsplit
        query = parse_qs(urlsplit(self.page.url).fragment.split("?", 1)[1])
        point = result["selected_point"]
        self.assertEqual(float(query["latitude"][0]), point["latitude"])
        self.assertEqual(float(query["longitude"][0]), point["longitude"])
        self.assertNotEqual((point["latitude"], point["longitude"]), (30.7, 104.1))
        self.assertGreater(point["boundary_clearance_meter"], 12 - 1e-7)
        selected_url = self.bot.url
        self.bot.open()
        self.assertEqual(self.page.url, selected_url)
        self.assertEqual(self.posts, 0)

    def test_area_outside_time_does_not_claim_range_or_submit(self):
        from auto_location.areas import AVAILABLE
        self.context.route(BASE + AVAILABLE, lambda route: route.fulfill(json={"code": 0, "data": [
            {"latitude": 30.7, "longitude": 104.1, "radiusMeter": 120, "groupName": "测试宿舍"}]}))
        self.html = self.html.replace("const r =", "await fetch('" + AVAILABLE + "'); const r =")
        self.html = self.html.replace("已进入打卡范围：测试宿舍", "未到打卡时间段")
        self.bot.open()
        result = self.bot.use_area()
        self.assertFalse(result["page_in_range"])
        self.assertEqual(result["range_validation"], "not_visible_in_current_state")
        with self.assertRaises(CheckinError):
            self.bot.checkin()
        self.assertEqual(self.posts, 0)

    @classmethod
    def setUpClass(cls):
        cls.manager = Camoufox(headless=True, geoip=False, locale="zh-CN",
            firefox_user_prefs=location_preferences(Location(30.5, 104.1, 20)), config={
            "geolocation:latitude": 30.5, "geolocation:longitude": 104.1,
            "geolocation:accuracy": 20})
        cls.browser = cls.manager.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.manager.__exit__(None, None, None)

    def setUp(self):
        self.context = self.browser.new_context(permissions=["geolocation"],
                                               geolocation={"latitude": 30.5, "longitude": 104.1, "accuracy": 20})
        self.page = self.context.new_page()
        self.date = datetime.now(SHANGHAI).date().isoformat()
        self.state = "PENDING"
        self.posts = 0
        self.reject = False
        self.reject_code = 1
        self.reject_message = ""
        self.reads = 0
        self.html = HTML

        def route(request):
            path = request.request.url.removeprefix(BASE)
            if path == TODAY:
                self.reads += 1
                data = {"recordStatusCode": self.state, "businessDate": self.date,
                        "checkinTime": self.date + " 22:00:00" if self.state == "CHECKED_IN" else None}
                request.fulfill(json={"code": 0, "data": data})
            elif path == SUBMIT:
                self.posts += 1
                if not self.reject:
                    self.state = "CHECKED_IN"
                request.fulfill(json={"code": self.reject_code if self.reject else 0,
                                      "msg": self.reject_message if self.reject else ""})
            elif path == REVERSE:
                request.fulfill(json={"code": 0, "data": "fixture location"})
            else:
                request.fulfill(body=self.html, content_type="text/html; charset=utf-8")
        self.context.route(BASE + "/**", route)
        self.bot = CheckinPage(self.page, BASE + "/daily-checkin-h5/", timeout=15)

    def tearDown(self):
        self.context.close()

    def test_submit_reload_and_idempotency(self):
        self.bot.open()
        result = self.bot.checkin()
        self.assertEqual(result["status"], "verified")
        self.assertEqual(self.posts, 1)
        self.assertGreaterEqual(self.reads, 3)
        self.assertTrue(self.bot.checkin()["already_checked_in"])
        self.assertEqual(self.posts, 1)

    def test_open_reads_record_when_portal_loaded_app_first(self):
        self.page.goto(self.bot.url, wait_until="domcontentloaded")
        self.page.locator(".check-in-circle").wait_for(state="visible")
        self.bot.record = None
        previous_reads = self.reads
        self.bot.open()
        self.assertIsNotNone(self.bot.record)
        self.assertGreater(self.reads, previous_reads)

    def test_server_rejection_is_not_success(self):
        self.reject = True
        self.bot.open()
        with self.assertRaises(CheckinError):
            self.bot.checkin()
        self.assertEqual(self.posts, 1)

    def test_miniprogram_required_response_is_reported_as_failure(self):
        self.reject = True
        self.reject_code = 4006
        self.reject_message = "请在微信小程序内打开打卡页面后再打卡"
        self.bot.open()
        with self.assertRaisesRegex(CheckinError, "4006.*微信小程序"):
            self.bot.checkin()
        self.assertEqual(self.posts, 1)
        self.assertEqual(self.bot.record["recordStatusCode"], "PENDING")

    def test_platform_simulation_against_local_server(self):
        observed = []
        state = {"status": "PENDING"}
        date = self.date
        payload = {"latitude": 30.5, "longitude": 104.1, "accuracy": 20,
                   "coordinateSystemCode": "gcj02"}
        body = json.dumps(payload, separators=(",", ":"))
        html = HTML.replace("async function load() {", """
const originalFetch = window.fetch.bind(window);
window.fetch = (url, options = {}) => originalFetch(url, {...options, headers: {
 platform: 'h5', Authorization: 'Bearer fixture-token', 'X-Fixture': 'preserve',
 ...options.headers
}});
async function load() {""").replace("{method:'POST'}", "{method:'POST', headers:{'Content-Type':'application/json'}, body:" + json.dumps(body) + "}")

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def reply(self, value, html=False):
                raw = (value if html else json.dumps(value)).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8" if html else "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def record(self, body=b""):
                observed.append({"method": self.command, "path": self.path, "body": body,
                                 "headers": {k.lower(): v for k, v in self.headers.items()}})

            def do_GET(self):
                self.record()
                if self.path == TODAY:
                    self.reply({"code": 0, "data": {"recordStatusCode": state["status"],
                        "businessDate": date, "checkinTime": date + " 22:00:00" if state["status"] == "CHECKED_IN" else None}})
                elif self.path == REVERSE:
                    self.reply({"code": 0, "data": "fixture location"})
                elif self.path == SUBMIT:
                    self.reply({"code": 0})
                else:
                    self.reply(html, html=True)

            def do_POST(self):
                self.record(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
                accepted = self.headers.get("platform") == "mp-weixin"
                if self.path == SUBMIT and accepted:
                    state["status"] = "CHECKED_IN"
                self.reply({"code": 0 if accepted or self.path != SUBMIT else 4006,
                            "msg": "" if accepted else "请在微信小程序内打开打卡页面后再打卡"})

        with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
            thread = Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base = f"http://127.0.0.1:{server.server_port}"
            self.context.add_cookies([{"name": "fixture_session", "value": "fixture-cookie", "url": base}])
            try:
                normal = CheckinPage(self.page, base + "/daily-checkin-h5/", timeout=15)
                normal.open()
                with self.assertRaisesRegex(CheckinError, "4006"):
                    normal.checkin()
                simulated = CheckinPage(self.page, base + "/daily-checkin-h5/", timeout=15,
                                        simulate_miniprogram=True)
                simulated.open()
                result = simulated.checkin()
                self.assertEqual(result["status"], "verified")
                self.assertEqual(result["platform_simulation"],
                                 {"platform": "mp-weixin", "requests": 1, "original_platform": "h5"})
                self.assertTrue(simulated.checkin()["already_checked_in"])
                # The same endpoint's GET and an unrelated POST retain h5.
                self.page.evaluate("""async paths => {
                    await fetch(paths[0], {headers:{platform:'h5'}});
                    await fetch(paths[1], {method:'POST', headers:{platform:'h5'}, body:'unrelated'});
                }""", [SUBMIT, SUBMIT.rsplit("/", 1)[0] + "/report"])
                submissions = [r for r in observed if r["method"] == "POST" and r["path"] == SUBMIT]
                self.assertEqual([r["headers"]["platform"] for r in submissions], ["h5", "mp-weixin"])
                for request in submissions:
                    self.assertEqual(request["body"], body.encode())
                    self.assertEqual(request["headers"]["authorization"], "Bearer fixture-token")
                    self.assertEqual(request["headers"]["cookie"], "fixture_session=fixture-cookie")
                    self.assertEqual(request["headers"]["x-fixture"], "preserve")
                for request in observed:
                    if request["path"] in (TODAY, REVERSE) or (request["method"] == "GET" and request["path"] == SUBMIT) or request["path"].endswith("/report"):
                        self.assertEqual(request["headers"]["platform"], "h5")
                self.assertEqual(simulated.platform_simulation["requests"], 1)
            finally:
                server.shutdown()
                thread.join(timeout=3)

    def test_failure_evidence_saved_before_keep_open(self):
        self.reject = True
        self.reject_code = 4006
        self.reject_message = "请在微信小程序内打开打卡页面后再打卡"
        with TemporaryDirectory() as directory:
            output = Path(directory) / "result.json"

            @contextmanager
            def retained_session(*_):
                try:
                    yield self.context
                finally:
                    # This is where the real session waits for user input.
                    result = json.loads(output.read_text(encoding="utf-8"))
                    self.assertEqual(result["record"]["recordStatusCode"], "PENDING")
                    self.assertEqual(result["submission"]["code"], 4006)
                    self.assertEqual(result["platform_simulation"]["requests"], 1)

            with patch("auto_location.cli.session", retained_session), patch("sys.stderr"):
                code = cli_main(["checkin", "--simulate-miniprogram", "--keep-open",
                                 "--url", BASE + "/daily-checkin-h5/", "--timeout", "15",
                                 "--output", str(output)])
            self.assertEqual(code, 1)
            self.assertEqual(self.posts, 1)

    def test_waits_for_location_before_rejecting_initial_outside_state(self):
        self.html = HTML.replace("await fetch('" + REVERSE + "');", """
        app.innerHTML="<div class='check-in-circle disabled'>请打卡</div><span>您当前未在宿舍打卡区域</span>";
        await new Promise(resolve=>setTimeout(resolve,600));
        await fetch('""" + REVERSE + "');")
        self.bot.open()
        self.assertEqual(self.bot.checkin()["status"], "verified")
        self.assertEqual(self.posts, 1)

    def test_outside_range_never_submits(self):
        self.html = HTML.replace("class='check-in-circle'", "class='check-in-circle disabled'").replace("已进入打卡范围：测试宿舍", "您当前未在宿舍打卡区域")
        self.bot.open()
        with self.assertRaisesRegex(CheckinError, "不在宿舍"):
            self.bot.checkin()
        self.assertEqual(self.posts, 0)

    def test_wrong_business_date_never_submits(self):
        self.date = (datetime.now(SHANGHAI) - timedelta(days=1)).date().isoformat()
        self.bot.open()
        with self.assertRaisesRegex(CheckinError, "业务日期"):
            self.bot.checkin()
        self.assertEqual(self.posts, 0)

    def test_exemption_never_submits(self):
        self.state = "EXEMPT"
        self.bot.open()
        with self.assertRaisesRegex(CheckinError, "EXEMPT"):
            self.bot.checkin()
        self.assertEqual(self.posts, 0)

    def test_browser_receives_location(self):
        self.bot.open()
        location = self.bot.capture_location()
        self.assertAlmostEqual(location.latitude, 30.5, places=5)
        self.assertAlmostEqual(location.longitude, 104.1, places=5)
        self.assertEqual(location.coordinate_system, "WGS84")

    def auth_fixture(self, *, authenticated=False, rejected=False, challenge=False, auth_host=AUTH_HOST):
        self.auth_submissions = 0
        state = {"authenticated": authenticated}
        portal = """<html><meta charset='utf-8'><a href='/daily-checkin-h5/test' target='_blank'>晚安成电</a></html>"""
        form = """<html><meta charset='utf-8'><div class='login-main'><form class='m-account' method='POST' action='/authserver/login'>
        <input id='username' name='username'><input id='password' name='password' type='password'>
        <button id='login_submit'>登录</button></form><span id='showErrorTip'>ERROR</span>CHALLENGE</div></html>"""

        def route(request):
            url = request.request.url
            if url == LOGIN_URL:
                if state["authenticated"]:
                    request.fulfill(body=portal, content_type="text/html; charset=utf-8")
                else:
                    # Playwright routes do not re-intercept a server redirect's
                    # next hop. Use a fresh browser navigation to keep all
                    # fixture requests local and avoid contacting the school.
                    request.fulfill(body=f"<script>location.href='https://{auth_host}/authserver/login'</script>",
                                    content_type="text/html")
            elif "/authserver/login" in url:
                if request.request.method == "POST":
                    self.auth_submissions += 1
                    if not rejected:
                        state["authenticated"] = True
                        request.fulfill(body=f"<script>location.href='{LOGIN_URL}'</script>", content_type="text/html")
                        return
                error = "认证未通过" if rejected and self.auth_submissions else ""
                captcha = "<input id='captcha'>" if challenge else ""
                request.fulfill(body=form.replace("ERROR", error).replace("CHALLENGE", captcha),
                                content_type="text/html; charset=utf-8")
            elif "/daily-checkin-h5/" in url:
                request.fulfill(body="<html><div class='check-in-circle'>test</div></html>", content_type="text/html")
            else:
                request.abort()
        self.context.route("**/*", route)

    def test_auto_login_opens_portal_then_app_popup(self):
        self.auth_fixture()
        with patch.dict("os.environ", {"UESTC_USERNAME": "fixture-user", "UESTC_PASSWORD": "fixture-password"}):
            page = PortalLogin(self.page, timeout=15).login()
        self.assertIn("/daily-checkin-h5/", page.url)
        self.assertIsNot(page, self.page)
        self.assertEqual(self.auth_submissions, 1)

    def test_encrypted_vault_login_and_visible_session_only_guard(self):
        from auto_location.vault import CredentialVault
        import os
        with TemporaryDirectory() as directory:
            key=Path(directory) / 'key'
            key.write_bytes(os.urandom(32))
            vault=CredentialVault(Path(directory) / 'credentials',key)
            vault.save('fixture','fixture-user','fixture-password')
            self.auth_fixture()
            login=PortalLogin(self.page,timeout=15,allow_environment=False,
                              credential_loader=lambda:vault.read('fixture'))
            page=login.login()
            self.assertIn('/daily-checkin-h5/',page.url)
            self.assertEqual(login.credential_source,'encrypted_vault')
            self.assertEqual(self.auth_submissions,1)
            self.auth_fixture()
            with self.assertRaisesRegex(CheckinError,'可见浏览器不填写密码'):
                PortalLogin(self.page,timeout=15,credential_loader=lambda:self.fail('vault read in visible phase'),
                            session_only=True).login()
            self.assertEqual(self.auth_submissions,0)

    def test_auto_login_reuses_session_without_credentials(self):
        self.auth_fixture(authenticated=True)
        with patch.dict("os.environ", {}, clear=True):
            page = PortalLogin(self.page, timeout=15).login()
        self.assertIn("/daily-checkin-h5/", page.url)
        self.assertEqual(self.auth_submissions, 0)

    def test_auto_login_rejection_does_not_retry_or_log_credentials(self):
        self.auth_fixture(rejected=True)
        with patch.dict("os.environ", {"UESTC_USERNAME": "fixture-user", "UESTC_PASSWORD": "fixture-password"}):
            with self.assertRaises(CheckinError) as error:
                PortalLogin(self.page, timeout=15).login()
        self.assertNotIn("fixture-password", str(error.exception))
        self.assertNotIn("fixture-user", str(error.exception))
        self.assertEqual(self.auth_submissions, 1)

    def test_auto_login_missing_credentials_never_submits(self):
        self.auth_fixture()
        with patch.dict("os.environ", {}, clear=True):
            with self.assertRaisesRegex(CheckinError, "UESTC_USERNAME"):
                PortalLogin(self.page, timeout=15).login()
        self.assertEqual(self.auth_submissions, 0)

    def test_registered_account_never_uses_global_environment_credentials(self):
        self.auth_fixture()
        with patch.dict("os.environ", {"UESTC_USERNAME": "fixture-user", "UESTC_PASSWORD": "fixture-password"}):
            with self.assertRaisesRegex(CheckinError, "此账号 profile"):
                PortalLogin(self.page, timeout=15, allow_environment=False).login()
        self.assertEqual(self.auth_submissions, 0)

    def test_auto_login_challenge_never_submits(self):
        self.auth_fixture(challenge=True)
        with patch.dict("os.environ", {"UESTC_USERNAME": "fixture-user", "UESTC_PASSWORD": "fixture-password"}):
            with self.assertRaisesRegex(CheckinError, "验证码"):
                PortalLogin(self.page, timeout=15).login()
        self.assertEqual(self.auth_submissions, 0)

    def test_local_gui_login_accepts_manually_filled_initial_captcha(self):
        from auto_location.local_auth import LocalAuthentication
        self.auth_fixture(challenge=True)
        def fill_code(_):
            self.page.locator('#captcha').fill('fixture-code')
            return ''
        with patch('builtins.input', side_effect=fill_code):
            login = PortalLogin(self.page, timeout=15, credential_loader=lambda: ('fixture-user', 'fixture-password'),
                                interaction=LocalAuthentication())
            self.assertIn('/daily-checkin-h5/', login.login().url)
        self.assertEqual(self.auth_submissions, 1)

    def test_web_mfa_controls_exclude_password_and_reject_stale_controls(self):
        from auto_location.web_auth import WebAuthentication
        with TemporaryDirectory() as directory:
            path = Path(directory) / 'verification.json'
            with patch('auto_location.web_auth.threading.Thread'):
                interaction = WebAuthentication(path)
            self.page.route('**/*', lambda route: route.fulfill(body="""
                <input id='password' type='password' value='fixture-secret'>
                <button onclick="document.querySelector('#password').type='text'">显示密码</button>
                <input id='otp' placeholder='验证码'>
                <label><input id='trustDevice' type='checkbox'>信任此设备</label>
                <button onclick="document.body.dataset.sent='yes'">发送验证码</button>
                <button onclick="document.body.dataset.verified=document.querySelector('#otp').value">确认</button>
            """, content_type='text/html; charset=utf-8'))
            self.page.goto('https://' + AUTH_HOST + '/authserver/mfa')
            interaction.tick(self.page)
            state = json.loads(path.read_text(encoding='utf-8'))
            self.assertEqual(len(state['controls']), 4)
            self.assertNotIn('fixture-secret', path.read_text(encoding='utf-8'))
            code = next(c['id'] for c in state['controls'] if c['kind'] == 'code')
            trust = next(c['id'] for c in state['controls'] if c['kind'] == 'trust')
            confirm = next(c['id'] for c in state['controls'] if c['label'] == '确认')
            interaction.actions.put({'action': 'control', 'id': code, 'value': '246810'})
            interaction.actions.put({'action': 'control', 'id': trust, 'value': True})
            interaction.actions.put({'action': 'continue'})
            interaction.tick(self.page)
            self.assertTrue(self.page.locator('#trustDevice').is_checked())
            self.assertEqual(self.page.locator('body').get_attribute('data-verified'), '246810')
            self.assertNotIn('246810', path.read_text(encoding='utf-8'))
            self.page.locator('button').filter(has_text='确认').evaluate("el => el.remove()")
            interaction.actions.put({'action': 'continue'})
            interaction.tick(self.page)
            self.assertIn('未找到唯一', json.loads(path.read_text(encoding='utf-8'))['error'])
            self.page.reload()
            self.page.locator('#otp').fill('')
            interaction.actions.put({'action': 'control', 'id': code, 'value': 'stale'})
            interaction.tick(self.page)
            self.assertEqual(self.page.locator('#otp').input_value(), '')
            self.assertIsNotNone(json.loads(path.read_text(encoding='utf-8'))['error'])
            self.page.goto('https://untrusted.invalid/')
            interaction.tick(self.page)
            self.assertEqual(json.loads(path.read_text(encoding='utf-8'))['controls'], [])

    def test_web_first_login_mfa_and_trusted_session_reuse(self):
        from auto_location.web_auth import WebAuthentication
        submissions = []
        def route(request):
            url = request.request.url
            if url == LOGIN_URL:
                if 'trusted-fixture=yes' in request.request.headers.get('cookie', ''):
                    body = "<a href='/daily-checkin-h5/test' target='_blank'>晚安成电</a>"
                else:
                    body = f"<script>location.href='https://{AUTH_HOST}/authserver/login'</script>"
            elif '/authserver/login' in url:
                if request.request.method == 'POST':
                    submissions.append('login')
                    body = f"<script>location.href='https://{AUTH_HOST}/authserver/mfa'</script>"
                else:
                    body = """<div class='login-main'><form class='m-account' method='post'>
                        <input id='username'><input id='password' type='password'>
                        <button id='login_submit'>登录</button></form></div>"""
            elif '/authserver/mfa' in url:
                body = f"""<input id='otp' placeholder='验证码'><label><input id='trustDevice' type='checkbox'>信任此设备</label>
                    <button onclick="if(document.querySelector('#otp').value==='123456' && document.querySelector('#trustDevice').checked) location.href='{LOGIN_URL}?verified'">确认</button>"""
            elif url == LOGIN_URL + '?verified':
                request.fulfill(body=f"<script>location.href='{LOGIN_URL}'</script>",
                    headers={'Set-Cookie': 'trusted-fixture=yes; Domain=uestc.edu.cn; Path=/; Secure'},
                    content_type='text/html; charset=utf-8')
                return
            elif '/daily-checkin-h5/' in url:
                body = '<div class="check-in-circle">test</div>'
            else:
                request.abort(); return
            request.fulfill(body=body, content_type='text/html; charset=utf-8')
        self.context.route('**/*', route)
        with TemporaryDirectory() as directory:
            class FixtureInteraction(WebAuthentication):
                def tick(inner, page, before_submit=False):
                    super().tick(page, before_submit=before_submit)
                    for c in inner.previous['controls']:
                        if c['kind'] == 'code':
                            inner.actions.put({'action': 'control', 'id': c['id'], 'value': '123456'})
                        elif c['kind'] == 'trust':
                            inner.actions.put({'action': 'control', 'id': c['id'], 'value': True})
                        else:
                            inner.actions.put({'action': 'continue'})
                    return super().tick(page, before_submit=before_submit)
            with patch('auto_location.web_auth.threading.Thread'):
                interaction = FixtureInteraction(Path(directory) / 'auth.json')
            login = PortalLogin(self.page, timeout=15, credential_loader=lambda: ('fixture', 'fixture-secret'),
                                interaction=interaction)
            self.assertIn('/daily-checkin-h5/', login.login().url)
            self.assertEqual(submissions, ['login'])
            self.assertNotIn('fixture-secret', interaction.path.read_text(encoding='utf-8'))
            reused = PortalLogin(self.context.new_page(), timeout=15,
                                credential_loader=lambda: self.fail('trusted session should reuse cookies'))
            self.assertIn('/daily-checkin-h5/', reused.login().url)
            self.assertEqual(submissions, ['login'])

    def test_auto_login_untrusted_host_never_submits(self):
        self.auth_fixture(auth_host="untrusted.invalid")
        with patch.dict("os.environ", {"UESTC_USERNAME": "fixture-user", "UESTC_PASSWORD": "fixture-password"}):
            with self.assertRaisesRegex(CheckinError, "地址不匹配"):
                PortalLogin(self.page, timeout=15).login()
        self.assertEqual(self.auth_submissions, 0)


if __name__ == "__main__":
    unittest.main()


