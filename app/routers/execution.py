"""Targeted capture and aireplay-ng execution (streamed via WebSocket).

These endpoints spawn background jobs whose stdout is consumed over the
``/ws/terminal/{job_id}`` WebSocket. Each returns a ``job_id`` the frontend
connects to immediately.

Intended strictly for authorized auditing of networks you own or have written
permission to test.
"""
from __future__ import annotations

import asyncio
import contextlib
import re
import time

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from .. import config
from ..core import csv_parser
from ..core.handshakes import store as handshake_store
from ..core.process_manager import manager
from ..core.security import clean_bssid, clean_channel, clean_count, clean_iface

router = APIRouter(prefix="/api", tags=["execution"])

# airodump-ng prints e.g. "WPA handshake: AA:BB:CC:DD:EE:FF" on success, but
# embedded in ANSI cursor codes — strip those before matching.
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
_HANDSHAKE_RE = re.compile(r"WPA handshake:\s*([0-9A-Fa-f:]{17})")


class CaptureStart(BaseModel):
    iface: str
    bssid: str
    channel: str | int
    essid: str | None = None  # display label, recorded with the handshake
    name: str | None = None  # optional capture file prefix


class DeauthStart(BaseModel):
    iface: str
    bssid: str
    channel: str | int | None = None
    client: str | None = None  # optional specific client to deauth
    count: int = 5  # number of deauth bursts; 0 = continuous


def _safe_prefix(raw: str | None, fallback: str) -> str:
    """Build a filesystem-safe capture prefix from an optional user label."""
    import re
    if not raw:
        return fallback
    cleaned = re.sub(r"[^A-Za-z0-9._-]", "_", raw)[:48]
    return cleaned or fallback


@router.post("/capture/start")
async def start_capture(body: CaptureStart) -> dict:
    """Targeted handshake capture on one BSSID/channel."""
    iface = clean_iface(body.iface)
    bssid = clean_bssid(body.bssid)
    channel = clean_channel(body.channel)

    # A single Wi-Fi adapter can't host two airodump-ng instances. Stop any
    # running discovery scan so the capture can take over the interface.
    for sj in manager.jobs_by_kind("scan", running_only=True):
        await manager.stop(sj.job_id)

    prefix = _safe_prefix(body.name, f"capture-{bssid.replace(':', '')}")
    prefix_path = str(config.CAPTURES_DIR / prefix)

    cmd = [
        *config.sudo_prefix(),
        "airodump-ng",
        "--background", "1",          # suppress the TUI redraw flood (no TTY)
        "--bssid", bssid,
        "--write", prefix_path,
        "--output-format", "pcap,csv",
    ]
    if channel:
        cmd += ["--channel", channel]
    cmd.append(iface)

    essid = (body.essid or "").strip()[:64]
    job = await manager.start(
        "capture", cmd,
        meta={
            "iface": iface, "bssid": bssid, "channel": channel,
            "prefix": prefix, "essid": essid, "handshake_captured": False,
        },
    )

    def _watch_for_handshake(j, line: str) -> None:
        """Detect the handshake notice, record it, then auto-stop the capture."""
        if j.meta.get("handshake_captured"):
            return
        m = _HANDSHAKE_RE.search(_ANSI_RE.sub("", line))
        if not m:
            return
        # Confirm it's our target (case-insensitive); ignore stray matches.
        if m.group(1).upper() != bssid.upper():
            return
        j.meta["handshake_captured"] = True
        # Newest .cap written under this prefix holds the handshake.
        caps = sorted(
            config.CAPTURES_DIR.glob(f"{prefix}-*.cap"),
            key=lambda p: p.stat().st_mtime, reverse=True,
        )
        cap_name = caps[0].name if caps else None
        # If the SSID was hidden, the live CSV may now hold the revealed name.
        recorded_essid = essid
        live = csv_parser.parse_latest_csv(config.CAPTURES_DIR, prefix)
        ap = next((a for a in live["aps"] if a["bssid"].upper() == bssid.upper()), None)
        if ap and ap.get("essid"):
            recorded_essid = ap["essid"]
            j.meta["essid"] = recorded_essid
        if cap_name:
            handshake_store.add(bssid=bssid, essid=recorded_essid, channel=channel, cap=cap_name)
            j.meta["cap"] = cap_name
        j.publish("\n[✓ WPA handshake captured — stopping capture]")
        # Stop in a separate task so the pump keeps draining stdout (no deadlock).
        asyncio.create_task(manager.stop(j.job_id))

    def _cleanup_sidecars(j) -> None:
        """On finish, drop the airodump CSV/netxml sidecars — keep only the .cap."""
        pfx = j.meta.get("prefix")
        if not pfx:
            return
        for p in config.CAPTURES_DIR.glob(f"{pfx}-*"):
            if p.suffix.lower() not in {".cap", ".pcap"}:
                with contextlib.suppress(OSError):
                    p.unlink()

    job.line_hook = _watch_for_handshake
    job.done_hook = _cleanup_sidecars
    return {"job_id": job.job_id, "ws": f"/ws/terminal/{job.job_id}"}


