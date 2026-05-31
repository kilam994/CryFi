"""Background wordlist-generation jobs.

Generation is a pure-Python stream, so it runs in a daemon thread (not the
subprocess ProcessManager). The job streams entries to one or more split files
with constant memory:

  * dedupe is OPTIONAL and memory-bounded — once the seen-set hits DEDUPE_CAP the
    job keeps going *without* dedupe, so output size is limited by disk, not RAM.
  * output can be split into N-line files (wordlist-00001.txt, -00002.txt, …).
  * a disk-space guard stops the job before the volume fills.
  * Stop is cooperative; whatever was written so far is preserved and usable.

This is what lets the generator scale from thousands to ~1e12 lines.
"""
from __future__ import annotations

import shutil
import threading
import time
import uuid
from typing import Callable, Iterator

from .. import config


def _split_name(base: str, idx: int) -> str:
    stem = base[:-4] if base.lower().endswith(".txt") else base
    return f"{stem}-{idx:05d}.txt"


def _single_name(base: str) -> str:
    return base if base.lower().endswith(".txt") else base + ".txt"


class GenJob:
    def __init__(self, job_id: str, base_name: str, build: Callable[[], Iterator[str]],
                 target_lines: int, lines_per_file: int, dedupe: bool) -> None:
        self.id = job_id
        self.base_name = base_name
        self.build = build
        self.target_lines = target_lines
        self.lines_per_file = lines_per_file
        self.dedupe = dedupe
        self.status = "queued"  # queued|running|done|stopped|disk_full|error
        self.lines = 0
        self.bytes = 0
        self.files: list[str] = []
        self.dedupe_capped = False
        self.error: str | None = None
        self.started = time.monotonic()
        self.stop_event = threading.Event()
        self.done = False


class GenManager:
    def __init__(self) -> None:
        self._jobs: dict[str, GenJob] = {}

    def start(self, *, base_name: str, build, target_lines: int,
              lines_per_file: int, dedupe: bool) -> GenJob:
        job = GenJob(uuid.uuid4().hex[:12], base_name, build, target_lines, lines_per_file, dedupe)
        self._jobs[job.id] = job
        threading.Thread(target=self._run, args=(job,), daemon=True).start()
        return job

    def get(self, job_id: str) -> GenJob | None:
        return self._jobs.get(job_id)

    def stop(self, job_id: str) -> bool:
        job = self._jobs.get(job_id)
        if not job or job.done:
            return False
        job.stop_event.set()
        return True

    def status(self, job: GenJob) -> dict:
        elapsed = max(time.monotonic() - job.started, 1e-6)
        return {
            "job_id": job.id, "status": job.status, "done": job.done,
            "lines": job.lines, "bytes": job.bytes, "files": job.files,
            "rate": int(job.lines / elapsed), "dedupe_capped": job.dedupe_capped,
            "target": job.target_lines, "error": job.error,
        }

    def reap(self, max_jobs: int = 50) -> None:
        finished = [j for j in self._jobs.values() if j.done]
        if len(finished) > max_jobs:
            finished.sort(key=lambda j: j.started)
            for j in finished[: len(finished) - max_jobs]:
                self._jobs.pop(j.id, None)

    def _run(self, job: GenJob) -> None:
        job.status = "running"
        per = job.lines_per_file
        seen: set[str] | None = set() if job.dedupe else None
        wl_dir = config.WORDLISTS_DIR
        f = None
        cur = 0
        idx = 0

        def open_next():
            nonlocal f, idx, cur
            if f:
                f.close()
            idx += 1
            name = _split_name(job.base_name, idx) if per else _single_name(job.base_name)
            job.files.append(name)
            f = open(wl_dir / name, "w", encoding="utf-8", errors="ignore", newline="\n")
            cur = 0

        check = 0
        try:
            open_next()
            for w in job.build():
                if job.stop_event.is_set():
                    job.status = "stopped"
                    break
                if not w:
                    continue
                if seen is not None:
                    if len(seen) >= config.WORDGEN_DEDUPE_CAP:
                        seen = None            # drop dedupe, keep streaming
                        job.dedupe_capped = True
                    elif w in seen:
                        continue
                    else:
                        seen.add(w)
                if per and cur >= per:
                    open_next()
                f.write(w)
                f.write("\n")
                job.bytes += len(w) + 1
                job.lines += 1
                cur += 1
                if job.lines >= job.target_lines:
                    job.status = "done"
                    break
                check += 1
                if check >= 200_000:
                    check = 0
                    if shutil.disk_usage(wl_dir).free < config.WORDGEN_MIN_FREE_BYTES:
                        job.status = "disk_full"
                        break
            if job.status == "running":
                job.status = "done"
        except Exception as e:  # noqa: BLE001 — surface any generation error to the UI
            job.status = "error"
            job.error = str(e)
        finally:
            if f:
                f.close()
            job.done = True
            self.reap()


manager = GenManager()
