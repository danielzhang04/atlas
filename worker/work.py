"""Background work lifecycle manager."""
from __future__ import annotations

import asyncio
from hashlib import sha256
import logging
import math
from pathlib import Path
import threading
from typing import Callable, Mapping
from uuid import uuid4

from .claude_launcher import ClaudeLauncher, parse_result, worker_prompt
from .jobstore import Job, JobState, JobStore


logger = logging.getLogger("atlas.work")


class WorkManager:
    def __init__(
        self,
        store: JobStore,
        launcher: ClaudeLauncher,
        workspace_root: Path,
        *,
        poll_s: float = 2.0,
        folders: Mapping[str, Path] | None = None,
    ) -> None:
        self.store = store
        self.launcher = launcher
        self.workspace_root = Path(workspace_root)
        self.poll_s = float(poll_s)
        self.folders = {
            str(name): Path(path)
            for name, path in (folders or {}).items()
        }
        self._seen: dict[str, set[str]] = {}
        self._callbacks: list[Callable[[Job], None]] = []
        self._notified: set[str] = set()
        self._terminal_lock = threading.Lock()
        self._job_locks: dict[str, threading.Lock] = {}
        self._job_locks_lock = threading.Lock()

    def _job_lock(self, job_id: str) -> threading.Lock:
        with self._job_locks_lock:
            lock = self._job_locks.get(job_id)
            if lock is None:
                lock = threading.Lock()
                self._job_locks[job_id] = lock
            return lock

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
            requested_session_id = str(uuid4())
            with self._job_lock(job_id):
                job = self.store.get(job_id)
                if job.state is not JobState.QUEUED:
                    return
                self.store.transition(
                    job_id,
                    JobState.LAUNCHING,
                    session_id=requested_session_id,
                )
            prompt = worker_prompt(
                job_id,
                self._nonce(job_id),
                self.store.brief(job_id),
                folders=self.folders,
            )
            launched_session_id = self.launcher.launch(
                session_id=requested_session_id,
                name=f"atlas-{job_id[:8]}",
                prompt=prompt,
                cwd=job_dir,
            )
            with self._job_lock(job_id):
                job = self.store.get(job_id)
                if job.state is not JobState.LAUNCHING:
                    self.launcher.cancel(launched_session_id, cwd=job_dir)
                    return
                self.store.transition(
                    job_id,
                    JobState.RUNNING,
                    session_id=launched_session_id,
                )
        except Exception:
            with self._job_lock(job_id):
                job = self.store.get(job_id)
                if job.state is JobState.CANCELLED:
                    return
                if job.state not in {JobState.QUEUED, JobState.LAUNCHING}:
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
        with self._job_lock(job_id):
            job = self.store.get(job_id)
            if job.state in {
                JobState.SUCCEEDED,
                JobState.FAILED,
                JobState.CANCELLED,
            }:
                return job
            if job.state is JobState.RUNNING and job.session_id:
                try:
                    self.launcher.cancel(
                        job.session_id,
                        cwd=self.workspace_root / job.job_id,
                    )
                except Exception:
                    self.store.append_output(
                        job_id,
                        "cancel failed; still running",
                    )
                    raise RuntimeError("cancel failed") from None
            terminal = self.store.transition(job_id, JobState.CANCELLED)
        self._terminal(terminal)
        return terminal

    async def cancel_active(self, timeout_s: float = 15.0) -> bool:
        """Cancel every active job and wait within a fixed shutdown budget."""
        if (
            isinstance(timeout_s, bool)
            or not isinstance(timeout_s, (int, float))
            or not math.isfinite(float(timeout_s))
            or timeout_s < 0
        ):
            raise ValueError("invalid shutdown timeout")
        loop = asyncio.get_running_loop()
        deadline = loop.time() + float(timeout_s)
        tasks = {
            asyncio.create_task(asyncio.to_thread(self.cancel, job.job_id)): job.job_id
            for job in self.active()
        }
        if tasks:
            remaining = max(0.0, deadline - loop.time())
            done, pending = await asyncio.wait(tasks, timeout=remaining)
            for task in done:
                try:
                    task.result()
                except Exception as exc:
                    logger.warning(
                        "shutdown cancellation failed for job %s: %s",
                        tasks[task],
                        type(exc).__name__,
                    )
            for task in pending:
                task.cancel()
        while self.active():
            remaining = deadline - loop.time()
            if remaining <= 0:
                return False
            await asyncio.sleep(min(0.05, remaining))
        return True

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
        seen = self._seen.get(job_id)
        if seen is None:
            seen = {
                event.text
                for event in self.store.events(job_id)
                if event.kind == "output"
            }
            self._seen[job_id] = seen
        for line in lines:
            if line in seen:
                continue
            if not line.strip():
                continue
            if not any(character.isalnum() for character in line):
                continue
            self.store.append_output(job_id, line)
            seen.add(line)

    def _poll(self, job: Job) -> None:
        with self._job_lock(job.job_id):
            job = self.store.get(job.job_id)
            if job.state is not JobState.RUNNING:
                return
        if not job.session_id:
            terminal = self._finish_running(job.job_id, JobState.FAILED, error="orphaned")
            if terminal is not None:
                self._terminal(terminal)
            return

        job_dir = self.workspace_root / job.job_id
        lines = self.launcher.logs(job.session_id, cwd=job_dir)
        self._append_new_lines(job.job_id, lines)
        session_status = self.launcher.status(job.session_id, cwd=job_dir)
        if session_status in {"running", "unknown"}:
            return
        if session_status == "needs_input":
            terminal = self._finish_running(
                job.job_id,
                JobState.FAILED,
                error="needs_input",
            )
            if terminal is not None:
                self._terminal(terminal)
            return
        if session_status == "failed":
            terminal = self._finish_running(
                job.job_id,
                JobState.FAILED,
                error="session_failed",
            )
            if terminal is not None:
                self._terminal(terminal)
            return

        result = parse_result(
            lines,
            nonce=self._nonce(job.job_id),
            job_id=job.job_id,
        )
        if result is None:
            terminal = self._finish_running(
                job.job_id,
                JobState.FAILED,
                error="result_missing",
            )
            if terminal is not None:
                self._terminal(terminal)
            return

        result_status, summary = result
        if result_status == "succeeded":
            terminal = self._finish_running(
                job.job_id,
                JobState.SUCCEEDED,
                summary=summary,
                result=summary,
            )
        elif result_status == "failed":
            terminal = self._finish_running(
                job.job_id,
                JobState.FAILED,
                summary=summary,
                error="task_failed",
            )
        else:
            terminal = self._finish_running(
                job.job_id,
                JobState.CANCELLED,
                summary=summary,
            )
        if terminal is not None:
            self._terminal(terminal)

    def _finish_running(
        self,
        job_id: str,
        state: JobState,
        *,
        summary: str | None = None,
        error: str | None = None,
        result: str | None = None,
    ) -> Job | None:
        with self._job_lock(job_id):
            job = self.store.get(job_id)
            if job.state is not JobState.RUNNING:
                return None
            if result is not None:
                self.store.set_result(job_id, result)
            return self.store.transition(
                job_id,
                state,
                summary=summary,
                error=error,
            )

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
                except Exception as exc:
                    logger.warning(
                        "poll failed for job %s: %s",
                        job.job_id,
                        type(exc).__name__,
                    )
                    continue
            try:
                await asyncio.wait_for(stop.wait(), timeout=self.poll_s)
            except TimeoutError:
                continue


__all__ = ["WorkManager"]
