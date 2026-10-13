"""Persist only Camoufox fingerprint config, never launch environment/secrets."""
import json
import uuid
import sys
from pathlib import Path

from camoufox.utils import launch_options

from .checkin import CheckinError


def profile_identity(profile: Path, executable: Path):
    path = profile / ".auto-location-identity.json"
    if path.exists():
        try:
            identity = json.loads(path.read_text(encoding="utf-8"))
            if identity["version"] != 1 or not isinstance(identity["config"], dict) or not identity["config"].get("navigator.userAgent"):
                raise ValueError()
            uuid.UUID(identity["id"])
            return identity
        except (ValueError, KeyError, TypeError):
            raise CheckinError("账号指纹文件无效，未自动重建或更换身份。") from None
    target_os = "windows" if sys.platform == "win32" else "macos" if sys.platform == "darwin" else "linux"
    options = launch_options(os=target_os, locale="zh-CN", geoip=False,
                             executable_path=executable, headless=False)
    chunks = sorted((int(key.rsplit("_", 1)[1]), value) for key, value in options["env"].items()
                    if key.startswith("CAMOU_CONFIG_"))
    config = json.loads("".join(value for _, value in chunks))
    # No OS environment, profile cookies, credentials or location is persisted.
    config = {key: value for key, value in config.items() if not key.startswith("geolocation:")}
    identity = {"version": 1, "id": str(uuid.uuid4()), "config": config}
    profile.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as stream:
        json.dump(identity, stream, ensure_ascii=False, indent=2)
    return identity
