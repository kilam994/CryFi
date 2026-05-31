"""Regulatory-domain awareness.

We determine per-channel transmit capability from the **full** regulatory rules
(`iw reg get`) — the authoritative source — combined with the monitor radio's
actual supported channels and operational state (`iw phy`).

`iw reg get` lists frequency ranges with flags that decide whether the radio may
*transmit* there:

  * ``NO-IR`` / ``PASSIVE-SCAN`` -> receive only (deauth won't go out)
  * ``DFS``                      -> radar channel, passive until clear
  * ``IR-CONCURRENT``           -> transmit only while associated (n/a in monitor)
  * ``NO-OUTDOOR``             -> informational

A channel is deauth-capable only when the device enables it AND its rule has
none of NO-IR / PASSIVE-SCAN.
"""
from __future__ import annotations

import re

from fastapi import APIRouter

from .interfaces import _parse_iw_dev, _run, _sysfs

router = APIRouter(prefix="/api/regulatory", tags=["regulatory"])

_COUNTRY_RE = re.compile(r"country\s+([A-Za-z0-9]{2})\s*:\s*(\S+)?")
# Rule line: "(5735 - 5755 @ 80), (6, 22), (N/A), AUTO-BW, NO-HT40MINUS, ..."
_RULE_RE = re.compile(r"\(\s*(\d+)\s*-\s*(\d+)\s*@")
# iw phy channel: "* 5745.0 MHz [149] (22.0 dBm)"  /  "* 5320 MHz [64] (disabled)"
_FREQ_RE = re.compile(r"\*\s*([\d.]+)\s*MHz\s*\[(\d+)\](.*)$")


_BLOCK_RE = re.compile(r"^\s*(global|phy#\d+)", re.IGNORECASE)


def _reg_blocks(reg_out: str) -> dict[str, str]:
    """Split `iw reg get` into its 'global' and per-'phy#N' rule blocks."""
    blocks: dict[str, str] = {}
    cur, lines = None, []
    for line in reg_out.splitlines():
        h = _BLOCK_RE.match(line)
        if h:
            if cur is not None:
                blocks[cur] = "\n".join(lines)
            cur, lines = h.group(1).lower(), [line]
        elif cur is not None:
            lines.append(line)
    if cur is not None:
        blocks[cur] = "\n".join(lines)
    return blocks


def _parse_reg_rules(block_text: str) -> list[dict]:
    """Parse frequency ranges + transmit-relevant flags from one reg block."""
    rules: list[dict] = []
    for line in block_text.splitlines():
        m = _RULE_RE.search(line)
        if not m:
            continue
        up = line.upper()
        rules.append({
            "lo": int(m.group(1)), "hi": int(m.group(2)),
            "no_ir": "NO-IR" in up,
            "passive": "PASSIVE-SCAN" in up,
            "dfs": "DFS" in up,
            "ir_concurrent": "IR-CONCURRENT" in up,
            "no_outdoor": "NO-OUTDOOR" in up,
        })
    return rules


@router.get("")
async def regulatory() -> dict:
    """Country + per-channel transmit-capability map, scoped to the monitor radio."""
    _, reg_out, _ = await _run(["iw", "reg", "get"])

    # Scope to the radio we scan/transmit with: the monitor interface's phy.
    _, dev_out, _ = await _run(["iw", "dev"])
    ifaces = _parse_iw_dev(dev_out)
    chosen = next((i for i in ifaces if i["monitor"]), ifaces[0] if ifaces else None)
    phy = _sysfs(chosen["name"], "phy80211/name") if chosen else ""  # e.g. "phy1"

    # Use this phy's own regulatory block when it self-manages (phy#N), else the
    # global block — so we read the rules the radio actually enforces.
    blocks = _reg_blocks(reg_out)
    phy_idx = phy[3:] if phy.startswith("phy") else ""
    block = blocks.get(f"phy#{phy_idx}") or blocks.get("global") or reg_out
    rules = _parse_reg_rules(block)

    cm = _COUNTRY_RE.findall(block) or _COUNTRY_RE.findall(reg_out)
    country = next((c[0].upper() for c in cm if c[0].upper() != "00"),
                   cm[0][0].upper() if cm else "00")
    dfs_region = next((c[1] for c in cm if c[1]), None)

    if phy:
        _, phy_out, _ = await _run(["iw", "phy", phy, "info"])
    else:
        _, phy_out, _ = await _run(["iw", "phy"])

    channels: dict[str, dict] = {}
    for line in phy_out.splitlines():
        m = _FREQ_RE.search(line)
        if not m:
            continue
        freq = float(m.group(1))
        # 6 GHz (Wi-Fi 6E) renumbers channels 1, 5, 9… colliding with 2.4 GHz;
        # scans only cover 2.4/5 GHz, so skip it.
        if freq >= 5900:
            continue
        ch = int(m.group(2))
        flags = m.group(3).lower()
        disabled = "disabled" in flags

        rule = next((r for r in rules if r["lo"] <= freq <= r["hi"]), None)
        # NO-IR comes from either the device's live flag or the regulatory rule.
        no_ir = ("no ir" in flags) or bool(rule and rule["no_ir"])
        passive = bool(rule and rule["passive"])
        dfs = ("radar" in flags) or bool(rule and rule["dfs"])
        ir_concurrent = bool(rule and rule["ir_concurrent"])
        tx_ok = not disabled and not (no_ir or passive)

        if disabled:
            reason = "disabled"
        elif tx_ok:
            reason = "transmit allowed"
        elif dfs:
            reason = "DFS / radar (passive)"
        elif passive:
            reason = "passive-scan only"
        elif ir_concurrent:
            reason = "IR-concurrent (transmit only while associated)"
        else:
            reason = "no-IR (no transmit)"

        channels[str(ch)] = {
            "freq": freq, "tx_ok": tx_ok, "reason": reason,
            "no_ir": no_ir, "passive": passive, "dfs": dfs,
            "ir_concurrent": ir_concurrent, "disabled": disabled,
        }

    tx_blocked = sorted((int(c) for c, v in channels.items() if not v["tx_ok"]))
    return {
        "country": country,
        "dfs_region": dfs_region,
        "self_managed": "self-managed" in reg_out.lower(),
        "iface": chosen["name"] if chosen else None,
        "phy": phy or None,
        "channels": channels,
        "tx_blocked_channels": tx_blocked,
    }
