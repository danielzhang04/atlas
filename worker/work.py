"""Background work lifecycle manager."""
from __future__ import annotations

import asyncio
from hashlib import sha256
from pathlib import Path
import threading
from typing import Callable
from uuid import uuid4

from .claude_launcher import ClaudeLauncher, parse_result, worker_prompt
from .jobstore import Job, JobState, JobStore


class WorkManager:
    def __init__(self, store: JobStore, launcher: ClaudeLauncher, workspace_root: Path,
                 *, poll_s: float = 2.0) -> None:
        self.store, self.launcher = store, launcher
        self.workspace_root, self.poll_s = Path(workspace_root), float(poll_s)
        self._seen: dict[str, int] = {}
        self._callbacks: list[Callable[[Job], None]] = []
        self._notified: set[str] = set()

    @staticmethod
    def _nonce(job_id: str) -> str:
        return sha256(f"atlas-result:{job_id}".encode()).hexdigest()[:32]

    def launch(self, title: str, brief: str) -> Job:
        job = self.store.create(title, brief)
        threading.Thread(target=self._launch, args=(job.job_id,), daemon=True,
                         name=f"atlas-work-{job.job_id[:8]}").start()
        return job

    def _launch(self, job_id: str) -> None:
        try:
            job_dir = self.workspace_root / job_id
            job_dir.mkdir(parents=True, exist_ok=True)
            self.store.transition(job_id, JobState.LAUNCHING)
            session_id = str(uuid4())
            prompt = worker_prompt(job_id, self._nonce(job_id), self.store.brief(job_id))
            launched = self.launcher.launch(session_id=session_id, name=f"atlas-{job_id[:8]}",
                                            prompt=prompt, cwd=job_dir)
            if self.store.get(job_id).state is JobState.CANCELLED:
                self.launcher.cancel(launched)
            else:
                self.store.transition(job_id, JobState.RUNNING, session_id=launched)
        except Exception:
            if self.store.get(job_id).state is not JobState.CANCELLED:
                self._terminal(self.store.transition(job_id, JobState.FAILED, error="launch_failed"))

    def active(self) -> list[Job]: return list(self.store.active())
    def recent(self, n: int) -> list[Job]: return list(self.store.recent(n))

    def cancel(self, job_id: str) -> Job:
        job = self.store.get(job_id)
        if job.session_id:
            try: self.launcher.cancel(job.session_id)
            except Exception: pass
        terminal = self.store.transition(job_id, JobState.CANCELLED)
        self._terminal(terminal)
        return terminal

    def on_terminal(self, fn: Callable[[Job], None]) -> None:
        if not callable(fn): raise TypeError("callback must be callable")
        self._callbacks.append(fn)

    def _terminal(self, job: Job) -> None:
        if job.job_id in self._notified: return
        self._notified.add(job.job_id)
        for callback in tuple(self._callbacks): callback(job)

    def _poll(self, job: Job) -> None:
        if not job.session_id:
            self._terminal(self.store.transition(job.job_id, JobState.FAILED, error="orphaned"))
            return
        lines = self.launcher.logs(job.session_id)
        start = self._seen.get(job.job_id, 0)
        for line in lines[start:]: self.store.append_output(job.job_id, line)
        self._seen[job.job_id] = len(lines)
        status = self.launcher.status(job.session_id)
        if status == "running": return
        if status == "needs_input":
            self._terminal(self.store.transition(job.job_id, JobState.FAILED, error="needs_input"))
        elif status == "failed":
            self._terminal(self.store.transition(job.job_id, JobState.FAILED, error="session_failed"))
        elif status == "done":
            summary = parse_result(lines, nonce=self._nonce(job.job_id), job_id=job.job_id)
            if summary is None:
                self._terminal(self.store.transition(job.job_id, JobState.FAILED, error="result_missing"))
            else:
                self.store.set_result(job.job_id, summary)
                self._terminal(self.store.transition(job.job_id, JobState.SUCCEEDED, summary=summary))

    async def run(self, stop: asyncio.Event) -> None:
        for job in self.store.active():
            if job.state in {JobState.LAUNCHING, JobState.RUNNING} and not job.session_id:
                self._terminal(self.store.transition(job.job_id, JobState.FAILED, error="orphaned"))
        while not stop.is_set():
            for job in self.store.active():
                if job.state is JobState.RUNNING:
                    try: self._poll(job)
                    except Exception: pass
            try: await asyncio.wait_for(stop.wait(), timeout=self.poll_s)
            except TimeoutError: pass


__all__ = ["WorkManager"]
