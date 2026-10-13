from __future__ import annotations

import json
import shutil
import sqlite3
from contextlib import closing
from pathlib import Path

from camoufox.pkgman import get_path, launch_path
from playwright.sync_api import Page

from .checkin import CheckinError

AUTH_ORIGIN = "https://idas.uestc.edu.cn"


def prepare_password_browser(runtime_root: Path = Path(".state/browser-runtime")) -> Path:
    """Enable Firefox's password manager in a project-owned runtime copy."""
    source = Path(get_path(""))
    target = runtime_root.resolve() / source.name
    executable = target / Path(launch_path()).name
    marker = target / ".password-manager-ready"
    if marker.exists() and executable.exists():
        return executable
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, target, dirs_exist_ok=True)
    policy_path = target / "distribution" / "policies.json"
    policy = json.loads(policy_path.read_text(encoding="utf-8")) if policy_path.exists() else {"policies": {}}
    policy.setdefault("policies", {}).update(PasswordManagerEnabled=True, OfferToSaveLogins=True)
    policy_path.parent.mkdir(parents=True, exist_ok=True)
    policy_path.write_text(json.dumps(policy, ensure_ascii=False, indent=2), encoding="utf-8")
    if not executable.exists():
        raise CheckinError("项目浏览器运行目录中没有找到 Camoufox 可执行文件。")
    marker.write_text(source.name, encoding="utf-8")
    return executable


def password_preferences() -> dict:
    return {"signon.rememberSignons": True, "signon.storeSignons": True,
            "signon.autofillForms": True, "signon.autofillForms.autocompleteOff": True,
            "signon.storeWhenAutocompleteOff": True, "signon.formlessCapture.enabled": True,
            "browser.privatebrowsing.autostart": False}


class BrowserPasswords:
    """Inspect encrypted metadata; leave encryption and filling to Firefox."""

    def __init__(self, profile: Path):
        self.profile = profile.resolve()

    def saved_count(self) -> int:
        database = self.profile / "logins.db"
        if database.exists():
            try:
                # Current Firefox uses encrypted secFields in SQLite. Only
                # count metadata; never retrieve or decrypt credential values.
                with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as connection:
                    return connection.execute("""
                        SELECT COUNT(*) FROM (
                            SELECT guid FROM loginsL
                            WHERE origin=? AND is_deleted=0 AND secFields IS NOT NULL AND secFields<>''
                            UNION
                            SELECT guid FROM loginsM AS mirror
                            WHERE origin=? AND is_overridden=0 AND secFields IS NOT NULL AND secFields<>''
                              AND NOT EXISTS (SELECT 1 FROM loginsL AS local WHERE local.guid=mirror.guid)
                        )
                    """, (AUTH_ORIGIN, AUTH_ORIGIN)).fetchone()[0]
            except sqlite3.Error:
                raise CheckinError("无法读取浏览器的加密登录信息，请在密码管理器中检查。") from None
        # Older Firefox releases use the encrypted JSON store.
        path = self.profile / "logins.json"
        if not path.exists():
            return 0
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            return sum(entry.get("hostname") == AUTH_ORIGIN
                       and bool(entry.get("encryptedUsername")) and bool(entry.get("encryptedPassword"))
                       for entry in payload.get("logins", []))
        except (OSError, ValueError, TypeError, AttributeError):
            raise CheckinError("无法读取浏览器的加密登录信息，请在密码管理器中检查。") from None

    def open_editor(self, page: Page):
        try:
            page.goto("about:logins", wait_until="domcontentloaded")
            if self.saved_count():
                # Existing credentials stay masked; the user manages them here.
                return
            page.locator("create-login-button, .create-login-button").first.click()
            page.locator("input[name=origin]").fill(AUTH_ORIGIN)
        except Exception:
            raise CheckinError("无法打开浏览器密码管理器，请使用项目专用 Camoufox 运行目录。") from None

    def summary(self) -> dict:
        count = self.saved_count()
        if not count or not (self.profile / "key4.db").exists():
            raise CheckinError("浏览器尚未加密保存学校凭据；请在密码管理器中填写并点击保存。")
        return {"status": "credentials_saved", "origin": AUTH_ORIGIN,
                "credential_count": count, "storage": "Firefox password manager",
                "credential_store": "logins.db" if (self.profile / "logins.db").exists() else "logins.json",
                "profile": str(self.profile)}
