"""Cracking interface: run aircrack-ng and parse its live status.

aircrack-ng paints a curses-style screen with ANSI cursor codes. We strip those
and pull out the live numbers (speed, current passphrase, progress, found key)
into ``job.meta`` so the UI can show a clean dashboard instead of raw scrollback.
"""
from __future__ import annotations

import re

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from .. import config
from ..core.handshakes import store as handshake_store
from ..core.process_manager import manager
from ..core.security import clean_bssid, safe_path_in

router = APIRouter(prefix="/api/crack", tags=["crack"])

_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
# "[00:00:04] 5168/9822 keys tested (1268.45 k/s)"
_RE_PROG = re.compile(r"([\d,]+)\s*/\s*([\d,]+)\s+keys tested\s*\(([\d.]+)\s*k/s\)")
# "Current passphrase: secret123                 " (padded with spaces)
_RE_CUR = re.compile(r"Current passphrase:\s*(.+?)\s{2,}")
_RE_FOUND = re.compile(r"KEY FOUND!\s*\[\s*(.*?)\s*\]")
_RE_NOTFOUND = re.compile(r"KEY NOT FOUND")
# WPA3/SAE captures and empty captures fail in distinctive ways.
_RE_WPA3 = re.compile(r"May be WPA3|Unsupported key version", re.I)
_RE_NOHS = re.compile(r"no valid WPA handshakes|Got no data packets|No networks found", re.I)


class CrackStart(BaseModel):
    cap_file: str
    wordlist: str
    bssid: str | None = None  # optional: restrict to one BSSID in the cap


def _crack_hook(job, line: str) -> None:
    """Parse one output line into the job's live status meta."""
    s = _ANSI_RE.sub("", line)
    m = _RE_PROG.search(s)
    if m:
        job.meta["tested"] = int(m.group(1).replace(",", ""))
        job.meta["total"] = int(m.group(2).replace(",", ""))
        job.meta["speed"] = m.group(3)
        if job.meta["total"]:
            job.meta["percent"] = round(job.meta["tested"] * 100.0 / job.meta["total"], 2)
    m = _RE_CUR.search(s)
    if m and m.group(1).strip():
        job.meta["current"] = m.group(1).strip()
    m = _RE_FOUND.search(s)
    if m:
        job.meta["key"] = m.group(1)
        job.meta["status"] = "found"
        # Persist the cracked passphrase onto its handshake entry.
        cap = job.meta.get("cap")
        if cap:
            handshake_store.set_password(cap, m.group(1))
        return
    if _RE_WPA3.search(s):
        job.meta["status"] = "wpa3"
        return
    if _RE_NOHS.search(s):
        job.meta["status"] = "no_handshake"
        return
    if _RE_NOTFOUND.search(s):
        job.meta["status"] = "notfound"


@router.post("/start")
async def start_crack(body: CrackStart) -> dict:
    """Run `aircrack-ng -w <wordlist> <cap>` as a background job with live parsing."""
    cap_path = safe_path_in(config.CAPTURES_DIR, body.cap_file)
    if not cap_path.is_file():
        raise HTTPException(status_code=404, detail="Capture file not found")
    if cap_path.suffix.lower() not in {".cap", ".pcap"}:
        raise HTTPException(status_code=400, detail="Select a .cap/.pcap capture file")

    wl_path = safe_path_in(config.WORDLISTS_DIR, body.wordlist)
    if not wl_path.is_file():
        raise HTTPException(status_code=404, detail="Wordlist not found")

    cmd = [
        *config.sudo_prefix(),
        "aircrack-ng",
        "-w", str(wl_path),
    ]
    if body.bssid:
        cmd += ["-b", clean_bssid(body.bssid)]
    cmd.append(str(cap_path))

    job = await manager.start(
        "crack", cmd,
        meta={"cap": cap_path.name, "wordlist": wl_path.name, "status": "running",
              "current": "", "speed": None, "percent": None, "key": None},
    )
    job.line_hook = _crack_hook
    return {"job_id": job.job_id, "ws": f"/ws/terminal/{job.job_id}"}
