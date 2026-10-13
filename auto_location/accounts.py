import json
import re
from pathlib import Path

from .checkin import CheckinError


def valid_id(value):
    if not isinstance(value, str) or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,47}", value):
        raise CheckinError("account-id 需为 1–48 位字母、数字、下划线或短横线。")
    if value.upper() in {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}:
        raise CheckinError("account-id 不能使用 Windows 保留名称。")
    return value


def load_accounts(path: Path):
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or data.get("version") != 1 or not isinstance(data.get("accounts"), list):
            raise ValueError()
        result = []
        for item in data["accounts"]:
            if not isinstance(item, dict) or set(item) - {"id", "profile", "enabled", "area_index", "auto_login"}:
                raise ValueError()
            ident = valid_id(item["id"])
            if not isinstance(item["profile"], str) or not item["profile"].strip():
                raise ValueError()
            profile = Path(item["profile"])
            profile = (path.resolve().parent / profile).resolve() if not profile.is_absolute() else profile.resolve()
            enabled = item.get("enabled", True)
            auto_login = item.get("auto_login", True)
            index = item.get("area_index")
            if not isinstance(enabled, bool) or not isinstance(auto_login, bool) or (index is not None and (type(index) is not int or index < 0)):
                raise ValueError()
            if any(a["id"].lower() == ident.lower() or a["profile"] == profile
                   or a["profile"] in profile.parents or profile in a["profile"].parents for a in result):
                raise CheckinError("账号 ID 或 profile 重复/嵌套，不能保证隔离。")
            result.append({"id": ident, "profile": profile, "enabled": enabled, "area_index": index, "auto_login": auto_login})
        return result
    except (ValueError, KeyError, TypeError):
        raise CheckinError("账号配置无效；仅保存账号别名、profile、enabled、auto_login 和 area_index，不保存密码。") from None


def account_by_id(path, ident):
    for account in load_accounts(path):
        if account["id"].lower() == valid_id(ident).lower():
            return account
    raise CheckinError("账号尚未注册，请先运行 account-add。")


def add_account(path, ident, profile=None, area_index=None):
    ident = valid_id(ident)
    accounts = load_accounts(path)
    if any(a["id"].lower() == ident.lower() for a in accounts):
        raise CheckinError("账号已注册，不覆盖现有配置。")
    profile = profile.resolve() if profile is not None else path.resolve().parent / "profiles" / ident
    if area_index is not None and area_index < 0:
        raise CheckinError("area-index 必须非负。")
    if any(a["profile"] == profile or a["profile"] in profile.parents or profile in a["profile"].parents for a in accounts):
        raise CheckinError("profile 已被使用或与现有 profile 嵌套。")
    accounts.append({"id": ident, "profile": profile, "enabled": True, "area_index": area_index, "auto_login": True})
    save_accounts(path, accounts)
    return {"status": "account_added", "account_id": ident, "profile": str(profile)}


def save_accounts(path, accounts):
    path.parent.mkdir(parents=True, exist_ok=True)
    value = {"version": 1, "accounts": [{**a, "profile": str(a["profile"])} for a in accounts]}
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(path)


def update_account(path, ident, enabled, auto_login, area_index):
    if type(enabled) is not bool or type(auto_login) is not bool:
        raise CheckinError("启用和自动登录必须为布尔值。")
    if area_index is not None and (type(area_index) is not int or area_index < 0):
        raise CheckinError("区域序号必须为空或非负整数。")
    accounts = load_accounts(path)
    account = next((a for a in accounts if a['id'].lower() == valid_id(ident).lower()), None)
    if account is None:
        raise CheckinError("账号尚未注册。")
    account.update(enabled=enabled, auto_login=auto_login, area_index=area_index)
    save_accounts(path, accounts)


def remove_account(path, ident):
    account = account_by_id(path, ident)
    accounts = load_accounts(path)
    save_accounts(path, [a for a in accounts if a['id'].lower() != account['id'].lower()])
    return account
