"""Captured-handshake listing + cleanup.

Backs the Handshakes tab: each entry points at a ``.cap`` known to contain a
WPA handshake, ready to feed into the dictionary-attack form.
"""
from __future__ import annotations

from fastapi import APIRouter

from .. import config
from ..core.handshakes import store
from ..core.security import clean_filename, safe_path_in

router = APIRouter(prefix="/api/handshakes", tags=["handshakes"])


@router.get("")
async def list_handshakes() -> dict:
    return {"handshakes": store.list()}


@router.delete("/{cap}")
async def delete_handshake(cap: str) -> dict:
    """Forget a handshake and delete its capture file."""
    name = clean_filename(cap)
    store.remove(name)
    path = safe_path_in(config.CAPTURES_DIR, name)
    if path.is_file():
        path.unlink()
    return {"ok": True, "deleted": name}
