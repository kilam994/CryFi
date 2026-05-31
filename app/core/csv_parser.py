"""Parser for ``airodump-ng`` CSV output.

airodump-ng's ``-w <prefix> --output-format csv`` writes ``<prefix>-NN.csv``
files containing two sections separated by a blank line:

    1. Access points  (header begins with "BSSID")
    2. Stations/clients (header begins with "Station MAC")

We tolerate partial/just-flushed files and return structured dicts.
"""
from __future__ import annotations

import csv
import io
from pathlib import Path


def _to_int(value: str, default: int = 0) -> int:
    try:
        return int(value.strip())
    except (ValueError, AttributeError):
        return default


def parse_csv_text(text: str) -> dict:
    """Parse airodump CSV text into ``{"aps": [...], "clients": [...]}``."""
    # Split into the two logical sections on the blank-line boundary.
    lines = text.splitlines()
    ap_block: list[str] = []
    client_block: list[str] = []
    target = ap_block
    seen_station_header = False

    for line in lines:
        stripped = line.strip()
        if stripped.startswith("Station MAC"):
            seen_station_header = True
            target = client_block
            target.append(line)
            continue
        if not stripped and seen_station_header:
            continue
        target.append(line)

    aps = _parse_aps(ap_block)
    clients = _parse_clients(client_block)
    return {"aps": aps, "clients": clients}


def _parse_aps(block: list[str]) -> list[dict]:
    rows: list[dict] = []
    reader = csv.reader(io.StringIO("\n".join(block)))
    for fields in reader:
        if not fields or not fields[0].strip():
            continue
        if fields[0].strip() == "BSSID":  # header row
            continue
        if len(fields) < 14:
            continue
        bssid = fields[0].strip()
        # Basic shape guard: BSSID must look like a MAC.
        if bssid.count(":") != 5:
            continue
        # Hidden APs broadcast no name: the ESSID is blank or just null/control
        # bytes. Strip those so "hidden" is a clean, reliable flag.
        essid = "".join(c for c in fields[13] if c.isprintable()).strip()
        rows.append({
            "bssid": bssid,
            "first_seen": fields[1].strip(),
            "last_seen": fields[2].strip(),
            "channel": _to_int(fields[3]),
            "speed": _to_int(fields[4]),
            "privacy": fields[5].strip(),
            "cipher": fields[6].strip(),
            "auth": fields[7].strip(),
            "power": _to_int(fields[8], default=-100),
            "beacons": _to_int(fields[9]),
            "data": _to_int(fields[10]),
            "essid": essid,
            "hidden": essid == "",
        })
    return rows


def _parse_clients(block: list[str]) -> list[dict]:
    rows: list[dict] = []
    reader = csv.reader(io.StringIO("\n".join(block)))
    for fields in reader:
        if not fields or not fields[0].strip():
            continue
        if fields[0].strip() == "Station MAC":  # header row
            continue
        if len(fields) < 6:
            continue
        station = fields[0].strip()
        if station.count(":") != 5:
            continue
        rows.append({
            "station": station,
            "first_seen": fields[1].strip(),
            "last_seen": fields[2].strip(),
            "power": _to_int(fields[3], default=-100),
            "packets": _to_int(fields[4]),
            "bssid": fields[5].strip(),
        })
    return rows


def parse_latest_csv(prefix_dir: Path, prefix: str) -> dict:
    """Find the newest ``<prefix>-NN.csv`` under ``prefix_dir`` and parse it."""
    candidates = sorted(
        prefix_dir.glob(f"{prefix}-*.csv"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    if not candidates:
        return {"aps": [], "clients": []}
    try:
        text = candidates[0].read_text(errors="replace")
    except OSError:
        return {"aps": [], "clients": []}
    return parse_csv_text(text)
