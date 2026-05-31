"""Minimal single-user session auth with a runtime-changeable password.

Credentials come from env by default. When the password is changed from the UI
it's stored — salted + PBKDF2-hashed — in ``DATA_DIR/auth.json`` and takes
precedence over the env defaults. Sessions are opaque random tokens held
server-side with an expiry.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import time

from .. import config

COOKIE_NAME = "cryfi_session"
_PBKDF2_ROUNDS = 100_000

_SESSIONS: dict[str, float] = {}   # token -> created (monotonic seconds)
_CREDS: dict | None = None         # {user, salt, hash} once loaded/changed
_LOADED = False


def _creds_path():
    return config.DATA_DIR / "auth.json"


def _hash(password: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), _PBKDF2_ROUNDS).hex()


def _load_creds() -> dict | None:
    global _CREDS, _LOADED
    if not _LOADED:
        _LOADED = True
        try:
            _CREDS = json.loads(_creds_path().read_text())
        except (OSError, ValueError):
            _CREDS = None
    return _CREDS


def current_user() -> str:
    c = _load_creds()
    return c["user"] if c else config.AUTH_USER


def check_credentials(user: str, password: str) -> bool:
    """Constant-time check against the stored (hashed) or env (plain) creds."""
    c = _load_creds()
    if c:
        u_ok = hmac.compare_digest(user or "", c["user"])
        p_ok = hmac.compare_digest(_hash(password or "", c["salt"]), c["hash"])
        return u_ok and p_ok
    u_ok = hmac.compare_digest(user or "", config.AUTH_USER)
    p_ok = hmac.compare_digest(password or "", config.AUTH_PASSWORD)
    return u_ok and p_ok


def set_password(new_password: str, user: str | None = None) -> None:
    """Persist a new (hashed) password, keeping the current username by default."""
    global _CREDS
    salt = secrets.token_hex(16)
    keep_user = user or current_user()
    _CREDS = {"user": keep_user, "salt": salt, "hash": _hash(new_password, salt)}
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    _creds_path().write_text(json.dumps(_CREDS))


# --- sessions ------------------------------------------------------------

def create_session() -> str:
    token = secrets.token_urlsafe(32)
    _SESSIONS[token] = time.monotonic()
    return token


def valid_session(token: str | None) -> bool:
    if not token:
        return False
    created = _SESSIONS.get(token)
    if created is None:
        return False
    if (time.monotonic() - created) > config.SESSION_HOURS * 3600:
        _SESSIONS.pop(token, None)
        return False
    return True


def destroy_session(token: str | None) -> None:
    if token:
        _SESSIONS.pop(token, None)
