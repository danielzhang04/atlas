"""SQLite persistence for Atlas background work."""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from hashlib import sha256
from pathlib import Path
import sqlite3
import threading
import time
from typing import Callable, Literal
from uuid import uuid4

from .payload_codec import PayloadCodec, WindowsCurrentUserDPAPICodec


SCHEMA_VERSION = 1
_ACTIVE_STATES = (
    "queued",
    "launching",
    "running",
)
_TERMINAL_STATES = (
    "succeeded",
    "failed",
    "cancelled",
)


class JobState(str, Enum):
    QUEUED = "queued"
    LAUNCHING = "launching"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass(frozen=True, slots=True)
class Job:
    job_id: str
    title: str
    state: JobState
    session_id: str | None
    created_at: float
    updated_at: float
    summary: str | None
    error: str | None

    def to_public(self) -> dict:
        return {
            "id": self.job_id,
            "title": self.title,
            "status": self.state.value,
            "session_id": self.session_id,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "summary": self.summary,
            "error": self.error,
        }


@dataclass(frozen=True, slots=True)
class JobEvent:
    sequence: int
    timestamp: float
    kind: Literal["state", "output"]
    text: str


class JobStore:
    """Thread-safe store with protected briefs and results."""

    def __init__(
        self,
        path: str | Path = ":memory:",
        *,
        payload_codec: PayloadCodec | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.path = str(path)
        self._clock = clock
        self._codec = payload_codec or WindowsCurrentUserDPAPICodec()
        self._lock = threading.RLock()
        if self.path != ":memory:":
            target = Path(self.path)
            target.parent.mkdir(parents=True, exist_ok=True)
            self._replace_old_schema(target)
        self._db = sqlite3.connect(self.path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.executescript(
            """
            PRAGMA foreign_keys=ON;
            CREATE TABLE IF NOT EXISTS jobs(
                job_id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                state TEXT NOT NULL,
                session_id TEXT,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                summary TEXT,
                error TEXT,
                brief BLOB NOT NULL,
                result BLOB
            );
            CREATE TABLE IF NOT EXISTS events(
                job_id TEXT NOT NULL REFERENCES jobs(job_id) ON DELETE CASCADE,
                sequence INTEGER NOT NULL,
                timestamp REAL NOT NULL,
                kind TEXT NOT NULL,
                text TEXT NOT NULL,
                PRIMARY KEY(job_id, sequence)
            );
            CREATE INDEX IF NOT EXISTS jobs_state ON jobs(state);
            """
        )
        self._db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        self._db.commit()

    @staticmethod
    def _replace_old_schema(target: Path) -> None:
        if not target.exists():
            return
        try:
            probe = sqlite3.connect(target)
            try:
                row = probe.execute("PRAGMA user_version").fetchone()
                version = int(row[0])
            finally:
                probe.close()
        except (OSError, sqlite3.DatabaseError, TypeError, ValueError):
            version = -1
        if version == SCHEMA_VERSION:
            return
        backup = target.with_name(f"{target.name}.pre-revamp")
        if backup.exists():
            backup.unlink()
        target.replace(backup)

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def __enter__(self) -> "JobStore":
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    @staticmethod
    def _entropy(job_id: str, field: str) -> bytes:
        value = f"atlas-work-v1:{job_id}:{field}".encode()
        return sha256(value).digest()

    def create(self, title: str, brief: str) -> Job:
        if not isinstance(title, str) or not title.strip() or len(title) > 200:
            raise ValueError("invalid title")
        if not isinstance(brief, str) or not brief:
            raise ValueError("invalid brief")
        if len(brief.encode("utf-8")) > 65_536:
            raise ValueError("invalid brief")
        job_id = str(uuid4())
        now = float(self._clock())
        protected = self._codec.protect(
            brief.encode("utf-8"),
            entropy=self._entropy(job_id, "brief"),
        )
        values = (
            job_id,
            title.strip(),
            JobState.QUEUED.value,
            None,
            now,
            now,
            None,
            None,
            protected,
            None,
        )
        with self._lock:
            self._db.execute("INSERT INTO jobs VALUES(?,?,?,?,?,?,?,?,?,?)", values)
            self._append(job_id, now, "state", JobState.QUEUED.value)
            self._db.commit()
        return self.get(job_id)

    def get(self, job_id: str) -> Job:
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM jobs WHERE job_id=?",
                (job_id,),
            ).fetchone()
        if row is None:
            raise KeyError(job_id)
        return self._job(row)

    def active(self) -> tuple[Job, ...]:
        query = (
            "SELECT * FROM jobs WHERE state IN (?,?,?) "
            "ORDER BY created_at ASC, job_id ASC"
        )
        with self._lock:
            rows = self._db.execute(query, _ACTIVE_STATES).fetchall()
        return tuple(self._job(row) for row in rows)

    def recent(self, n: int) -> tuple[Job, ...]:
        if isinstance(n, bool) or not isinstance(n, int) or not 1 <= n <= 100:
            raise ValueError("invalid limit")
        query = (
            "SELECT * FROM jobs WHERE state IN (?,?,?) "
            "ORDER BY updated_at DESC, job_id DESC LIMIT ?"
        )
        parameters = (*_TERMINAL_STATES, n)
        with self._lock:
            rows = self._db.execute(query, parameters).fetchall()
        return tuple(self._job(row) for row in rows)

    def brief(self, job_id: str) -> str:
        value = self._private(job_id, "brief")
        if value is None:
            return ""
        return value

    def result(self, job_id: str) -> str | None:
        return self._private(job_id, "result")

    def set_result(self, job_id: str, text: str) -> None:
        if not isinstance(text, str) or not text:
            raise ValueError("invalid result")
        if len(text.encode("utf-8")) > 65_536:
            raise ValueError("invalid result")
        value = self._codec.protect(
            text.encode("utf-8"),
            entropy=self._entropy(job_id, "result"),
        )
        with self._lock:
            changed = self._db.execute(
                "UPDATE jobs SET result=? WHERE job_id=?",
                (value, job_id),
            ).rowcount
            if changed != 1:
                raise KeyError(job_id)
            self._db.commit()

    def transition(
        self,
        job_id: str,
        state: JobState | str,
        *,
        session_id: str | None = None,
        summary: str | None = None,
        error: str | None = None,
    ) -> Job:
        target = state if isinstance(state, JobState) else JobState(state)
        now = float(self._clock())
        query = (
            "UPDATE jobs SET state=?,session_id=COALESCE(?,session_id),"
            "updated_at=?,summary=?,error=? WHERE job_id=?"
        )
        parameters = (
            target.value,
            session_id,
            now,
            summary,
            error,
            job_id,
        )
        with self._lock:
            changed = self._db.execute(query, parameters).rowcount
            if changed != 1:
                raise KeyError(job_id)
            self._append(job_id, now, "state", target.value)
            self._db.commit()
        return self.get(job_id)

    def append_output(self, job_id: str, text: str) -> JobEvent:
        if not isinstance(text, str):
            raise TypeError("output must be text")
        clean = "".join(
            character
            for character in text
            if ord(character) >= 32 and ord(character) != 127
        )[:2_048]
        now = float(self._clock())
        with self._lock:
            count = self._db.execute(
                "SELECT COUNT(*) FROM events WHERE job_id=? AND kind='output'",
                (job_id,),
            ).fetchone()[0]
            if count >= 2_000:
                raise ValueError("output limit reached")
            sequence = self._append(job_id, now, "output", clean)
            self._db.commit()
        return JobEvent(sequence, now, "output", clean)

    def events(self, job_id: str, after: int = 0) -> tuple[JobEvent, ...]:
        query = (
            "SELECT sequence,timestamp,kind,text FROM events "
            "WHERE job_id=? AND sequence>? ORDER BY sequence"
        )
        with self._lock:
            rows = self._db.execute(query, (job_id, after)).fetchall()
        return tuple(
            JobEvent(row["sequence"], row["timestamp"], row["kind"], row["text"])
            for row in rows
        )

    def _append(self, job_id: str, now: float, kind: str, text: str) -> int:
        row = self._db.execute(
            "SELECT COALESCE(MAX(sequence),0)+1 FROM events WHERE job_id=?",
            (job_id,),
        ).fetchone()
        sequence = row[0]
        self._db.execute(
            "INSERT INTO events VALUES(?,?,?,?,?)",
            (job_id, sequence, now, kind, text),
        )
        return sequence

    def _private(self, job_id: str, field: str) -> str | None:
        with self._lock:
            row = self._db.execute(
                f"SELECT {field} FROM jobs WHERE job_id=?",
                (job_id,),
            ).fetchone()
        if row is None:
            raise KeyError(job_id)
        if row[0] is None:
            return None
        plaintext = self._codec.unprotect(
            bytes(row[0]),
            entropy=self._entropy(job_id, field),
        )
        return plaintext.decode("utf-8")

    @staticmethod
    def _job(row: sqlite3.Row) -> Job:
        return Job(
            row["job_id"],
            row["title"],
            JobState(row["state"]),
            row["session_id"],
            row["created_at"],
            row["updated_at"],
            row["summary"],
            row["error"],
        )


__all__ = ["JobState", "Job", "JobEvent", "JobStore"]
