import base64
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from camoufox.sync_api import Camoufox

from auto_location.auth import LOGIN_URL, PortalLogin
from auto_location.browser_passwords import (
    AUTH_ORIGIN, BrowserPasswords, password_preferences, prepare_password_browser,
)
from auto_location.checkin import CheckinError


FIXTURE_USERNAME = "fixture-browser-user"
FIXTURE_PASSWORD = "fixture-browser-password-only"


class PasswordBrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = TemporaryDirectory(prefix="auto-location-password-tests-")
        cls.profile = Path(cls.directory.name) / "saved-profile"
        cls.executable = prepare_password_browser()
        cls.passwords = BrowserPasswords(cls.profile)
        try:
            with cls.browser(cls.profile) as context:
                page = context.new_page()
                cls.passwords.open_editor(page)
                page.locator("input[name=username]").fill(FIXTURE_USERNAME)
                page.locator("input[name=password]").fill(FIXTURE_PASSWORD)
                page.locator(".save-changes-button").click()
                for _ in range(50):
                    if cls.passwords.saved_count():
                        break
                    page.wait_for_timeout(200)
                cls.passwords.summary()
        except BaseException:
            cls.directory.cleanup()
            raise

    @classmethod
    def tearDownClass(cls):
        cls.directory.cleanup()

    @classmethod
    def browser(cls, profile):
        return Camoufox(headless=True, geoip=False, persistent_context=True,
                        user_data_dir=str(profile), executable_path=str(cls.executable),
                        firefox_user_prefs=password_preferences())

    def auth_fixture(self, context, *, rejected=False, challenge=False,
                     suppress_autofill=False, challenge_on_fill=False):
        self.posts = []
        state = {"authenticated": False}
        portal = "<html><meta charset='utf-8'><a href='/daily-checkin-h5/test' target='_blank'>晚安成电</a></html>"
        # Reproduce the school's client handling: leave the visible password
        # intact, disable it and submit a separate encrypted hidden field.
        form = """<html><meta charset='utf-8'><div class='login-main'>
        <form class='m-account' method='POST' action='/authserver/login'
          onsubmit="document.querySelector('#saltPassword').value=btoa(document.querySelector('#password').value);document.querySelector('#password').disabled=true;">
          <input id='username' name='username' autocomplete='off'>
          <input id='password' name='passwordText' type='password' autocomplete='PASSWORD_AUTOCOMPLETE'>
          <input id='saltPassword' name='saltPassword' type='hidden'>
          <button id='login_submit'>登录</button>
        </form><span id='showErrorTip'>ERROR</span>CHALLENGE</div>FILL_CHALLENGE</html>"""
        form = form.replace("PASSWORD_AUTOCOMPLETE", "new-password" if suppress_autofill else "off")
        form = form.replace("FILL_CHALLENGE", """<script>
            document.querySelector('#username').addEventListener('input', () => {
                if (!document.querySelector('#captcha')) {
                    document.querySelector('.m-account').insertAdjacentHTML('beforeend', '<input id="captcha">');
                }
            });
        </script>""" if challenge_on_fill else "")

        def route(intercept):
            request = intercept.request
            if request.url == LOGIN_URL:
                body = portal if state["authenticated"] else f"<script>location.href='{AUTH_ORIGIN}/authserver/login'</script>"
                intercept.fulfill(body=body, content_type="text/html; charset=utf-8")
            elif request.url == AUTH_ORIGIN + "/authserver/login":
                if request.method == "POST":
                    self.posts.append(parse_qs(request.post_data))
                    if not rejected:
                        state["authenticated"] = True
                        intercept.fulfill(body=f"<script>location.href='{LOGIN_URL}'</script>", content_type="text/html")
                        return
                error = "认证未通过" if rejected and self.posts else ""
                captcha = "<input id='captcha'>" if challenge else ""
                intercept.fulfill(body=form.replace("ERROR", error).replace("CHALLENGE", captcha),
                                  content_type="text/html; charset=utf-8")
            elif urlsplit(request.url).hostname == "jzsz.uestc.edu.cn" and "/daily-checkin-h5/" in request.url:
                intercept.fulfill(body="<html><div class='check-in-circle'>fixture</div></html>", content_type="text/html")
            else:
                # Catch all requests, including unexpected paths. Dummy
                # credentials must never reach the school's actual servers.
                intercept.abort()

        context.route("**/*", route)

    def test_native_store_encrypts_credentials_and_summary_contains_no_secrets(self):
        self.assertEqual(self.passwords.saved_count(), 1)
        summary = self.passwords.summary()
        self.assertTrue((self.profile / "key4.db").exists())
        encoded_summary = json.dumps(summary)
        for secret in (FIXTURE_USERNAME, FIXTURE_PASSWORD):
            self.assertNotIn(secret, encoded_summary)
            # Include database journals/backups if present after browser close.
            for path in self.profile.glob("logins*"):
                if path.is_file():
                    self.assertNotIn(secret.encode(), path.read_bytes())

    def test_restart_autofills_and_logs_in_without_environment_credentials(self):
        with self.browser(self.profile) as context:
            self.auth_fixture(context)
            login = PortalLogin(context.new_page(), timeout=15, passwords=self.passwords)
            with patch.dict("os.environ", {}, clear=True):
                page = login.login()
            self.assertIn("/daily-checkin-h5/", page.url)
            self.assertEqual(login.authentication_info(), {"source": "browser_passwords", "attempts": 1})
            self.assertEqual(self.posts, [{"username": [FIXTURE_USERNAME],
                "saltPassword": [base64.b64encode(FIXTURE_PASSWORD.encode()).decode()]}])

    def test_compatible_form_fill_keeps_encrypted_submission_and_submits_once(self):
        with self.browser(self.profile) as context:
            self.auth_fixture(context, suppress_autofill=True)
            login = PortalLogin(context.new_page(), timeout=15, passwords=self.passwords)
            with patch.dict("os.environ", {}, clear=True):
                page = login.login()
            self.assertIn("/daily-checkin-h5/", page.url)
            self.assertEqual(login.authentication_info(), {"source": "browser_passwords", "attempts": 1})
            self.assertEqual(self.posts, [{"username": [FIXTURE_USERNAME],
                "saltPassword": [base64.b64encode(FIXTURE_PASSWORD.encode()).decode()]}])

    def test_challenge_appearing_during_compatible_fill_never_submits(self):
        with self.browser(self.profile) as context:
            self.auth_fixture(context, suppress_autofill=True, challenge_on_fill=True)
            original = context.new_page()
            login = PortalLogin(original, timeout=15, passwords=self.passwords)
            with patch.dict("os.environ", {}, clear=True):
                with self.assertRaisesRegex(CheckinError, "验证码"):
                    login.login()
            self.assertEqual(self.posts, [])
            self.assertEqual(login.login_attempts, 0)
            self.assertEqual(original.locator('[id^=auto-location-autofill-]').count(), 0)

    def test_separate_profile_does_not_inherit_credentials_or_submit(self):
        isolated = Path(self.directory.name) / "isolated-profile"
        with self.browser(isolated) as context:
            passwords = BrowserPasswords(isolated)
            self.assertEqual(passwords.saved_count(), 0)
            self.auth_fixture(context)
            login = PortalLogin(context.new_page(), timeout=15, passwords=passwords)
            with patch.dict("os.environ", {}, clear=True):
                with self.assertRaisesRegex(CheckinError, "credentials"):
                    login.login()
            self.assertEqual(self.posts, [])
            self.assertEqual(login.login_attempts, 0)

    def test_saved_login_rejection_does_not_retry_or_expose_secrets(self):
        with self.browser(self.profile) as context:
            self.auth_fixture(context, rejected=True)
            login = PortalLogin(context.new_page(), timeout=15, passwords=self.passwords)
            with patch.dict("os.environ", {}, clear=True):
                with self.assertRaisesRegex(CheckinError, "未通过") as error:
                    login.login()
            self.assertEqual(len(self.posts), 1)
            for secret in (FIXTURE_USERNAME, FIXTURE_PASSWORD):
                self.assertNotIn(secret, str(error.exception))
                self.assertNotIn(secret, json.dumps(login.authentication_info()))

    def test_saved_login_challenge_does_not_submit(self):
        with self.browser(self.profile) as context:
            self.auth_fixture(context, challenge=True)
            login = PortalLogin(context.new_page(), timeout=15, passwords=self.passwords)
            with patch.dict("os.environ", {}, clear=True):
                with self.assertRaisesRegex(CheckinError, "验证码"):
                    login.login()
            self.assertEqual(self.posts, [])


if __name__ == "__main__":
    unittest.main()
