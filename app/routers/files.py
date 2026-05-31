"""File management: capture (.cap) listing/download/delete + wordlist uploads."""
from __future__ import annotations

import asyncio
import contextlib
import re
from pathlib import Path

from fastapi import APIRouter, HTTPException, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel

from .. import config
from ..core.handshakes import store as handshake_store
from ..core.security import clean_filename, safe_path_in

router = APIRouter(prefix="/api", tags=["files"])

# Capture artifacts we expose for download.
_CAPTURE_EXTS = {".cap", ".pcap", ".csv", ".netxml", ".kismet.csv"}
# The packet captures that can actually contain a handshake.
_PACKET_EXTS = {".cap", ".pcap"}
# aircrack-ng prints e.g. "WPA (1 handshake)" next to networks with a handshake.
_HS_COUNT_RE = re.compile(r"\((\d+)\s+handshake", re.IGNORECASE)


def _describe(path) -> dict:
    st = path.stat()
    return {"name": path.name, "size": st.st_size, "mtime": int(st.st_mtime)}


def _siblings(name: str) -> list[Path]:
    """All artifacts sharing a capture's stem (cap + csv + netxml + …)."""
    stem = Path(name).stem  # "capture-XX-01.cap" -> "capture-XX-01"
    out: list[Path] = []
    for p in config.CAPTURES_DIR.iterdir():
        if p.is_file() and (p.name == name or p.name.startswith(stem + ".")):
            out.append(p)
    return out


def _delete_capture_set(name: str) -> list[str]:
    """Delete a capture and its sibling artifacts; forget any handshake."""
    deleted: list[str] = []
    for p in _siblings(name):
        try:
            p.unlink()
            deleted.append(p.name)
        except OSError:
            pass
    handshake_store.remove(name)
    return deleted


