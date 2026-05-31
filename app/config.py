"""Central configuration: filesystem paths and runtime constants."""
from __future__ import annotations

import os
from pathlib import Path

# Project root = parent of the `app` package directory.
BASE_DIR = Path(__file__).resolve().parent.parent

# Directories are overridable via env so the container can mount volumes.
CAPTURES_DIR = Path(os.environ.get("CRYFI_CAPTURES_DIR", BASE_DIR / "captures"))
WORDLISTS_DIR = Path(os.environ.get("CRYFI_WORDLISTS_DIR", BASE_DIR / "wordlists"))
DATA_DIR = Path(os.environ.get("CRYFI_DATA_DIR", BASE_DIR / "data"))
STATIC_DIR = BASE_DIR / "static"

# Ensure the working directories exist on startup.
for _d in (CAPTURES_DIR, WORDLISTS_DIR, DATA_DIR):
    _d.mkdir(parents=True, exist_ok=True)

# Whether to prefix privileged commands with `sudo`. Inside the privileged
# container we run as root, so sudo is unnecessary (and often absent).
USE_SUDO = os.environ.get("CRYFI_USE_SUDO", "0") == "1"

# --- Login / session -----------------------------------------------------
# Default single-user login. Overridable via env; the password can also be
# changed at runtime from the UI (persisted, hashed, to DATA_DIR/auth.json).
AUTH_USER = os.environ.get("CRYFI_USER", "Admin")
AUTH_PASSWORD = os.environ.get("CRYFI_PASSWORD", "Admin")
SESSION_HOURS = float(os.environ.get("CRYFI_SESSION_HOURS", "12"))

# How long (seconds) to wait after SIGTERM before escalating to SIGKILL.
PROC_TERM_GRACE = float(os.environ.get("CRYFI_TERM_GRACE", "3.0"))

# Polling cadence hint surfaced to the frontend (milliseconds).
SCAN_POLL_INTERVAL_MS = int(os.environ.get("CRYFI_SCAN_POLL_MS", "2000"))

# Upload guardrails.
MAX_WORDLIST_BYTES = int(os.environ.get("CRYFI_MAX_WORDLIST_BYTES", str(500 * 1024 * 1024)))

# Wordlist generator limits.
#  - MAX_TARGET: the largest line count a single job may be asked to produce.
#  - DEFAULT_TARGET: used when the request omits a target.
#  - DEDUPE_CAP: in-memory dedupe set ceiling; past this the job keeps streaming
#    WITHOUT dedupe (so size is unbounded by RAM — only disk).
#  - MIN_FREE_BYTES: stop a job if free disk drops below this.
WORDGEN_MAX_TARGET = int(os.environ.get("CRYFI_WORDGEN_MAX_TARGET", str(1_000_000_000_000)))  # 1T
WORDGEN_DEFAULT_TARGET = int(os.environ.get("CRYFI_WORDGEN_DEFAULT_TARGET", str(5_000_000)))
WORDGEN_DEDUPE_CAP = int(os.environ.get("CRYFI_WORDGEN_DEDUPE_CAP", str(20_000_000)))
WORDGEN_MIN_FREE_BYTES = int(os.environ.get("CRYFI_WORDGEN_MIN_FREE_BYTES", str(500 * 1024 * 1024)))
# Kept for the preview sampler; generation no longer uses a single hard cap.
MAX_WORDGEN_LINES = WORDGEN_MAX_TARGET


def sudo_prefix() -> list[str]:
    """Return ``['sudo']`` when sudo escalation is enabled, else ``[]``."""
    return ["sudo"] if USE_SUDO else []
