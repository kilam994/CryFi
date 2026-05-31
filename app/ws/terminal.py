"""WebSocket endpoint streaming a job's live stdout to the browser console.

On connect we replay the backlog, then forward each new line. When the client
disconnects, if no other subscriber remains for a *streaming* job (capture or
deauth), the underlying process is terminated — preventing zombie processes
left running after the user closes the terminal panel.
"""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from ..core import auth
from ..core.process_manager import manager

router = APIRouter()

# Job kinds that should be killed once nobody is watching their terminal.
# Crack is intentionally excluded: it's driven by the polled status dashboard,
# so closing the raw-output stream must not abort the crack.
_KILL_ON_LAST_DISCONNECT = {"capture", "deauth"}


@router.websocket("/ws/terminal/{job_id}")
async def terminal(ws: WebSocket, job_id: str) -> None:
    # Reject unauthenticated sockets (cookie sent automatically by the browser).
    if not auth.valid_session(ws.cookies.get(auth.COOKIE_NAME)):
        await ws.close(code=1008)  # policy violation
        return
    await ws.accept()
    job = manager.get(job_id)
    if job is None:
        await ws.send_text("[error] unknown job id")
        await ws.close()
        return

    queue: asyncio.Queue = asyncio.Queue(maxsize=1000)
    job.subscribers.add(queue)

    # Replay backlog so a late joiner sees prior output.
    for line in list(job.backlog):
        await ws.send_text(line)
    if job.done:
        await ws.send_text(f"[process already finished, code {job.returncode}]")

    async def forward() -> None:
        while True:
            line = await queue.get()
            if line is None:  # sentinel: process finished
                return
            await ws.send_text(line)

    async def watch_client() -> None:
        # Drain inbound frames; any disconnect raises and ends the task.
        while True:
            await ws.receive_text()

    forward_task = asyncio.create_task(forward())
    watch_task = asyncio.create_task(watch_client())
    try:
        done, pending = await asyncio.wait(
            {forward_task, watch_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        for t in pending:
            t.cancel()
    except WebSocketDisconnect:
        pass
    finally:
        job.subscribers.discard(queue)
        forward_task.cancel()
        watch_task.cancel()
        # If this was the last viewer of a streaming job, stop the process.
        if (
            job.kind in _KILL_ON_LAST_DISCONNECT
            and not job.subscribers
            and not job.done
        ):
            await manager.stop(job.job_id)
        manager.reap()
