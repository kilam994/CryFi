"""Input sanitization.

Every value that originates from the client and reaches a subprocess call must
pass through one of these validators. We never use ``shell=True`` and always
build commands as argument lists, so even a validator gap cannot yield a shell
metacharacter injection. The regexes below are belt-and-suspenders on top of
that structural defense.
"""
from __future__ import annotations

import re
from pathlib import Path

from fastapi import HTTPException

# --- Patterns -------------------------------------------------------------

_BSSID_RE = re.compile(r"^([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}$")
# Linux iface names: alnum plus a few separators, max 15 chars (IFNAMSIZ-1).
_IFACE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,14}$")
_CHANNEL_RE = re.compile(r"^[0-9]{1,3}$")
_BAND_RE = re.compile(r"^[abgn]{1,4}$")
# Filenames: allow spaces and common punctuation (e.g. "rock you (1).txt").
# Path separators and control chars stay excluded, and clean_filename() also
# enforces value == Path(value).name, which blocks ".." traversal. Spaces are
# safe because every command is run as an argv list (never shell=True).
_FILENAME_RE = re.compile(r"^[A-Za-z0-9 ._()\[\]+,@-]{1,128}$")


def _reject(message: str) -> "HTTPException":
    return HTTPException(status_code=400, detail=message)


def clean_bssid(value: str) -> str:
    value = (value or "").strip().upper()
    if not _BSSID_RE.match(value):
        raise _reject(f"Invalid BSSID: {value!r}")
    return value


def clean_iface(value: str) -> str:
    value = (value or "").strip()
    if not _IFACE_RE.match(value):
        raise _reject(f"Invalid interface name: {value!r}")
    return value


def clean_channel(value: str | int | None) -> str | None:
    if value is None or value == "":
        return None
    value = str(value).strip()
    if not _CHANNEL_RE.match(value):
        raise _reject(f"Invalid channel: {value!r}")
    if not (1 <= int(value) <= 196):
        raise _reject(f"Channel out of range: {value!r}")
    return value


def clean_band(value: str | None) -> str | None:
    if value is None or value == "":
        return None
    value = str(value).strip().lower()
    if not _BAND_RE.match(value):
        raise _reject(f"Invalid band: {value!r}")
    return value


def clean_count(value: str | int | None, default: int = 0, maximum: int = 100000) -> int:
    if value is None or value == "":
        return default
    try:
        n = int(value)
    except (TypeError, ValueError):
        raise _reject(f"Invalid count: {value!r}")
    if not (0 <= n <= maximum):
        raise _reject(f"Count out of range (0-{maximum}): {value!r}")
    return n


def clean_filename(value: str) -> str:
    """Validate a bare filename (no directory component)."""
    value = (value or "").strip()
    if value != Path(value).name or not _FILENAME_RE.match(value):
        raise _reject(f"Invalid filename: {value!r}")
    return value


def safe_path_in(directory: Path, filename: str) -> Path:
    """Resolve ``filename`` strictly inside ``directory`` (block traversal)."""
    name = clean_filename(filename)
    target = (directory / name).resolve()
    root = directory.resolve()
    if root not in target.parents and target != root:
        raise _reject("Path escapes the permitted directory")
    return target