_HS_COUNT_RE = re.compile(r"\((\d+)\s+handshake", re.IGNORECASE)


async def _cap_has_handshake(cap_path) -> bool:
    """aircrack-ng confirms a usable WPA handshake in the cap (robust fallback)."""
    try:
        proc = await asyncio.create_subprocess_exec(
            *config.sudo_prefix(), "aircrack-ng", str(cap_path),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=15.0)
    except (asyncio.TimeoutError, FileNotFoundError, OSError):
        with contextlib.suppress(Exception):
            proc.kill()
        return False
    return any(int(n) > 0 for n in _HS_COUNT_RE.findall(out.decode(errors="replace")))


@router.get("/capture/{job_id}/status")
async def capture_status(job_id: str) -> dict:
    """Parsed capture progress: data-packet count + connected clients + handshake.

    Reads the live CSV airodump writes; additionally, if airodump's stdout
    handshake notice was missed, falls back to a throttled aircrack-ng check on
    the .cap so the handshake still surfaces live (no refresh/analyze needed).
    """
    job = manager.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Unknown capture job")
    bssid = (job.meta.get("bssid") or "").upper()
    prefix = job.meta.get("prefix")
    data = csv_parser.parse_latest_csv(config.CAPTURES_DIR, prefix) if prefix else {"aps": [], "clients": []}
    ap = next((a for a in data["aps"] if a["bssid"].upper() == bssid), None)
    clients = [
        {"station": c["station"], "power": c["power"], "packets": c["packets"]}
        for c in data["clients"] if (c.get("bssid") or "").upper() == bssid
    ]

    # Fallback handshake detection: once there's data traffic and not already
    # flagged, check the cap with aircrack-ng — throttled to ~4s between runs.
    if prefix and not job.meta.get("handshake_captured") and ap and ap.get("data", 0) > 0:
        now = time.monotonic()
        if now - job.meta.get("_last_hs_check", 0.0) > 4.0:
            job.meta["_last_hs_check"] = now
            caps = sorted(config.CAPTURES_DIR.glob(f"{prefix}-*.cap"),
                          key=lambda p: p.stat().st_mtime, reverse=True)
            if caps and await _cap_has_handshake(caps[0]):
                job.meta["handshake_captured"] = True
                rec_essid = (ap.get("essid") if ap else None) or job.meta.get("essid", "")
                handshake_store.add(bssid=job.meta.get("bssid"), essid=rec_essid,
                                    channel=job.meta.get("channel"), cap=caps[0].name)
                job.meta["cap"] = caps[0].name

    # When a client associates/probes, airodump fills in a previously hidden
    # ESSID — surface it so the UI can show the revealed name.
    revealed = ap["essid"] if (ap and ap.get("essid")) else None
    return {
        "job_id": job.job_id,
        "done": job.done,
        "handshake_captured": bool(job.meta.get("handshake_captured")),
        "essid": job.meta.get("essid"),
        "revealed_essid": revealed,
        "bssid": job.meta.get("bssid"),
        "channel": job.meta.get("channel"),
        "data": ap["data"] if ap else 0,
        "beacons": ap["beacons"] if ap else 0,
        "clients": clients,
    }


@router.post("/aireplay/deauth")
async def start_deauth(body: DeauthStart) -> dict:
    """Send deauthentication frames to a target BSSID (authorized use only)."""
    iface = clean_iface(body.iface)
    bssid = clean_bssid(body.bssid)
    count = clean_count(body.count, default=5, maximum=100000)
    client = clean_bssid(body.client) if body.client else None

    # Syntax: aireplay-ng --deauth <count> -a <ap_bssid> [-c <client>] <iface>
    #   --deauth N            send N deauth bursts (0 = continuous)
    #   -a                    target AP BSSID
    #   -c                    specific client (omitted => broadcast deauth)
    #   --ignore-negative-one tolerate drivers that report channel as -1, which
    #                         otherwise makes aireplay hang on "fixed channel".
    cmd = [
        *config.sudo_prefix(),
        "aireplay-ng",
        "--deauth", str(count),
        "-a", bssid,
        "--ignore-negative-one",
    ]
    if client:
        cmd += ["-c", client]
    cmd.append(iface)

    job = await manager.start(
        "deauth", cmd,
        meta={"iface": iface, "bssid": bssid, "client": client, "count": count},
    )
    return {"job_id": job.job_id, "ws": f"/ws/terminal/{job.job_id}"}


@router.get("/jobs")
async def list_jobs() -> dict:
    return {"jobs": manager.list_jobs()}


@router.get("/jobs/{job_id}")
async def get_job(job_id: str) -> dict:
    """Status of a single job — polled by the UI to detect capture completion."""
    job = manager.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Unknown job id")
    return {
        "job_id": job.job_id,
        "kind": job.kind,
        "done": job.done,
        "returncode": job.returncode,
        "meta": job.meta,
    }


@router.post("/jobs/{job_id}/stop")
async def stop_job(job_id: str) -> dict:
    stopped = await manager.stop(job_id)
    return {"ok": stopped, "job_id": job_id}
