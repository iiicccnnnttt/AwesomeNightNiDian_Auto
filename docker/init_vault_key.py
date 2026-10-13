"""Initialize a separate secret volume, never replace an existing key."""
import os
import sys
from pathlib import Path

path = Path(sys.argv[1] if len(sys.argv) > 1 else "/run/secrets/vault.key")
path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
if path.exists():
    if len(path.read_bytes()) != 32:
        raise SystemExit("Existing key is invalid; restore it instead of overwriting it.")
    print("Existing vault key preserved.")
else:
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(os.urandom(32))
        stream.flush()
        os.fsync(stream.fileno())
    print("Vault key initialized; no key material printed.")
if os.name != "nt" and os.getuid() == 0:
    os.chown(path.parent, 10001, 10001)
    os.chown(path, 10001, 10001)
    os.chmod(path.parent, 0o700)
    os.chmod(path, 0o600)