async def _has_handshake(cap_path: Path) -> bool:
    """Ask aircrack-ng whether a capture holds a WPA handshake.

    Runs with stdin closed so the interactive network-picker prompt gets EOF
    and the process exits instead of hanging.
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            *config.sudo_prefix(), "aircrack-ng", str(cap_path),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=25.0)
    except (asyncio.TimeoutError, FileNotFoundError, OSError):
        with contextlib.suppress(Exception):
            proc.kill()
        return False
    text = out.decode(errors="replace")
    return any(int(n) > 0 for n in _HS_COUNT_RE.findall(text))


# --- Captures -------------------------------------------------------------

@router.get("/captures")
async def list_captures() -> dict:
    # Only show packet captures (.cap/.pcap). The airodump CSV/netxml sidecars
    # are noise and are auto-removed when a capture finishes.
    files = [
        _describe(p)
        for p in config.CAPTURES_DIR.iterdir()
        if p.is_file() and p.suffix.lower() in _PACKET_EXTS
    ]
    files.sort(key=lambda f: f["mtime"], reverse=True)
    return {"captures": files}


@router.post("/captures/analyze")
async def analyze_captures() -> dict:
    """Check every .cap/.pcap for a WPA handshake (bounded concurrency)."""
    caps = [
        p for p in config.CAPTURES_DIR.iterdir()
        if p.is_file() and p.suffix.lower() in _PACKET_EXTS
    ]
    sem = asyncio.Semaphore(4)

    async def check(p: Path) -> dict:
        async with sem:
            return {"name": p.name, "has_handshake": await _has_handshake(p)}

    results = await asyncio.gather(*(check(p) for p in caps))
    return {"results": list(results)}


@router.get("/captures/{name}/download")
async def download_capture(name: str):
    path = safe_path_in(config.CAPTURES_DIR, name)
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Capture not found")
    return FileResponse(path, filename=path.name, media_type="application/octet-stream")


@router.delete("/captures/{name}")
async def delete_capture(name: str) -> dict:
    name = clean_filename(name)
    if not _siblings(name):
        raise HTTPException(status_code=404, detail="Capture not found")
    return {"ok": True, "deleted": _delete_capture_set(name)}


class NameList(BaseModel):
    names: list[str]


@router.post("/captures/delete")
async def delete_captures(body: NameList) -> dict:
    """Delete a batch of captures (each with its sibling artifacts)."""
    deleted: list[str] = []
    for raw in body.names:
        deleted += _delete_capture_set(clean_filename(raw))
    return {"ok": True, "deleted": deleted, "count": len(deleted)}


@router.post("/captures/clean")
async def clean_captures() -> dict:
    """Delete every capture (and siblings) that has no WPA handshake."""
    caps = [
        p for p in config.CAPTURES_DIR.iterdir()
        if p.is_file() and p.suffix.lower() in _PACKET_EXTS
    ]
    sem = asyncio.Semaphore(4)

    async def check(p: Path) -> tuple[str, bool]:
        async with sem:
            return p.name, await _has_handshake(p)

    statuses = await asyncio.gather(*(check(p) for p in caps))
    removed: list[str] = []
    kept: list[str] = []
    for name, has in statuses:
        if has:
            kept.append(name)
        else:
            removed += _delete_capture_set(name)

    # Sweep orphan artifacts (csv/netxml/scan dumps) with no surviving capture.
    surviving = [p for p in config.CAPTURES_DIR.iterdir() if p.is_file()]
    packet_bases = {p.name.split(".")[0] for p in surviving if p.suffix.lower() in _PACKET_EXTS}
    for p in surviving:
        suf = p.suffix.lower()
        if suf in _PACKET_EXTS or suf not in _CAPTURE_EXTS:
            continue  # keep packets and non-artifacts (e.g. handshakes.json)
        if p.name.split(".")[0] not in packet_bases:
            with contextlib.suppress(OSError):
                p.unlink()
                removed.append(p.name)

    return {"ok": True, "removed": removed, "removed_count": len(removed), "kept_with_handshake": kept}


# --- Wordlists ------------------------------------------------------------

# Cache line counts so big wordlists aren't re-scanned on every list load.
_LINE_CACHE: dict[str, tuple[int, int, int]] = {}  # name -> (size, mtime, lines)


def _count_lines(path: Path) -> int:
    n = 0
    with path.open("rb") as f:
        while chunk := f.read(1024 * 1024):
            n += chunk.count(b"\n")
    return n


def _wordlist_lines(path: Path, st) -> int:
    key = path.name
    cached = _LINE_CACHE.get(key)
    if cached and cached[0] == st.st_size and cached[1] == int(st.st_mtime):
        return cached[2]
    lines = _count_lines(path)
    _LINE_CACHE[key] = (st.st_size, int(st.st_mtime), lines)
    return lines


# Sync def => runs in a threadpool, so counting a large file won't block the loop.
@router.get("/wordlists")
def list_wordlists() -> dict:
    files = []
    for p in config.WORDLISTS_DIR.iterdir():
        if p.is_file() and p.suffix.lower() == ".txt":
            d = _describe(p)
            d["lines"] = _wordlist_lines(p, p.stat())
            files.append(d)
    files.sort(key=lambda f: f["name"])
    return {"wordlists": files}


@router.get("/wordlists/{name}/preview")
def preview_wordlist(name: str, lines: int = 10) -> dict:
    """Return the first N lines of a wordlist (default 10)."""
    path = safe_path_in(config.WORDLISTS_DIR, name)
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Wordlist not found")
    n = max(1, min(int(lines), 100))
    sample: list[str] = []
    with path.open("r", encoding="utf-8", errors="replace") as f:
        for i, line in enumerate(f):
            if i >= n:
                break
            sample.append(line.rstrip("\n"))
    return {"name": name, "sample": sample}


@router.post("/wordlists/upload")
async def upload_wordlist(file: UploadFile) -> dict:
    if not file.filename or not file.filename.lower().endswith(".txt"):
        raise HTTPException(status_code=400, detail="Only .txt wordlists are allowed")
    name = clean_filename(file.filename)
    dest = safe_path_in(config.WORDLISTS_DIR, name)

    written = 0
    with dest.open("wb") as out:
        while chunk := await file.read(1024 * 1024):
            written += len(chunk)
            if written > config.MAX_WORDLIST_BYTES:
                out.close()
                dest.unlink(missing_ok=True)
                raise HTTPException(status_code=413, detail="Wordlist exceeds size limit")
            out.write(chunk)
    return {"ok": True, "name": name, "size": written}


@router.delete("/wordlists/{name}")
async def delete_wordlist(name: str) -> dict:
    path = safe_path_in(config.WORDLISTS_DIR, name)
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Wordlist not found")
    path.unlink()
    return {"ok": True, "deleted": name}
