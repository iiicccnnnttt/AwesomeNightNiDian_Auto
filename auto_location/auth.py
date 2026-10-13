from __future__ import annotations

import os
import time
import uuid
from urllib.parse import urlsplit

from playwright.sync_api import Page, Error as PlaywrightError

from .checkin import CheckinError
from .browser_passwords import BrowserPasswords

LOGIN_URL = "https://jzsz.uestc.edu.cn/"
PORTAL_HOST = "jzsz.uestc.edu.cn"
AUTH_HOST = "idas.uestc.edu.cn"
USERNAME = ".login-main .m-account #username"
PASSWORD = ".login-main .m-account #password"
SUBMIT = ".login-main #login_submit"


def checkin_page(context, fallback: Page) -> Page:
    pages = [page for page in context.pages if not page.is_closed()
             and urlsplit(page.url).hostname == PORTAL_HOST
             and urlsplit(page.url).path.startswith("/daily-checkin-h5/")]
    return pages[-1] if pages else fallback


class PortalLogin:
    def __init__(self, page: Page, timeout: float = 60, passwords: BrowserPasswords | None = None,
                 allow_environment: bool = True, credential_loader=None, session_only=False, interaction=None):
        self.page, self.timeout = page, timeout
        self.passwords = passwords
        self.allow_environment = allow_environment
        self.credential_loader = credential_loader
        self.session_only = session_only
        self.interaction = interaction
        self.credential_source = "session"
        self.login_attempts = 0
        page.set_default_timeout(timeout * 1000)

    def authentication_info(self) -> dict:
        return {"source": self.credential_source, "attempts": self.login_attempts}

    def _credentials_filled(self):
        # Only booleans leave the browser. Do not read/decrypt saved passwords
        # into Python, logs, screenshots, or result files.
        return (self.page.locator(USERNAME).evaluate("el => Boolean(el.value.trim())")
                and self.page.locator(PASSWORD).evaluate("el => Boolean(el.value)"))

    def _fill_credentials(self):
        if self.session_only:
            raise CheckinError("认证会话已失效；请重新运行自动登录。可见浏览器不填写密码。")
        if self.credential_loader is not None:
            username, password = self.credential_loader()
            try:
                self.page.locator(PASSWORD).evaluate("el => {el.type='password';}")
                self.page.locator(USERNAME).fill(username)
                self.page.locator(PASSWORD).fill(password)
                self.credential_source = "encrypted_vault"
                return
            except Exception:
                raise CheckinError("填写加密凭据失败；未提交登录。") from None
            finally:
                del username, password
        if self._credentials_filled():
            self.credential_source = "browser_passwords"
            return
        if self.passwords is not None and self.passwords.saved_count():
            self.credential_source = "browser_passwords"
            # Some CAS forms are not recognized for automatic filling even
            # though the browser offers a saved-login candidate on focus.
            try:
                self._wait(self._credentials_filled, "等待浏览器自动填充。",
                           timeout=min(2, self.timeout))
            except CheckinError:
                self._browser_compatible_fill()
            return
        if not self.allow_environment:
            raise CheckinError("此账号 profile 没有浏览器保存的凭据；请用 credentials --account-id 初始化，或手动登录。")
        username = os.environ.get("UESTC_USERNAME", "").strip()
        password = os.environ.get("UESTC_PASSWORD", "")
        if not username or not password:
            raise CheckinError("请先运行 credentials，在浏览器中保存学校账号密码；也可使用本地环境变量 UESTC_USERNAME 和 UESTC_PASSWORD。")
        try:
            self.page.locator(USERNAME).fill(username)
            self.page.locator(PASSWORD).fill(password)
            self.credential_source = "environment"
        except Exception:
            # Playwright fill errors can echo arguments. Never propagate them.
            raise CheckinError("填写统一认证失败，请手动登录。") from None
        finally:
            del username, password

    def _browser_compatible_fill(self):
        if not self._trusted_auth():
            raise CheckinError("认证页面地址不匹配，未填写账号密码。")
        assert self.passwords is not None
        if self.passwords.saved_count() != 1:
            raise CheckinError("此 profile 的学校凭据不唯一；请在浏览器中保留一个账号，或使用独立 profile。")
        key = "auto-location-autofill-" + uuid.uuid4().hex
        try:
            # Give the native password manager a standard form on the same
            # trusted origin. Its decrypted values stay entirely in the page;
            # Python only receives whether filling has finished. Keep the
            # school's original inputs/listeners and encryption handler intact.
            self.page.evaluate("""key => {
                const form = document.createElement('form');
                form.id = key;
                form.method = 'post';
                form.action = location.origin + '/authserver/login';
                form.style.cssText = 'position:fixed;left:-10000px;top:0';
                form.setAttribute('aria-hidden', 'true');
                form.innerHTML = '<input name="username" autocomplete="username" tabindex="-1">'
                  + '<input name="password" type="password" autocomplete="current-password" tabindex="-1">'
                  + '<button type="button" tabindex="-1">Login</button>';
                form.addEventListener('submit', event => event.preventDefault());
                document.body.append(form);
            }""", key)
            self._wait(lambda: self.page.locator("#" + key).evaluate("""form => Boolean(
                form.querySelector('[name=username]').value.trim()
                && form.querySelector('[name=password]').value)"""),
                "浏览器未能自动填充已保存凭据；请检查主密码是否已解锁。")
            self.page.evaluate("""({key, username, password}) => {
                const form = document.getElementById(key);
                for (const [selector, name] of [[username, 'username'], [password, 'password']]) {
                    const input = document.querySelector(selector);
                    input.value = form.querySelector('[name=' + name + ']').value;
                    input.dispatchEvent(new Event('input', {bubbles:true}));
                    input.dispatchEvent(new Event('change', {bubbles:true}));
                }
            }""", {"key": key, "username": USERNAME, "password": PASSWORD})
        except CheckinError:
            raise
        except Exception:
            raise CheckinError("浏览器兼容填充失败，请检查已保存凭据。") from None
        finally:
            try:
                self.page.evaluate("key => document.getElementById(key)?.remove()", key)
            except Exception:
                pass
        if not self._credentials_filled():
            raise CheckinError("浏览器未完成账号密码填充，未提交登录。")

    def _portal_ready(self):
        parsed = urlsplit(self.page.url)
        return (parsed.scheme == "https" and parsed.hostname == PORTAL_HOST
                and self.page.get_by_text("晚安成电", exact=True).first.is_visible())

    def _wait(self, predicate, message, timeout=None):
        deadline = time.monotonic() + (self.timeout if timeout is None else timeout)
        while time.monotonic() < deadline:
            try:
                if predicate():
                    return
            except PlaywrightError as exc:
                # CAS redirects can destroy the old document between the URL
                # and DOM checks. Wait for the next document; never resubmit.
                if "Execution context was destroyed" not in str(exc):
                    raise
            self.page.wait_for_timeout(200)
        raise CheckinError(message)

    def _trusted_auth(self):
        parsed = urlsplit(self.page.url)
        return (parsed.scheme == "https" and parsed.hostname == AUTH_HOST
                and parsed.path == "/authserver/login")

    def _challenge(self):
        for selector in (".login-main #captcha:visible", "#sliderCaptchaDiv > *:visible",
                         ".login-main .m-phone #dynamicCode:visible"):
            if self.page.locator(selector).count():
                return True
        return False

    def login(self) -> Page:
        try:
            return self._login()
        finally:
            if self.interaction:
                self.interaction.close()

    def _login(self) -> Page:
        self.page.goto(LOGIN_URL, wait_until="domcontentloaded")
        self._wait(lambda: self._portal_ready() or self.page.locator(USERNAME).is_visible(),
                   "未识别到平台或统一认证页面；请使用 login 手动登录。")
        if not self._portal_ready():
            if not self._trusted_auth():
                raise CheckinError("认证页面地址不匹配，未填写账号密码。")
            if self._challenge() and not self.interaction:
                raise CheckinError("统一认证需要验证码或二次认证；请使用 login 人工完成。")
            self._fill_credentials()
            if self._challenge():
                if not self.interaction:
                    raise CheckinError("统一认证需要验证码或二次认证；请使用 login 人工完成。")
                self._wait(lambda: self.interaction.tick(self.page, before_submit=True),
                           "等待人工验证码超时；未提交登录。")
            try:
                # Use the site's button so its password encryption and CAS form
                # handling remain intact. Never submit a raw password API request.
                self.login_attempts += 1
                self.page.locator(SUBMIT).click()
            except Exception:
                raise CheckinError("提交统一认证失败，请手动登录。") from None

            def outcome():
                if self._portal_ready():
                    return True
                if self.interaction:
                    self.interaction.tick(self.page)
                if self._challenge() and not self.interaction:
                    raise CheckinError("统一认证需要验证码或二次认证；请使用 login 人工完成。")
                errors = self.page.locator(".login-main #showErrorTip:visible, .login-main .item-error-tip:visible")
                if any(value.strip() for value in errors.all_text_contents()):
                    raise CheckinError("统一认证未通过；请检查账号密码。未自动重试。")
                return False

            self._wait(outcome, "未确认登录成功；请手动检查认证页面。未自动重试。")

        existing = set(self.page.context.pages)
        self.page.get_by_text("晚安成电", exact=True).first.click()
        selected = None

        def app_opened():
            nonlocal selected
            for candidate in self.page.context.pages:
                parsed = urlsplit(candidate.url)
                if (parsed.hostname == PORTAL_HOST and parsed.path.startswith("/daily-checkin-h5/")
                        and (candidate not in existing or candidate is self.page)):
                    selected = candidate
                    return True
            return False

        self._wait(app_opened, "登录已完成，但未能从平台打开“晚安成电”。")
        assert selected is not None
        selected.wait_for_load_state("domcontentloaded")
        # Give the app's initial token/bootstrap flow time to finish before the
        # check-in driver navigates to its canonical URL.
        selected.locator(".check-in-circle, .location-pill.success, .status-free").first.wait_for(
            state="visible", timeout=self.timeout * 1000)
        return selected
