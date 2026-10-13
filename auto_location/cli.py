from __future__ import annotations

import argparse
import json
import sys
import os
import re
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

from camoufox.sync_api import Camoufox
from camoufox.pkgman import launch_path
from playwright.sync_api import sync_playwright

from .checkin import CheckinError, CheckinPage, Location, SHANGHAI, URL, location_preferences
from .auth import LOGIN_URL, PortalLogin, checkin_page
from .browser_passwords import BrowserPasswords, prepare_password_browser, password_preferences


def write_json(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def uses_vault(args):
    if not args.account_id:
        return False
    from .vault import CredentialVault
    return bool(os.environ.get("AUTO_LOCATION_VAULT_KEY_FILE") or
                CredentialVault(args.accounts.resolve().parent / "credentials").configured(args.account_id))


@contextmanager
def session(args, location):
    if args.connect:
        if location:
            raise ValueError("连接既有实例时不能可靠修改 Camoufox 的启动定位，请用工具启动浏览器。")
        with sync_playwright() as pw:
            browser = pw.firefox.connect(args.connect)
            if len(browser.contexts) != 1:
                raise CheckinError("连接实例必须只有一个浏览器上下文。")
            yield browser.contexts[0]
        return
    secure_credentials = uses_vault(args)
    options = dict(headless=args.headless, persistent_context=True,
                   user_data_dir=str(args.profile.resolve()), geoip=False,
                   locale="zh-CN", timezone_id="Asia/Shanghai",
                   executable_path=str(launch_path() if secure_credentials else prepare_password_browser()))
    if args.account_id:
        from .identities import profile_identity
        identity = profile_identity(args.profile.resolve(), Path(options["executable_path"]))
        args.fingerprint_id = identity["id"]
        options["config"] = dict(identity["config"])
        options["i_know_what_im_doing"] = True
    prefs = password_preferences()
    if secure_credentials:
        prefs.update({"signon.rememberSignons": False, "signon.storeSignons": False, "signon.autofillForms": False})
    prefs["geo.enabled"] = True
    if args.auto_location or args.command == "discover" or (args.account_id and args.command == "login"):
        prefs.update({"geo.enabled": False, "geo.provider.ms-windows-location": False})
    elif not location and os.name == "nt":
        # A profile used for supplied coordinates must not replay its old local
        # provider when the next run requests a real initial location.
        prefs.update({"geo.provider.testing": False,
                      "geo.provider.ms-windows-location": True,
                      "geo.provider.network.url": "", "geo.wifi.scan": False})
    if location:
        options["geolocation"] = {"latitude": location.latitude, "longitude": location.longitude,
                                  "accuracy": location.accuracy}
        prefs.update(location_preferences(location))
        options["config"] = {**options.get("config", {}), "geolocation:latitude": location.latitude,
                             "geolocation:longitude": location.longitude,
                             "geolocation:accuracy": location.accuracy}
    options["firefox_user_prefs"] = prefs
    if (secure_credentials and args.auto_login and not args.headless
            and not getattr(args, 'web_auth', None) and not getattr(args, 'interactive_login', False)):
        # Never render credentials on the monitored display. Authenticate in
        # a headless session, then reopen the persisted cookies visibly.
        from .vault import CredentialVault
        vault = CredentialVault(args.accounts.resolve().parent / "credentials")
        with Camoufox(**{**options, "headless": True}) as initial:
            if args.force_login:
                initial.clear_cookies(domain=re.compile(r"(^|\.)uestc\.edu\.cn$"))
            authentication = PortalLogin(initial.new_page(), args.timeout, allow_environment=False,
                                         credential_loader=lambda: vault.read(args.account_id))
            authentication.login()
            args.private_authentication = authentication.authentication_info()
    if sys.platform.startswith("linux") and not args.headless and not (args.profile / "prefs.js").exists():
        # Initialize a fresh Linux profile before creating its first visible
        # window. Cold headed startup can exceed Juggler's window-ready timeout.
        with Camoufox(**{**options, "headless": True}) as initial:
            initial.new_page().goto("about:blank")
    with Camoufox(**options) as context:
        try:
            yield context
        finally:
            if args.keep_open:
                try:
                    input("Camoufox 窗口保持打开，供监控和检查；在终端按回车后关闭：")
                except EOFError:
                    pass  # Preserve operation evidence when stdin is unavailable.


def parser():
    p = argparse.ArgumentParser(description="晚安成电 Camoufox 定位采集、打卡及结果核验")
    p.add_argument("command", choices=["credentials", "login", "capture", "discover", "inspect", "checkin", "verify",
                                       "account-add", "account-list", "batch-login", "batch-inspect", "batch-checkin"])
    p.add_argument("--profile", type=Path, help="单账号默认 .state/camoufox；注册账号时默认独立目录")
    p.add_argument("--accounts", type=Path, default=Path(".state/accounts.json"), help="批量账号清单（仅别名和 profile 等配置）")
    p.add_argument("--account-id", help="已注册账号别名；account-add 时为新别名")
    p.add_argument("--location", type=Path, help="WGS84 定位 JSON，用于 inspect/checkin/verify")
    p.add_argument("--auto-location", action="store_true", help="读取账户允许区域，通过网站 GCJ02 路由使用区域内随机位置（非实时定位）")
    p.add_argument("--area-index", type=int, help="discover 返回的区域序号；多个区域时必须指定")
    p.add_argument("--output", type=Path, help="capture 默认 .state/location.json；credentials 默认 .state/credentials-result.json；其他命令默认 .state/result.json")
    p.add_argument("--connect", help="Camoufox Playwright WebSocket 地址；不是 CDP/调试端口")
    p.add_argument("--url", default=URL)
    p.add_argument("--login-url", default=LOGIN_URL, help="统一登录入口，默认 jzsz.uestc.edu.cn 根页面")
    p.add_argument("--auto-login", action="store_true", help="复用会话或使用浏览器保存的凭据自动登录；环境变量作为兼容来源")
    p.add_argument("--force-login", action="store_true", help="清除本 profile 的学校登录 Cookie，验证完整自动登录；需配合 auto-login")
    p.add_argument("--simulate-miniprogram", action="store_true",
                   help="实验：仅将打卡 POST 的 platform 请求头改为 mp-weixin，仍刷新核验真实结果")
    p.add_argument("--timeout", type=float, default=60)
    p.add_argument("--headless", action="store_true")
    p.add_argument("--web-auth", type=Path, help="网页人工二次验证状态文件；输入操作来自 stdin")
    p.add_argument("--interactive-login", action="store_true", help="本机 GUI 自动填充登录，人工完成首次验证并信任设备")
    p.add_argument("--keep-open", action="store_true", help="操作结束后保留 Camoufox 可见窗口，按回车关闭")
    return p


def report_failure(args, output: Path, exc: BaseException, bot: CheckinPage | None = None,
                   authentication: PortalLogin | None = None):
    result = {"status": "failed", "error": str(exc) or type(exc).__name__,
              "time": datetime.now(SHANGHAI).isoformat()}
    if args.account_id:
        result["account_id"] = args.account_id
    if getattr(args, "fingerprint_id", None):
        result["fingerprint_id"] = args.fingerprint_id
    if bot is not None:
        result.update(record=bot.record, submission=bot.submit_result)
        if bot.areas is not None:
            result["allowed_areas"] = bot.areas
        if bot.location_resolution is not None:
            result["location_resolution"] = bot.location_resolution
        if bot.platform_simulation is not None:
            result["platform_simulation"] = dict(bot.platform_simulation)
    if authentication is not None:
        result["authentication"] = authentication.authentication_info()
    failure_output = output.with_name(output.stem + ".error.json") if args.command == "capture" or args.command.startswith("batch-") else output
    write_json(failure_output, result)
    print(json.dumps(result, ensure_ascii=False, indent=2), file=sys.stderr, flush=True)
    return 1


def main(argv=None):
    args = parser().parse_args(argv)
    defaults = {"capture": ".state/location.json", "credentials": ".state/credentials-result.json",
                "discover": ".state/allowed-areas.json", "account-add": ".state/account-management-result.json",
                "account-list": ".state/account-management-result.json",
                "batch-login": ".state/batch-result.json", "batch-inspect": ".state/batch-result.json",
                "batch-checkin": ".state/batch-result.json"}
    output = args.output or Path(defaults.get(args.command, ".state/result.json"))
    try:
        if args.timeout <= 0:
            raise ValueError("timeout 必须大于 0。")
        if args.web_auth and (args.command != 'login' or not args.auto_login or not args.account_id
                              or args.headless or args.connect or not uses_vault(args)):
            raise ValueError('网页验证仅支持使用加密凭据的注册账号 GUI 自动登录。')
        if args.interactive_login and (args.command != 'login' or not args.auto_login or not args.account_id
                                      or args.headless or args.connect or args.web_auth or not uses_vault(args)):
            raise ValueError('本机验证仅支持使用加密凭据的注册账号 GUI 自动登录。')
        from .accounts import add_account, load_accounts, account_by_id
        if args.command.startswith("account-"):
            if args.command == "account-add":
                result = add_account(args.accounts, args.account_id, args.profile, args.area_index)
            else:
                from .vault import CredentialVault
                credential_vault = CredentialVault(args.accounts.resolve().parent / "credentials")
                saved_count = lambda a: int(credential_vault.configured(a["id"])) if (
                    os.environ.get("AUTO_LOCATION_VAULT_KEY_FILE") or credential_vault.configured(a["id"])) else BrowserPasswords(a["profile"]).saved_count()
                result = {"status": "accounts_listed", "accounts": [
                    {**a, "profile": str(a["profile"]), "saved_credentials": saved_count(a)}
                    for a in load_accounts(args.accounts)]}
            write_json(output, result)
            print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)
            return 0
        if args.command.startswith("batch-"):
            if (args.account_id or args.profile or args.location or args.auto_location or args.area_index is not None
                    or args.force_login or args.connect or args.keep_open or args.url != URL or args.login_url != LOGIN_URL):
                raise ValueError("批量命令使用账号清单及独立 profile，不能传入单账号配置或 keep-open；各账号 area_index 在清单中配置。")
            if args.simulate_miniprogram and args.command != "batch-checkin":
                raise ValueError("simulate-miniprogram 仅用于 checkin/batch-checkin。")
            from .batch import run_batch
            result = run_batch(args, output, write_json)
            print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)
            return 0 if result["status"] == "completed" else 1
        if args.account_id:
            if args.profile or args.connect:
                raise ValueError("account-id 已指定独立 profile，不能与 profile/connect 同用。")
            account = account_by_id(args.accounts, args.account_id)
            args.profile = account["profile"]
            if uses_vault(args) and args.command == "credentials":
                raise ValueError("安全模式禁止打开账号密码管理器；请在网页仅写入的凭据表单配置。")
        else:
            args.profile = args.profile or Path(".state/camoufox")
        if args.auto_location and (args.location or args.command not in ("inspect", "checkin", "verify")):
            raise ValueError("auto-location 仅用于 inspect/checkin/verify，不能与 location 同用。")
        if args.command == "discover" and args.location:
            raise ValueError("discover 不需要定位覆盖。")
        if args.area_index is not None and not args.auto_location:
            raise ValueError("area-index 需要配合 auto-location。")
        if args.connect and (args.auto_location or args.command == "discover"):
            raise ValueError("区域发现模式需要工具启动浏览器，以禁用系统定位。")
        if args.keep_open and (args.headless or args.connect):
            raise ValueError("keep-open 需要工具启动的可见浏览器，不能与 headless/connect 同用。")
        if args.command in ("credentials", "login", "capture") and args.location:
            raise ValueError("credentials/login/capture 不能传入定位覆盖。")
        if args.headless and (args.command in ("credentials", "capture") or (args.command == "login" and not args.auto_login)):
            raise ValueError("credentials、手动 login 和 capture 需要可见浏览器。")
        if args.command == "credentials" and (args.connect or args.auto_login):
            raise ValueError("credentials 使用本地 profile 的浏览器密码管理器，不能与 connect/auto-login 同用。")
        if args.force_login and not args.auto_login:
            raise ValueError("force-login 需要配合 auto-login。")
        if args.force_login and args.connect:
            raise ValueError("force-login 只用于工具启动的本地 profile。")
        if args.auto_login and args.login_url != LOGIN_URL:
            raise ValueError("自动登录只支持学校官方统一认证入口。")
        if args.simulate_miniprogram and args.command != "checkin":
            raise ValueError("simulate-miniprogram 只用于 checkin，不修改登录、定位或查询请求。")
        location = Location(**json.loads(args.location.read_text(encoding="utf-8"))) if args.location else None
        with session(args, location) as context:
            bot = None
            authentication = None
            try:
                origin = urlsplit(args.url)
                if not args.auto_location and args.command != "discover":
                    context.grant_permissions(["geolocation"], origin=f"{origin.scheme}://{origin.netloc}")
                matches = [p for p in context.pages if "daily-checkin-h5" in p.url]
                page = matches[0] if args.connect and matches else context.new_page()
                passwords = BrowserPasswords(args.profile)
                if args.command == "credentials":
                    passwords.open_editor(page)
                    input("请在浏览器密码管理器中填写学校账号和密码并点击 Save/保存；完成后在终端按回车：")
                    result = passwords.summary()
                elif args.command == "login" and not args.auto_login:
                    page.goto(args.login_url, wait_until="domcontentloaded")
                    input("请在 Camoufox 中完成统一认证，再从平台打开“晚安成电”；进入今日打卡页面后按回车保存会话：")
                    selected = checkin_page(context, page)
                    page = selected
                    bot = CheckinPage(page, args.url, args.timeout)
                    bot.open()
                    result = {"status": "session_saved", **bot.inspect()}
                else:
                    if args.force_login and not getattr(args, "private_authentication", None):
                        context.clear_cookies(domain=re.compile(r"(^|\.)uestc\.edu\.cn$"))
                    if args.auto_login:
                        secure_credentials = uses_vault(args)
                        loader = None
                        if secure_credentials:
                            from .vault import CredentialVault
                            vault = CredentialVault(args.accounts.resolve().parent / "credentials")
                            loader = lambda: vault.read(args.account_id)
                        interaction = None
                        if args.web_auth:
                            from .web_auth import WebAuthentication
                            interaction = WebAuthentication(args.web_auth)
                        elif args.interactive_login:
                            from .local_auth import LocalAuthentication
                            interaction = LocalAuthentication()
                        authentication = PortalLogin(page, args.timeout,
                                                     passwords=None if args.connect or secure_credentials else passwords,
                                                     allow_environment=not bool(args.account_id),
                                                     credential_loader=loader,
                                                     session_only=bool(getattr(args, "private_authentication", None)),
                                                     interaction=interaction)
                        page = authentication.login()
                    bot = CheckinPage(page, args.url, args.timeout, args.simulate_miniprogram)
                    bot.open()
                    area_evidence = bot.use_area(args.area_index) if args.auto_location else None
                    if args.command == "login":
                        result = {"status": "session_saved", **bot.inspect()}
                    elif args.command == "capture":
                        result = bot.capture_location().to_dict()
                    elif args.command == "discover":
                        result = {"status": "discovered", **bot.discover_areas()}
                    elif args.command == "inspect":
                        result = {"status": "inspected", **bot.inspect()}
                    elif args.command == "checkin":
                        result = bot.checkin()
                    else:
                        result = bot.verify()
                    if area_evidence is not None:
                        result["location_resolution"] = area_evidence
                if authentication is not None:
                    result["authentication"] = getattr(args, "private_authentication", authentication.authentication_info())
                if args.account_id:
                    result["account_id"] = args.account_id
                    result["fingerprint_id"] = args.fingerprint_id
                write_json(output, result)
                print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)
                if args.web_auth and authentication is not None:
                    authentication.interaction.review(page)
                return 0
            except (Exception, KeyboardInterrupt) as exc:
                # Persist server evidence before keep-open waits for input.
                code = report_failure(args, output, exc, bot, authentication)
                if args.web_auth and authentication is not None and str(exc) != '用户取消了登录验证。':
                    try:
                        authentication.interaction.review(page, failed=True)
                    except Exception:
                        pass  # Closed/crashed browsers cannot retain a GUI.
                return code
    except (Exception, KeyboardInterrupt) as exc:
        return report_failure(args, output, exc)
