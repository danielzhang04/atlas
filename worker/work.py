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
    def __init__(
        self,
        store: JobStore,
        launcher: ClaudeLauncher,
        workspace_root: Path,
        *,
        poll_s: float = 2.0,
    ) -> None:
        self.store = store
        self.launcher = launcher
        self.workspace_root = Path(workspace_root)
        self.poll_s = float(poll_s)
        self._seen: dict[str, set[str]] = {}
        self._callbacks: list[Callable[[Job], None]] = []
        self._notified: set[str] = set()
        self._terminal_lock = threading.Lock()

    @staticmethod
    def _nonce(job_id: str) -> str:
        value = f"atlas-result:{job_id}".encode()
        return sha256(value).hexdigest()[:32]

    def launch(self, title: str, brief: str) -> Job:
        job = self.store.create(title, brief)
        thread = threading.Thread(
            target=self._launch,
            args=(job.job_id,),
            daemon=True,
            name=f"atlas-work-{job.job_id[:8]}",
        )
        thread.start()
        return job

    def _launch(self, job_id: str) -> None:
        try:
            job_dir = self.workspace_root / job_id
            job_dir.mkdir(parents=True, exist_ok=True)
            self.store.transition(job_id, JobState.LAUNCHING)
            requested_session_id = str(uuid4())
            prompt = worker_prompt(
                job_id,
                self._nonce(job_id),
                self.store.brief(job_id),
            )
            launched_session_id = self.launcher.launch(
                session_id=requested_session_id,
                name=f"atlas-{job_id[:8]}",
                prompt=prompt,
                cwd=job_dir,
            )
            if self.store.get(job_id).state is JobState.CANCELLED:
                self.launcher.cancel(launched_session_id)
                return
            self.store.transition(
                job_id,
                JobState.RUNNING,
                session_id=launched_session_id,
            )
        except Exception:
            if self.store.get(job_id).state is JobState.CANCELLED:
                return
            terminal = self.store.transition(
                job_id,
                JobState.FAILED,
                error="launch_failed",
            )
            self._terminal(terminal)

    def active(self) -> list[Job]:
        return list(self.store.active())

    def recent(self, n: int) -> list[Job]:
        return list(self.store.recent(n))

    def cancel(self, job_id: str) -> Job:
        job = self.store.get(job_id)
        if job.session_id:
            try:
                self.launcher.cancel(job.session_id)
            except Exception:
                pass
        terminal = self.store.transition(job_id, JobState.CANCELLED)
        self._terminal(terminal)
        return terminal

    def on_terminal(self, fn: Callable[[Job], None]) -> None:
        if not callable(fn):
            raise TypeError("callback must be callable")
        self._callbacks.append(fn)

    def _terminal(self, job: Job) -> None:
        with self._terminal_lock:
            if job.job_id in self._notified:
                return
            self._notified.add(job.job_id)
        for callback in tuple(self._callbacks):
            callback(job)

    def _append_new_lines(self, job_id: str, lines: list[str]) -> None:
        seen = self._seen.setdefault(job_id, set())
        for line in lines:
            if line in seen:
                continue
            self.store.append_output(job_id, line)
            seen.add(line)

    def _poll(self, job: Job) -> None:
        if not job.session_id:
            terminal = self.store.transition(
                job.job_id,
                JobState.FAILED,
                error="orphaned",
            )
            self._terminal(terminal)
            return

        lines = self.launcher.logs(job.session_id)
        self._append_new_lines(job.job_id, lines)
        session_status = self.launcher.status(job.session_id)
        if session_status in {"running", "unknown"}:
            return
        if session_status == "needs_input":
            terminal = self.store.transition(
                job.job_id,
                JobState.FAILED,
                error="needs_input",
            )
            self._terminal(terminal)
            return
        if session_status == "failed":
            terminal = self.store.transition(
                job.job_id,
                JobState.FAILED,
                error="session_failed",
            )
            self._terminal(terminal)
            return

        result = parse_result(
            lines,
            nonce=self._nonce(job.job_id),
            job_id=job.job_id,
        )
        if result is None:
            terminal = self.store.transition(
                job.job_id,
                JobState.FAILED,
                error="result_missing",
            )
            self._terminal(terminal)
            return

        result_status, summary = result
        if result_status == "succeeded":
            self.store.set_result(job.job_id, summary)
            terminal = self.store.transition(
                job.job_id,
                JobState.SUCCEEDED,
                summary=summary,
            )
        elif result_status == "failed":
            terminal = self.store.transition(
                job.job_id,
                JobState.FAILED,
                summary=summary,
                error="task_failed",
            )
        else:
            terminal = self.store.transition(
                job.job_id,
                JobState.CANCELLED,
                summary=summary,
            )
        self._terminal(terminal)

    def _reattach(self) -> None:
        for job in self.store.active():
            if job.state not in {JobState.LAUNCHING, JobState.RUNNING}:
                continue
            if not job.session_id:
                terminal = self.store.transition(
                    job.job_id,
                    JobState.FAILED,
                    error="orphaned",
                )
                self._terminal(terminal)
                continue
            if job.state is JobState.LAUNCHING:
                self.store.transition(
                    job.job_id,
                    JobState.RUNNING,
                    session_id=job.session_id,
                )

    async def run(self, stop: asyncio.Event) -> None:
        self._reattach()
        while not stop.is_set():
            for job in self.store.active():
                if job.state is not JobState.RUNNING:
                    continue
                try:
                    self._poll(job)
                except Exception:
                    continue
            try:
                await asyncio.wait_for(stop.wait(), timeout=self.poll_s)
            except TimeoutError:
                continue


__all__ = ["WorkManager"]
