"""Append-only, metadata-only turn tracing."""
from __future__ import annotations
from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from datetime import datetime
import logging
import math
import os
from pathlib import Path
import queue
import re
import sqlite3
import threading
import time
from typing import Callable, Mapping
from uuid import uuid4
logger = logging.getLogger("atlas.traces")
_EMPTY = {"turns": 0, "avg_ms": 0.0, "tool_calls": 0, "input_tokens": 0,
          "output_tokens": 0, "cache_read_tokens": 0, "cache_write_tokens": 0,
          "cache_hit_ratio": 0.0, "cost_usd": 0.0}
_WAKE_KINDS = frozenset({"ambient", "reflex", "reply", "text", "wake"})
_OUTCOMES = frozenset({
    # "interrupted" is a turn that produced no speech BECAUSE Daniel barged in.
    # It used to be recorded as "empty", which put a real failure and a normal
    # interruption in the same bucket -- and made the host apologise for it.
    "asleep", "cancelled", "dismissed", "empty", "error", "ignored",
    "interrupted", "responded", "speech_failed",
})
_UNRESOLVED = re.compile(r"%(?:[^%]+)%|\$(?:[A-Za-z_][A-Za-z0-9_]*|\{[^}]+\})")
_STOP = object()
_SCHEMA = """
CREATE TABLE IF NOT EXISTS turns (
    turn_id TEXT PRIMARY KEY, started_at REAL NOT NULL, ended_at REAL NOT NULL,
    total_ms INTEGER NOT NULL, addressed INTEGER NOT NULL, wake_kind TEXT,
    outcome TEXT, input_tokens INTEGER NOT NULL, output_tokens INTEGER NOT NULL,
    cache_read_tokens INTEGER NOT NULL, cache_write_tokens INTEGER NOT NULL,
    cost_usd REAL NOT NULL, model TEXT
);
CREATE TABLE IF NOT EXISTS steps (
    turn_id TEXT NOT NULL REFERENCES turns(turn_id) ON DELETE CASCADE, seq INTEGER NOT NULL,
    kind TEXT NOT NULL CHECK(kind IN ('ROUTE','GENERATE','TOOL_CALL','RESPOND')), name TEXT,
    ms INTEGER NOT NULL, ok INTEGER NOT NULL, tokens_in INTEGER NOT NULL,
    tokens_out INTEGER NOT NULL, interrupted INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (turn_id, seq)
);
CREATE INDEX IF NOT EXISTS turns_started_at ON turns(started_at);
"""
@dataclass(eq=False)
class _Turn:
    turn_id: str
    started_at: float
    wake_kind: str
    steps: list[dict] = field(default_factory=list)
    ended: bool = False
    # Set by mark_speech_interrupted() (from a SpeechHandle done-callback
    # registered at speech_created time; see the diagnosis comment at the
    # AgentSession construction in worker/app.py) before respond() records
    # the RESPOND step, so respond() can read it off the turn without
    # app.py's response funnel having to pass it through explicitly.
    speech_interrupted: bool = False
@dataclass
class _SummaryRequest:
    cutoff: float
    event: threading.Event = field(default_factory=threading.Event)
    value: dict | None = None
_ACTIVE: ContextVar[tuple["TraceRecorder", _Turn] | None] = ContextVar(
    "atlas_active_trace", default=None,
)
def default_path() -> Path:
    base = os.environ.get("LOCALAPPDATA")
    return ((Path(base) if base else Path.home() / "AppData" / "Local")
            / "Atlas" / "traces.db")
def configured_path(value: str | Path | None) -> Path:
    if value is None:
        return default_path()
    expanded = os.path.expandvars(str(value))
    return default_path() if _UNRESOLVED.search(expanded) else Path(expanded)
def activate(recorder: "TraceRecorder", turn: _Turn) -> Token:
    return _ACTIVE.set((recorder, turn))
def reset(token: Token) -> None:
    _ACTIVE.reset(token)
def active_turn() -> tuple["TraceRecorder", _Turn] | None:
    """Expose the active (recorder, turn) pair for out-of-band session-level
    hooks (e.g. a SpeechHandle done-callback registered at speech_created
    time) that need to attribute a signal to the in-flight turn without
    going through record_current_respond/generate/tool_call."""
    return _ACTIVE.get()
