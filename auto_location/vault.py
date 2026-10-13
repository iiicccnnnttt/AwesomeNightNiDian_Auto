"""Write-only web credentials; decrypt only inside the login worker."""
import base64
import json
import os
import tempfile
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .accounts import valid_id
from .checkin import CheckinError


def key_file():
    return Path(os.environ.get("AUTO_LOCATION_VAULT_KEY_FILE", ".secrets/vault.key"))


class CredentialVault:
    def __init__(self, root, key_path=None):
        self.root = Path(root)
        self.key_path = Path(key_path) if key_path is not None else key_file()

    def _path(self, ident):
        return self.root / (valid_id(ident).lower() + ".json")

    def _key(self):
        try:
            value = self.key_path.read_bytes()
            if len(value) != 32:
                raise ValueError()
            return value
        except (OSError, ValueError):
            raise CheckinError("凭据密钥不可用；请初始化或恢复独立密钥卷，不要重新生成已有密钥。") from None

    def _aad(self, ident):
        return ("auto-location:v1:idas.uestc.edu.cn:" + valid_id(ident).lower()).encode()

    def configured(self, ident):
        return self._path(ident).is_file()

    def save(self, ident, username, password):
        if not isinstance(username, str) or not username.strip() or len(username) > 256:
            raise CheckinError("账号不能为空或超过 256 字符。")
        if not isinstance(password, str) or not password or len(password) > 4096:
            raise CheckinError("密码不能为空或超过 4096 字符。")
        nonce = os.urandom(12)
        plaintext = json.dumps({"username": username.strip(), "password": password}, ensure_ascii=False).encode()
        ciphertext = AESGCM(self._key()).encrypt(nonce, plaintext, self._aad(ident))
        del plaintext
        value = {"version": 1, "algorithm": "AES-256-GCM",
                 "nonce": base64.b64encode(nonce).decode(), "ciphertext": base64.b64encode(ciphertext).decode()}
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd, temporary = tempfile.mkstemp(prefix=".vault-", dir=self.root)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(value, stream)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self._path(ident))
        finally:
            Path(temporary).unlink(missing_ok=True)

    def read(self, ident):
        try:
            value = json.loads(self._path(ident).read_text(encoding="utf-8"))
            if value["version"] != 1 or value["algorithm"] != "AES-256-GCM":
                raise ValueError()
            nonce = base64.b64decode(value["nonce"], validate=True)
            if len(nonce) != 12:
                raise ValueError()
            ciphertext = base64.b64decode(value["ciphertext"], validate=True)
            plaintext = AESGCM(self._key()).decrypt(nonce, ciphertext, self._aad(ident))
            result = json.loads(plaintext)
            del plaintext
            if set(result) != {"username", "password"} or not all(isinstance(v, str) and v for v in result.values()):
                raise ValueError()
            return result["username"], result["password"]
        except CheckinError:
            raise
        except Exception:
            raise CheckinError("无法解密账号凭据；请检查密钥、密文完整性或重新配置密码。") from None


def remove_browser_logins(profile):
    # Called only after a replacement credential has been durably encrypted,
    # with the account's browser closed. Preserve cookies and its fingerprint.
    for name in ("logins.json", "logins-backup.json", "logins.db", "logins.db-wal", "logins.db-shm", "logins.db-journal"):
        (Path(profile) / name).unlink(missing_ok=True)
