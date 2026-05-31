"""Scanning module: background airodump-ng + live CSV parsing.

A single discovery scan runs at a time. airodump writes CSV to a known prefix
under the captures dir; the frontend polls ``/results`` which re-parses the
newest CSV on each call.
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from .. import config
from ..core import csv_parser
from ..core.process_manager import manager
from ..core.security import clean_band, clean_channel, clean_iface

router = APIRouter(prefix="/api/scan", tags=["scan"])

# Fixed prefix for the rolling discovery scan (overwritten each run).
SCAN_PREFIX = "cryfi-scan"


class ScanStart(BaseModel):
    iface: str
    channel: str | int | None = None
    band: str | None = None  # combination of a/b/g/n, e.g. "bg" or "abg"


def _current_scan():
    jobs = manager.jobs_by_kind("scan", running_only=True)
    return jobs[0] if jobs else None


@router.post("/start")
async def start_scan(body: ScanStart) -> dict:
    """Launch a discovery `airodump-ng` writing CSV to the captures dir."""
    if _current_scan():
        raise HTTPException(status_code=409, detail="A scan is already running")

    iface = clean_iface(body.iface)
    channel = clean_channel(body.channel)
    band = clean_band(body.band)

    # Clear stale CSV/cap from a previous discovery run so the table is fresh.
    for old in config.CAPTURES_DIR.glob(f"{SCAN_PREFIX}-*"):
        try:
            old.unlink()
        except OSError:
            pass

    prefix_path = str(config.CAPTURES_DIR / SCAN_PREFIX)
    cmd = [
        *config.sudo_prefix(),
        "airodump-ng",
        "--write", prefix_path,
        "--output-format", "csv",
        "--write-interval", "1",
    ]
    # A fixed channel and a band sweep are mutually exclusive in airodump-ng.
    # A specific channel wins; otherwise sweep the requested band(s). Default to
    # "abg" so 5 GHz networks show up (airodump only scans 2.4 GHz by default).
    if channel:
        cmd += ["--channel", channel]
    else:
        cmd += ["--band", band or "abg"]
    cmd.append(iface)

    job = await manager.start("scan", cmd, meta={"iface": iface, "prefix": SCAN_PREFIX})
    return {"job_id": job.job_id, "status": "running", "poll_ms": config.SCAN_POLL_INTERVAL_MS}


@router.get("/results")
async def scan_results() -> dict:
    """Return the latest parsed APs/clients from the live CSV."""
    job = _current_scan()
    data = csv_parser.parse_latest_csv(config.CAPTURES_DIR, SCAN_PREFIX)
    return {
        "running": job is not None,
        "job_id": job.job_id if job else None,
        "aps": data["aps"],
        "clients": data["clients"],
    }


@router.post("/stop")
async def stop_scan() -> dict:
    """Terminate the active discovery scan."""
    job = _current_scan()
    if not job:
        return {"ok": True, "status": "idle"}
    await manager.stop(job.job_id)
    return {"ok": True, "status": "stopped", "job_id": job.job_id}
