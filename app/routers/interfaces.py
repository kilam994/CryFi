"""Interface management: list adapters and toggle monitor mode."""
from __future__ import annotations

import asyncio
import os
import re
from pathlib import Path

from fastapi import APIRouter, HTTPException

from .. import config
from ..core.security import clean_iface

router = APIRouter(prefix="/api/interfaces", tags=["interfaces"])

# `iw dev` blocks: "\tInterface wlan0" ... "\t\ttype managed"
_IFACE_NAME_RE = re.compile(r"^\s*Interface\s+(\S+)", re.MULTILINE)
_TYPE_RE = re.compile(r"^\s*type\s+(\S+)", re.MULTILINE)
_IPV4_RE = re.compile(r"inet\s+(\d+\.\d+\.\d+\.\d+/\d+)")
_SSID_RE = re.compile(r"SSID:\s*(.+)")


def _sysfs(iface: str, rel: str) -> str:
    try:
        return (Path("/sys/class/net") / iface / rel).read_text().strip()
    except OSError:
        return ""


def _sys_link_base(iface: str, rel: str) -> str:
    """basename of a /sys symlink target (driver name, usb/pci subsystem)."""
    try:
        return os.path.basename(os.path.realpath(str(Path("/sys/class/net") / iface / rel)))
    except OSError:
        return ""


async def _run(cmd: list[str], timeout: float = 20.0) -> tuple[int, str, str]:
    """Run a command to completion, returning (rc, stdout, stderr)."""
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        raise HTTPException(status_code=504, detail=f"Command timed out: {' '.join(cmd)}")
    return proc.returncode or 0, out.decode(errors="replace"), err.decode(errors="replace")


def _parse_iw_dev(text: str) -> list[dict]:
    """Parse `iw dev` into a list of {name, type} per interface block."""
    interfaces: list[dict] = []
    # Each "Interface <name>" starts a block; capture the following `type`.
    blocks = re.split(r"(?=^\s*Interface\s+\S+)", text, flags=re.MULTILINE)
    for block in blocks:
        name_m = _IFACE_NAME_RE.search(block)
        if not name_m:
            continue
        type_m = _TYPE_RE.search(block)
        mode = type_m.group(1) if type_m else "unknown"
        interfaces.append({
            "name": name_m.group(1),
            "mode": mode,
            "monitor": mode == "monitor",
        })
    return interfaces


async def _enrich(iface: dict) -> None:
    """Add MAC, bus, driver, IPv4 and connected SSID to an interface dict."""
    name = iface["name"]
    iface["mac"] = _sysfs(name, "address")
    iface["bus"] = _sys_link_base(name, "device/subsystem")     # "usb" | "pci"
    iface["driver"] = _sys_link_base(name, "device/driver")     # e.g. rtl8xxxu
    # IPv4 (managed adapters that are up)
    _, ip_out, _ = await _run(["ip", "-4", "-o", "addr", "show", "dev", name])
    m = _IPV4_RE.search(ip_out)
    iface["ipv4"] = m.group(1) if m else None
    # Connected SSID (only meaningful for managed mode)
    iface["ssid"] = None
    if not iface["monitor"]:
        _, link_out, _ = await _run(["iw", "dev", name, "link"])
        sm = _SSID_RE.search(link_out)
        if sm:
            iface["ssid"] = sm.group(1).strip()


@router.get("")
async def list_interfaces() -> dict:
    """List wireless interfaces with mode, bus/driver, IPv4 and connected SSID."""
    rc, out, err = await _run(["iw", "dev"])
    if rc != 0:
        raise HTTPException(status_code=500, detail=err or "`iw dev` failed")
    interfaces = _parse_iw_dev(out)
    await asyncio.gather(*(_enrich(i) for i in interfaces))
    return {"interfaces": interfaces}


@router.post("/{iface}/monitor/start")
async def start_monitor(iface: str) -> dict:
    """Enable monitor mode: `airmon-ng start <iface>`."""
    iface = clean_iface(iface)
    cmd = [*config.sudo_prefix(), "airmon-ng", "start", iface]
    rc, out, err = await _run(cmd, timeout=30.0)
    if rc != 0:
        raise HTTPException(status_code=500, detail=(err or out) or "airmon-ng start failed")
    # Re-read state so the UI reflects the (possibly renamed) monitor iface.
    state = await list_interfaces()
    return {"ok": True, "output": out, "interfaces": state["interfaces"]}


@router.post("/{iface}/monitor/stop")
async def stop_monitor(iface: str) -> dict:
    """Disable monitor mode: `airmon-ng stop <iface>`."""
    iface = clean_iface(iface)
    cmd = [*config.sudo_prefix(), "airmon-ng", "stop", iface]
    rc, out, err = await _run(cmd, timeout=30.0)
    if rc != 0:
        raise HTTPException(status_code=500, detail=(err or out) or "airmon-ng stop failed")
    state = await list_interfaces()
    return {"ok": True, "output": out, "interfaces": state["interfaces"]}
