"""Captured-handshake listing + cleanup + hidden-SSID recovery.

Backs the Handshakes tab: each entry points at a ``.cap`` known to contain a
WPA handshake, ready to feed into the dictionary-attack form. A handshake on a
hidden network still carries the SSID in the captured association frames, so
``aircrack-ng`` can recover it from the .cap.
"""
from __future__ import annotations

import asyncio
import re

from fastapi import APIRouter, HTTPException

from .. import config
from ..core.handshakes import store
from ..core.security import clean_filename, safe_path_in

router = APIRouter(prefix="/api/handshakes", tags=["handshakes"])

_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
# aircrack-ng network table row: "  1  AA:BB:..:FF  MyESSID            WPA (1 handshake)"
_ROW_RE = re.compile(
    r"^\s*\d+\s+([0-9A-Fa-f:]{17})\s+(.*?)\s{2,}(?:WPA\d?|WEP|OPN|None|Unknown)",
    re.MULTILINE,
)


@router.get("")
async def list_handshakes() -> dict:
    return {"handshakes": store.list()}


async def _extract_essid(cap_path, bssid: str) -> str:
    """Run aircrack-ng on the cap and pull the ESSID for ``bssid`` from its table."""
    try:
        proc = await asyncio.create_subprocess_exec(
            *config.sudo_prefix(), "aircrack-ng", str(cap_path),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=25.0)
    except (asyncio.TimeoutError, FileNotFoundError, OSError):
        return ""
    text = _ANSI_RE.sub("", out.decode(errors="replace"))
    for m in _ROW_RE.finditer(text):
        if m.group(1).upper() == bssid.upper():
            return m.group(2).strip()
    return ""


@router.post("/{cap}/reveal")
async def reveal_handshake_ssid(cap: str) -> dict:
    """Recover a hidden network's SSID from its captured handshake."""
    name = clean_filename(cap)
    entry = store.get(name)
    if entry is None:
        raise HTTPException(status_code=404, detail="Handshake not found")
    path = safe_path_in(config.CAPTURES_DIR, name)
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Capture file missing")
    essid = await _extract_essid(path, entry.get("bssid", ""))
    if not essid:
        raise HTTPException(status_code=404, detail="No SSID in this capture (no association frame was recorded)")
    store.update_essid(name, essid)
    return {"ok": True, "essid": essid}


@router.delete("/{cap}")
async def delete_handshake(cap: str) -> dict:
    """Forget a handshake and delete its capture file."""
    name = clean_filename(cap)
    store.remove(name)
    path = safe_path_in(config.CAPTURES_DIR, name)
    if path.is_file():
        path.unlink()
    return {"ok": True, "deleted": name}
