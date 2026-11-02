"""Service-Basic keyring (T2 / decision 12).

One AES-GCM keyring outside the database, owned by the -api:

    PROVISION_DIR/keys/service_basic.key   (directory 0700, file 0600)
    {"current": "v1", "keys": {"v1": "<base64 32 bytes>"}}

Creation is atomic and never overwrites: a fresh database creates v1; a
concurrent creator loses the exclusive create and reads the winner's file. If
encrypted rows exist and the keyring is missing, startup refuses — generating a
replacement key would orphan every ciphertext.

Ciphertext envelope: ``v<key_version>:<b64 nonce>:<b64 ciphertext>``. The
version travels with the row so rotation (add v2, re-encrypt, drop old) keeps
old and new rows decryptable during the window.

The gateway reads this same file (read-only); it never writes it.
"""

from __future__ import annotations

import base64
import json
import os
import secrets
import tempfile
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

KEY_BYTES = 32
NONCE_BYTES = 12


class KeyringError(RuntimeError):
    """The keyring is missing, unreadable or cannot decrypt a row."""


def keys_dir() -> Path:
    base = os.environ.get("PROVISION_DIR") or os.environ.get("GENERATED_DIR", "./generated")
    base_path = Path(base)
    # GENERATED_DIR points at .../generated; its parent is PROVISION_DIR.
    if not os.environ.get("PROVISION_DIR") and base_path.name == "generated":
        base_path = base_path.parent
    return base_path / "keys"


def keyring_path() -> Path:
    return keys_dir() / "service_basic.key"


def _new_ring() -> dict:
    return {
        "current": "v1",
        "keys": {"v1": base64.b64encode(secrets.token_bytes(KEY_BYTES)).decode()},
    }


def _write_atomic(path: Path, ring: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path.parent, 0o700)
    except OSError:
        pass
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=".service_basic.", suffix=".key")
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump(ring, fh)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp_name, 0o600)
        # Publish without replacing an existing file: link(2) fails with
        # EEXIST, which is exactly the concurrency guard wanted here.
        os.link(tmp_name, path)
    finally:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass


def load_or_create() -> dict:
    """Return the keyring, creating v1 exactly once on a fresh install."""
    path = keyring_path()
    if path.exists():
        return read()
    ring = _new_ring()
    try:
        _write_atomic(path, ring)
        return ring
    except FileExistsError:
        # Another process won the create race; read what it published.
        return read()


def read() -> dict:
    path = keyring_path()
    try:
        ring = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise KeyringError(f"service Basic keyring missing: {path}") from exc
    except (OSError, ValueError) as exc:
        raise KeyringError(f"service Basic keyring unreadable: {path}: {exc}") from exc
    if not isinstance(ring, dict) or not ring.get("keys") or not ring.get("current"):
        raise KeyringError(f"service Basic keyring malformed: {path}")
    return ring


def _key(ring: dict, version: str) -> bytes:
    value = (ring.get("keys") or {}).get(version)
    if not value:
        raise KeyringError(f"key version {version!r} not in the keyring")
    try:
        raw = base64.b64decode(value)
    except (ValueError, TypeError) as exc:
        raise KeyringError(f"key version {version!r} is not valid base64") from exc
    if len(raw) != KEY_BYTES:
        raise KeyringError(f"key version {version!r} is not {KEY_BYTES} bytes")
    return raw


def encrypt(plaintext: str, *, version: str | None = None) -> str:
    ring = read()
    ver = version or ring["current"]
    nonce = secrets.token_bytes(NONCE_BYTES)
    ct = AESGCM(_key(ring, ver)).encrypt(nonce, plaintext.encode("utf-8"), None)
    return f"v{ver}:{base64.b64encode(nonce).decode()}:{base64.b64encode(ct).decode()}"


def decrypt(envelope: str) -> str:
    try:
        marker, nonce_b64, ct_b64 = envelope.split(":", 2)
    except ValueError as exc:
        raise KeyringError("ciphertext envelope is malformed") from exc
    if not marker.startswith("v"):
        raise KeyringError("ciphertext envelope has no key version")
    ring = read()
    try:
        nonce = base64.b64decode(nonce_b64)
        ct = base64.b64decode(ct_b64)
    except (ValueError, TypeError) as exc:
        raise KeyringError("ciphertext envelope is not valid base64") from exc
    try:
        return AESGCM(_key(ring, marker[1:])).decrypt(nonce, ct, None).decode("utf-8")
    except Exception as exc:  # noqa: BLE001 — a wrong key must fail closed
        raise KeyringError("ciphertext cannot be decrypted with this keyring") from exc


def ensure_for_startup(encrypted_rows: int) -> dict:
    """Create v1 on a fresh database; refuse if ciphertext exists without a key."""
    path = keyring_path()
    if path.exists():
        return read()
    if encrypted_rows:
        raise KeyringError(
            f"{encrypted_rows} encrypted service_basic row(s) exist but {path} is missing; "
            "refusing to generate a replacement key"
        )
    return load_or_create()


def rotate_add(version: str = "v2") -> dict:
    """Add a new key version atomically (additive rotation, never a replace)."""
    path = keyring_path()
    ring = read()
    if version in ring["keys"]:
        return ring
    ring["keys"][version] = base64.b64encode(secrets.token_bytes(KEY_BYTES)).decode()
    # Replace via a temp + rename is safe here: rotation is serialized by the
    # operator tool, and the previous file is complete until the rename.
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=".service_basic.", suffix=".key")
    with os.fdopen(fd, "w") as fh:
        json.dump(ring, fh)
        fh.flush()
        os.fsync(fh.fileno())
    os.chmod(tmp_name, 0o600)
    os.replace(tmp_name, path)
    return ring


def set_current(version: str) -> dict:
    ring = read()
    if version not in ring["keys"]:
        raise KeyringError(f"cannot make unknown version {version!r} current")
    ring["current"] = version
    path = keyring_path()
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=".service_basic.", suffix=".key")
    with os.fdopen(fd, "w") as fh:
        json.dump(ring, fh)
        fh.flush()
        os.fsync(fh.fileno())
    os.chmod(tmp_name, 0o600)
    os.replace(tmp_name, path)
    return ring


def drop_version(version: str) -> dict:
    ring = read()
    if version == ring.get("current"):
        raise KeyringError("cannot drop the current key version")
    ring["keys"].pop(version, None)
    path = keyring_path()
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=".service_basic.", suffix=".key")
    with os.fdopen(fd, "w") as fh:
        json.dump(ring, fh)
        fh.flush()
        os.fsync(fh.fileno())
    os.chmod(tmp_name, 0o600)
    os.replace(tmp_name, path)
    return ring
