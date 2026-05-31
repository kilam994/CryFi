"""Background process registry.

Every long-running subprocess (scan, capture, deauth, crack) is launched here
and tracked by a generated ``job_id``. The manager owns clean teardown:
SIGTERM the whole process group, then SIGKILL after a grace period. This is the
single chokepoint that prevents orphaned/zombie aircrack processes when the UI
stops a job or a WebSocket disconnects.
"""
from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import time
import uuid
from dataclasses import dataclass, field
from typing import Callable, Optional

from .. import config


@dataclass
class Job:
    job_id: str
    kind: str  # "scan" | "capture" | "deauth" | "crack"
    cmd: list[str]
    proc: asyncio.subprocess.Process
    # Bounded ring buffer of recent output lines (for late WebSocket joiners).
    backlog: list[str] = field(default_factory=list)
    # Subscribers receive each new output line. asyncio.Queue per WS client.
    subscribers: set[asyncio.Queue] = field(default_factory=set)
    done: bool = False
    returncode: Optional[int] = None
    finished_at: Optional[float] = None  # time.monotonic() when the process ended
    meta: dict = field(default_factory=dict)
    # Optional per-line inspector, e.g. to detect a captured WPA handshake.
    # Called synchronously for every output line: line_hook(job, line).
    line_hook: Optional[Callable] = None
    # Optional completion callback, e.g. to clean up sidecar files: done_hook(job).
    done_hook: Optional[Callable] = None

    BACKLOG_MAX = 500

    def publish(self, line: str) -> None:
        self.backlog.append(line)
        if len(self.backlog) > self.BACKLOG_MAX:
            del self.backlog[: len(self.backlog) - self.BACKLOG_MAX]
        for q in list(self.subscribers):
            with contextlib.suppress(asyncio.QueueFull):
                q.put_nowait(line)


class ProcessManager:
    def __init__(self) -> None:
        self._jobs: dict[str, Job] = {}
        self._lock = asyncio.Lock()

    # --- lifecycle --------------------------------------------------------

    async def start(self, kind: str, cmd: list[str], meta: dict | None = None) -> Job:
        """Spawn ``cmd`` in its own process group and begin pumping output."""
        self.reap()  # drop old finished jobs so the registry stays bounded
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            # New session => new process group, so we can signal children too.
            start_new_session=True,
        )
        job = Job(job_id=uuid.uuid4().hex[:12], kind=kind, cmd=cmd, proc=proc, meta=meta or {})
        async with self._lock:
            self._jobs[job.job_id] = job
        asyncio.create_task(self._pump(job))
        return job

    async def _pump(self, job: Job) -> None:
        """Read stdout line-by-line and fan out to subscribers."""
        assert job.proc.stdout is not None
        try:
            while True:
                raw = await job.proc.stdout.readline()
                if not raw:
                    break
                line = raw.decode(errors="replace").rstrip("\n")
                job.publish(line)
                if job.line_hook is not None:
                    with contextlib.suppress(Exception):
                        job.line_hook(job, line)
        finally:
            job.returncode = await job.proc.wait()
            job.done = True
            job.finished_at = time.monotonic()
            if job.done_hook is not None:
                with contextlib.suppress(Exception):
                    job.done_hook(job)
            job.publish(f"\n[process exited with code {job.returncode}]")
            # Wake any blocked subscribers so they observe completion.
            for q in list(job.subscribers):
                with contextlib.suppress(asyncio.QueueFull):
                    q.put_nowait(None)

    async def stop(self, job_id: str) -> bool:
        job = self._jobs.get(job_id)
        if not job or job.done:
            return False
        await self._terminate(job)
        return True

    async def _terminate(self, job: Job) -> None:
        """Escalate signals to the whole process group until the job exits.

        Order: SIGINT (airodump-ng/aireplay-ng treat this like Ctrl-C and do a
        clean radio teardown) -> SIGTERM -> SIGKILL. Each step waits a short
        slice of the grace budget. Signalling the *group* (negative pid via
        killpg) reaches airodump's forked child too; tini (PID 1) then reaps any
        orphan so nothing is left as <defunct>.
        """
        if job.proc.returncode is not None:
            return

        try:
            pgid = os.getpgid(job.proc.pid)
        except (ProcessLookupError, PermissionError):
            return

        def _signal(sig) -> None:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(pgid, sig)

        grace = config.PROC_TERM_GRACE
        # Two graceful attempts (SIGINT then SIGTERM), then a hard SIGKILL.
        for sig, slice_ in ((signal.SIGINT, grace * 0.5), (signal.SIGTERM, grace * 0.5)):
            _signal(sig)
            try:
                await asyncio.wait_for(asyncio.shield(job.proc.wait()), timeout=max(slice_, 0.5))
                return
            except asyncio.TimeoutError:
                continue

        _signal(signal.SIGKILL)
        with contextlib.suppress(Exception):
            await job.proc.wait()

    # --- queries ----------------------------------------------------------

    def get(self, job_id: str) -> Optional[Job]:
        return self._jobs.get(job_id)

    def list_jobs(self) -> list[dict]:
        return [
            {
                "job_id": j.job_id,
                "kind": j.kind,
                "done": j.done,
                "returncode": j.returncode,
                "meta": j.meta,
            }
            for j in self._jobs.values()
        ]

    def jobs_by_kind(self, kind: str, running_only: bool = True) -> list[Job]:
        return [
            j for j in self._jobs.values()
            if j.kind == kind and (not running_only or not j.done)
        ]

    async def stop_all(self) -> None:
        for job in list(self._jobs.values()):
            if not job.done:
                await self._terminate(job)

    def reap(self, max_age: float = 120.0) -> None:
        """Drop finished, unsubscribed jobs that ended over ``max_age`` ago.

        The age guard is important: the UI polls ``GET /api/jobs/{id}`` right
        after a capture finishes to read ``meta.handshake_captured``. Purging a
        just-finished job would make that poll 404 and the Stop button no-op.
        """
        now = time.monotonic()
        stale = [
            j.job_id for j in self._jobs.values()
            if j.done and not j.subscribers
            and (j.finished_at is None or (now - j.finished_at) > max_age)
        ]
        for jid in stale:
            self._jobs.pop(jid, None)


# Singleton used across routers and the WebSocket handler.
manager = ProcessManager()
