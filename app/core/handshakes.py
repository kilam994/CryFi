"""Persistent registry of captured WPA handshakes.

When ``airodump-ng`` reports ``WPA handshake: <bssid>`` during a targeted
capture, we record an entry here pointing at the ``.cap`` file that holds it.
The Handshakes tab lists these, and each one can be sent straight to the
dictionary-attack (crack) form.

Stored as a small JSON sidecar in the captures dir so it survives restarts.
``list()`` filters out entries whose ``.cap`` no longer exists on disk.
"""
from __future__ import annotations

import json
from datetime import datetime

from .. import config

_STORE_PATH = config.CAPTURES_DIR / "handshakes.json"


class HandshakeStore:
    def __init__(self) -> None:
        self._items: list[dict] = []
        self._load()

    def _load(self) -> None:
        if _STORE_PATH.exists():
            try:
                self._items = json.loads(_STORE_PATH.read_text())
            except (OSError, ValueError):
                self._items = []

    def _save(self) -> None:
        try:
            _STORE_PATH.write_text(json.dumps(self._items, indent=2))
        except OSError:
            pass

    def add(self, *, bssid: str, essid: str, channel: str | int | None, cap: str) -> dict:
        """Record a handshake; dedup on (bssid, cap)."""
        entry = {
            "bssid": bssid,
            "essid": essid or "",
            "channel": channel,
            "cap": cap,
            "captured_at": datetime.now().isoformat(timespec="seconds"),
        }
        key = (bssid, cap)
        if not any((i["bssid"], i["cap"]) == key for i in self._items):
            self._items.append(entry)
            self._save()
        return entry

    def list(self) -> list[dict]:
        """Return entries whose capture file still exists, newest first."""
        live = [i for i in self._items if (config.CAPTURES_DIR / i["cap"]).is_file()]
        live.sort(key=lambda i: i.get("captured_at", ""), reverse=True)
        return live

    def remove(self, cap: str) -> None:
        before = len(self._items)
        self._items = [i for i in self._items if i["cap"] != cap]
        if len(self._items) != before:
            self._save()


store = HandshakeStore()