def speech_was_interrupted() -> bool:
    """Did a barge-in cut the in-flight turn's speech?

    Read by the response funnels: a turn that ends with nothing said because
    Daniel talked over it is not a turn that failed, and must not be answered
    with an apology. False whenever there is no active turn (text lane, tests),
    which keeps the honest-silence fallback the default."""
    active = _ACTIVE.get()
    return active is not None and active[1].speech_interrupted is True
def mark_speech_interrupted(turn: _Turn) -> None:
    """Record that a speech handle created during `turn` was interrupted;
    read by TraceRecorder.respond() when it records the RESPOND step."""
    turn.speech_interrupted = True
def _current(method: str, *args, **metrics) -> None:
    active = _ACTIVE.get()
    if active is not None:
        getattr(active[0], method)(active[1], *args, **metrics)
def record_current_generate(model: str, **metrics) -> None:
    _current("generate", model, **metrics)
def record_current_tool_call(name: str, **metrics) -> None:
    _current("tool_call", name, **metrics)
def record_current_respond(**metrics) -> None:
    _current("respond", **metrics)
class TraceRecorder:
    """Queue complete turn inserts on one bounded private database thread."""
    def __init__(self, path: str | Path | None = None, *, enabled: bool = True,
                 pricing: Mapping[str, Mapping[str, float]] | None = None,
                 cache_ttl: str = "5m", tool_names=(), model_names=(),
                 retention_days: int = 30, clock: Callable[[], float] = time.time,
                 local_tz=None, queue_size: int = 512) -> None:
        self.path = configured_path(path)
        self.enabled = bool(enabled)
        self._pricing = dict(pricing or {})
        self._long_cache_write = cache_ttl == "1h"
        self._tool_names = frozenset(tool_names)
        self._model_names = frozenset(model_names)
        self._retention_days = max(1, int(retention_days))
        self._clock = clock
        self._local_tz = local_tz
        self._queue: queue.Queue = queue.Queue(maxsize=max(1, int(queue_size)))
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._connection: sqlite3.Connection | None = None
        self._health = dict(_EMPTY)
        self._health_cutoff: float | None = None
        self._warned_database = False
        self._warned_drop = False
        self._warned_boundary = False
        self._closed = False
    @property
    def health(self) -> dict[str, int | float | bool]:
        with self._lock:
            summary = self._health if self._health_cutoff == self._local_midnight() else _EMPTY
            return {"enabled": self.enabled, **summary}
    def begin_turn(self, *, wake_kind: str) -> _Turn:
        return _Turn(str(uuid4()), self._clock(), self._enum(wake_kind, _WAKE_KINDS))
    def route(self, turn: _Turn, *, ms: int, ok: bool) -> None:
        self._step(turn, "ROUTE", None, ms=ms, ok=ok)
    def generate(self, turn: _Turn, model: str, *, ms: int, ok: bool,
                 tokens_in: int = 0, tokens_out: int = 0, cache_read_tokens: int = 0,
                 cache_write_tokens: int = 0) -> None:
        name = self._name(model, self._model_names)
        for step in turn.steps:
            if step["kind"] == "GENERATE":
                step["ms"] = self._count(step["ms"] + self._count(ms))
                step["ok"] = int(bool(step["ok"]) and ok is True)
                for key, value in (("tokens_in", tokens_in), ("tokens_out", tokens_out),
                                   ("cache_read", cache_read_tokens),
                                   ("cache_write", cache_write_tokens)):
                    step[key] = self._count(step[key] + self._count(value))
                return
        self._step(turn, "GENERATE", name, ms=ms, ok=ok, tokens_in=tokens_in,
                   tokens_out=tokens_out, cache_read_tokens=cache_read_tokens,
                   cache_write_tokens=cache_write_tokens)
    def tool_call(self, turn: _Turn, name: str, *, ms: int, ok: bool) -> None:
        self._step(turn, "TOOL_CALL", self._name(name, self._tool_names), ms=ms, ok=ok)
    def respond(self, turn: _Turn, *, ms: int, ok: bool) -> None:
        self._step(turn, "RESPOND", None, ms=ms, ok=ok, interrupted=turn.speech_interrupted)
    def end_turn(self, turn: _Turn, *, addressed: bool, wake_kind: str,
                 outcome: str, total_ms: int | None = None) -> None:
        if not self.enabled:
            return
        with self._lock:
            if self._closed or turn.ended:
                return
            turn.ended = True
        ended_at = self._clock()
        elapsed = round((ended_at - turn.started_at) * 1000) if total_ms is None else total_ms
        row = self._turn_row(turn, ended_at, elapsed, addressed, wake_kind, outcome)
        self._enqueue(("write", row, tuple(turn.steps)))
    def summary(self, *, days: int = 1) -> dict[str, int | float]:
        if not self.enabled:
            return dict(_EMPTY)
        request = _SummaryRequest(self._clock() - max(1, int(days)) * 86_400)
        if not self._enqueue(("summary", request)) or not request.event.wait(2.0):
            return dict(_EMPTY)
        return request.value or dict(_EMPTY)
    def close(self, *, timeout_s: float = 2.0) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            thread = self._thread
        if thread is None:
            return
        self._enqueue(_STOP, after_close=True)
        thread.join(max(0.0, min(float(timeout_s), 2.0)))
    def _step(self, turn: _Turn, kind: str, name: str | None, *, ms: int, ok: bool,
              tokens_in: int = 0, tokens_out: int = 0, cache_read_tokens: int = 0,
              cache_write_tokens: int = 0, interrupted: bool = False) -> None:
        if not self.enabled or turn.ended or len(turn.steps) >= 64:
            return
        turn.steps.append({
            "kind": kind, "name": name, "ms": self._count(ms), "ok": int(ok is True),
            "tokens_in": self._count(tokens_in), "tokens_out": self._count(tokens_out),
            "cache_read": self._count(cache_read_tokens),
            "cache_write": self._count(cache_write_tokens),
            "interrupted": int(interrupted is True),
        })
    def _enqueue(self, item, *, after_close: bool = False) -> bool:
        with self._lock:
            if self._closed and not after_close:
                return False
            if self._thread is None:
                self._thread = threading.Thread(
                    target=self._run, name="atlas-traces", daemon=True,
                )
                self._thread.start()
        try:
            self._queue.put_nowait(item)
        except queue.Full:
            try:
                self._queue.get_nowait()
            except queue.Empty:
                pass
            self._queue.put_nowait(item)
            if not self._warned_drop:
                logger.warning("turn trace queue full; dropping oldest record")
                self._warned_drop = True
        return True
    def _run(self) -> None:
        while True:
            item = self._queue.get()
            if item is _STOP:
                break
            if item[0] == "write":
                self._write_turn(item[1], item[2])
            else:
                request = item[1]
                request.value = self._read_summary(request.cutoff)
                request.event.set()
        if self._connection is not None:
            self._connection.close()
            self._connection = None
    def _connect(self) -> sqlite3.Connection:
        if self._connection is None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            connection = sqlite3.connect(self.path, timeout=1.5)
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA foreign_keys=ON")
            connection.executescript(_SCHEMA)
            # CREATE TABLE IF NOT EXISTS leaves a pre-existing steps table
            # (from before the `interrupted` column existed) untouched; add
            # it explicitly so an existing traces.db from an older build
            # doesn't hit a column-count mismatch on the next INSERT.
            columns = {row[1] for row in connection.execute("PRAGMA table_info(steps)")}
            if "interrupted" not in columns:
                connection.execute(
                    "ALTER TABLE steps ADD COLUMN interrupted INTEGER NOT NULL DEFAULT 0",
                )
            cutoff = self._clock() - self._retention_days * 86_400
            connection.execute("DELETE FROM turns WHERE started_at < ?", (cutoff,))
            connection.commit()
            self._connection = connection
        return self._connection
    def _write_turn(self, row: tuple, steps: tuple[dict, ...]) -> None:
        try:
            connection = self._connect()
            with connection:
                connection.execute("INSERT INTO turns VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", row)
                connection.executemany(
                    "INSERT INTO steps VALUES (?,?,?,?,?,?,?,?,?)",
                    ((row[0], seq, step["kind"], step["name"], step["ms"], step["ok"],
                      step["tokens_in"], step["tokens_out"], step.get("interrupted", 0))
                     for seq, step in enumerate(steps, 1)),
                )
            cutoff = self._local_midnight()
            health = self._read_summary(cutoff)
            with self._lock:
                self._health = health
                self._health_cutoff = cutoff
        except Exception as exc:
            self._database_error(exc)
    def _read_summary(self, cutoff: float) -> dict[str, int | float]:
        try:
            connection = self._connect()
            row = connection.execute(
                "SELECT COUNT(*),COALESCE(AVG(total_ms),0),COALESCE(SUM(input_tokens),0),"
                "COALESCE(SUM(output_tokens),0),COALESCE(SUM(cache_read_tokens),0),"
                "COALESCE(SUM(cache_write_tokens),0),COALESCE(SUM(cost_usd),0) "
                "FROM turns WHERE started_at >= ?", (cutoff,),
            ).fetchone()
            tools = connection.execute(
                "SELECT COUNT(*) FROM steps JOIN turns USING(turn_id) "
                "WHERE kind='TOOL_CALL' AND started_at >= ?", (cutoff,),
            ).fetchone()[0]
        except Exception as exc:
            self._database_error(exc)
            return dict(_EMPTY)
        denominator = row[2] + row[4] + row[5]
        return {"turns": row[0], "avg_ms": row[1], "tool_calls": tools,
                "input_tokens": row[2], "output_tokens": row[3],
                "cache_read_tokens": row[4], "cache_write_tokens": row[5],
                "cache_hit_ratio": row[4] / denominator if denominator else 0.0,
                "cost_usd": row[6]}
    def _turn_row(self, turn, ended_at, elapsed, addressed, wake_kind, outcome):
        totals = [sum(step[key] for step in turn.steps) for key in
                  ("tokens_in", "tokens_out", "cache_read", "cache_write")]
        model = next((step["name"] for step in reversed(turn.steps)
                      if step["kind"] == "GENERATE"), None)
        prices = self._pricing.get(model or "", {})
        rates = [prices.get(key) for key in
                 ("input_per_mtok", "output_per_mtok", "cache_read_per_mtok",
                  "cache_write_per_mtok")]
        # A 1-hour cache write is billed at 2x base input, not 2x the 5-minute
        # cache-write rate; see the cache_ttl comment in config/atlas.yaml.
        rates[3] = (self._price(rates[0]) * 2.0 if self._long_cache_write
                    else self._price(rates[3]))
        cost = sum(total * self._price(rate) for total, rate in zip(totals, rates)) / 1e6
        return (turn.turn_id, turn.started_at, ended_at, self._count(elapsed),
                int(addressed is True), self._enum(wake_kind, _WAKE_KINDS),
                self._enum(outcome, _OUTCOMES), *totals, cost, model)
    def _local_midnight(self) -> float:
        current = datetime.fromtimestamp(self._clock(), self._local_tz)
        return current.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    def _name(self, value: str, allowed: frozenset) -> str:
        if isinstance(value, str) and value in allowed:
            return value
        self._boundary_warning()
        return "other"
    def _enum(self, value: str, allowed: frozenset) -> str:
        if isinstance(value, str) and value in allowed:
            return value
        self._boundary_warning()
        return "other"
    def _boundary_warning(self) -> None:
        if not self._warned_boundary:
            logger.warning("unknown trace metadata replaced with other")
            self._warned_boundary = True
    def _database_error(self, exc: Exception) -> None:
        if not self._warned_database:
            logger.warning("turn tracing database error: %s", type(exc).__name__)
            self._warned_database = True
    @staticmethod
    def _count(value) -> int:
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            return 0
        return min(max(0, int(value)), 2_147_483_647)
    @staticmethod
    def _price(value) -> float:
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            return 0.0
        return min(max(0.0, float(value)), 1_000_000.0)
