"""Bounded, local, redacted conversation persistence (DD-4).

WHAT THIS IS. A CLI session remembers what you said to it earlier in the
session; Atlas forgot everything the moment its window closed. This store is
the disk half of that: the last N turns are seeded back into the brain at
boot, and everything else inside the retention window is reachable only when
the model asks for it by calling search_transcript. Recent tail eagerly,
older history on demand -- never the whole 30 days in the prefix.

WHAT IS STORED, exactly, and nothing else:
  - a turn timestamp (unix seconds),
  - a role -- 'user' or 'assistant', nothing else is accepted,
  - the SPOKEN text: what Daniel said, and what Atlas said back, after
    sanitize.redact_secrets and after a per-row length cap,
  - the NAMES of tools the turn touched, each one checked against the live
    registry and replaced with 'other' when it is not a registered name.

WHAT IS NEVER STORED, structurally rather than by convention:
  - tool ARGUMENTS. Only names reach record_exchange, and the column that
    holds them accepts nothing else.
  - prompts, the system prefix, persona text, capability text. The brain
    hands this store two strings per exchange and never its message array.
  - raw child stdout, MCP child environments.

WHAT IS BOUNDED RATHER THAN ABSENT, stated separately because the difference
matters and an earlier draft of this file blurred it:
  - MCP TOOL RESULTS. Almost never on the path -- but the confirmation lane's
    failure line quotes up to 160 characters of a tool's own result
    ("That didn't go through: ..."), and that line is what the turn persists.
    It goes through the same redaction and the same cap as anything else, and
    since the DD-4 rework it also forces the exchange to be recorded TAINTED,
    so it can never come back as an untainted boot seed. See
    Brain.respond's confirm branch and the amendment's 6b.
  - pairing tokens, shutdown tokens, credentials, private environment values.
    Rule 1 keeps them out of the brain's text in the first place; the
    redaction pass is a shape filter behind it, not a guarantee. It catches
    the 19 realistic credential shapes an adversarial review wrote at it; it
    cannot catch a passphrase of ordinary words dictated with no marker in
    front of it, and the amendment's 3c says so rather than implying it can.

RULE 10. This file is a new persistent sink and it is the first one in Atlas
that holds CONTENT rather than host-shaped metadata. That is a real extension
of what lives on disk, which is why the feature ships dark
(persistence.enabled defaults to false) behind
docs/amendments/dd4-rule10-transcript.md and Daniel's explicit sign-off.

ENCRYPTION AT REST -- decided against, on measurement. See the amendment for
the full write-up; the short version is that the only stdlib-reachable cipher
is Windows DPAPI through ctypes (CryptProtectData), it costs a measured
~1.2ms of fixed overhead PER CALL, and a keyword search over a full 30-day
store would therefore have to decrypt ~18,000 rows one at a time -- about 23
seconds for a tool that is supposed to answer inside a voice turn. Segmenting
many turns into one blob fixes the speed and buys an append-log with
re-encrypted tails, coarse eviction, and a new corruption mode. And it would
be protecting against the wrong thing: DPAPI is keyed to Daniel's Windows
logon, so any process already running as Daniel -- which is the threat that
matters for a file in his own profile -- decrypts it for free. There is no
AES in the stdlib and a hand-rolled cipher is worse than none, so the store is
plaintext-redacted, and the amendment names the file's actual on-disk ACLs as
a decision for Daniel rather than an assumption.
"""
from __future__ import annotations

from datetime import datetime
import logging
import os
from pathlib import Path
import re
import sqlite3
import threading
import time
from typing import Any, Callable, Iterable, Sequence

from . import sanitize

__all__ = ["TranscriptStore", "configured_path", "default_path"]

logger = logging.getLogger("atlas.transcript")

_ROLES = ("user", "assistant")
# Reused verbatim from worker/traces.py: a configured path that still contains
# an unexpanded %VAR%/$VAR is a misconfiguration, not a directory name.
_UNRESOLVED = re.compile(r"%(?:[^%]+)%|\$(?:[A-Za-z_][A-Za-z0-9_]*|\{[^}]+\})")
# Same shape the registry accepts (tools._TOOL_NAME). A name that clears this
# AND is registered is host vocabulary; anything else becomes 'other'.
_TOOL_NAME = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_OTHER = "other"
_MAX_TOOLS_PER_TURN = 8

# Per-row text cap. brain.MAX_TRANSCRIPT already bounds an utterance at 4096
# characters and a reply is bounded by max_tokens (500 -> ~2000 characters),
# so this is a backstop that should never bind, not a working limit.
MAX_TEXT_CHARS = 4096
_TRUNCATED = " ...[truncated]"

# Store bounds. TWO caps, whichever binds first, oldest-first eviction:
#
#   max_rows = 20_000 -- 10,000 exchanges. At a heavy 300 turns a day that is
#     over a month of conversation, so retention normally bins rows before
#     this does.
#   max_content_bytes = 4,194,304 CHARACTERS -- the sum of LENGTH(text) over
#     the table, and SQLite's LENGTH counts characters, not bytes. Roughly
#     13,000 turns at 300 characters each: again more than 30 days of real
#     use. It is the backstop against a pathological run (a wall of pasted
#     text, a loop), which is exactly the case where a row count alone would
#     not save us. File size is the wrong meter to cap against -- SQLite does
#     not give pages back on DELETE, so an eviction loop measured against it
#     would never converge.
#
# The NAME says bytes and the unit is characters, so the disk cost depends on
# the script (LOW-7 in the DD-4 rework review; the amendment's §4 carries the
# table). Measured at a full cap: ASCII 5.08 MB main file, CJK 14.51 MB, emoji
# 19.42 MB -- plus a write-ahead log that peaked at 4.13 MB during the run and
# is checkpointed away on close. So the honest envelope is ~9 MB for English
# and ~20-24 MB for CJK/emoji, not "4 MiB". Incremental auto-vacuum keeps the
# freed pages from accumulating after evictions.
MAX_ROWS = 20_000
MAX_CONTENT_BYTES = 4 * 1024 * 1024
RETENTION_DAYS = 30

# Clock-fault thresholds (LOW-9). A gap between the newest stored row and
# "now" larger than twice the retention window -- and never smaller than 90
# days, so a short retention setting cannot make the guard hair-trigger -- is
# read as a clock that moved rather than as time that passed. An hour of
# backward slack absorbs a DST step or a small NTP correction without
# tripping.
_CLOCK_JUMP_FLOOR_S = 90 * 86_400.0
_CLOCK_BACKWARD_SLACK_S = 3_600.0
# Re-warn on a store that keeps failing, at these attempt counts (INFO-10).
_ERROR_REPORT_AT = frozenset({10, 100, 1_000})
# "no argument given", distinct from an explicit None (which means "there is
# no newest row").
_UNSET = object()

# Boot seed. ~1500 tokens against a prefix that is already ~25K: enough for
# roughly the last ten exchanges of ordinary conversation, small enough that
# it is noise against the cached prefix rather than a second prefix.
SEED_TOKEN_BUDGET = 1_500
# 20 ROWS -- ten exchanges. Deliberately a turn count and not a byte count:
# what makes "as if the session never closed" true is the last handful of
# exchanges, whatever length they happened to be.
SEED_MAX_TURNS = 20
# ...and only from the last 24 hours. A three-day-old tail is not context, it
# is a wrong assumption the model would carry into the first live turn; that
# far back is what search_transcript is for.
SEED_MAX_AGE_HOURS = 24
# Rough and deliberately conservative: 4 characters per token overstates
# nothing for English prose, so the budget binds a little early rather than a
# little late. Nothing here justifies a tokenizer dependency (rule 11).
_CHARS_PER_TOKEN = 4

# Search bounds, all of them caps the model cannot raise.
SEARCH_MAX_RESULTS = 20
SEARCH_DEFAULT_RESULTS = 8
SEARCH_SNIPPET_CHARS = 240
SEARCH_MAX_TERMS = 5

_SCHEMA = """
CREATE TABLE IF NOT EXISTS turns (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at REAL NOT NULL,
    role TEXT NOT NULL CHECK(role IN ('user','assistant')),
    text TEXT NOT NULL,
    tools TEXT NOT NULL DEFAULT '',
    tainted INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS turns_at ON turns(at);
"""


def default_path() -> Path:
    base = os.environ.get("LOCALAPPDATA")
    return ((Path(base) if base else Path.home() / "AppData" / "Local")
            / "Atlas" / "transcript.db")


def configured_path(value: str | Path | None) -> Path:
    if value is None:
        return default_path()
    expanded = os.path.expandvars(str(value))
    return default_path() if _UNRESOLVED.search(expanded) else Path(expanded)


def _positive_int(value: Any, fallback: int, floor: int = 1) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < floor:
        return fallback
    return value


# An exchange is TWO rows, so a row cap below two exchanges cannot hold a
# conversation: the eviction that runs as each write lands would take the
# write with it, leaving a store that reports itself enabled and retains
# nothing (re-review LOW-F). runtime._positive_int raises on a configured
# value below this; here, where the constructor's contract is to clamp rather
# than raise, it falls back to the default instead.
MIN_ROWS = 4


class TranscriptStore:
    """One SQLite file of redacted conversation turns, bounded three ways.

    Synchronous on purpose, unlike TraceRecorder's queue-and-thread. A trace
    row is written mid-turn and must not cost the turn anything; a transcript
    row is written from Brain._remember, in the `finally` AFTER every spoken
    chunk has already been yielded, so a millisecond there is a millisecond
    nobody is waiting on. Synchronous also means a test can assert on the file
    the instant record_exchange returns, with no thread to join and no flush to
    race -- which is most of why the pinned tests below are readable.

    Never raises at its callers. Every database failure is swallowed behind a
    once-only host-shaped warning: losing conversation history is not a reason
    to lose the turn.

    One connection behind one lock. search() runs on a worker thread (the tool
    hands it to asyncio.to_thread, because a keyword scan is disk work and the
    loop it would otherwise run on is carrying Daniel's audio), so a search in
    flight can make a concurrent record_exchange wait. That is bounded by the
    same caps everything else here is: a LIKE scan of at most max_rows rows
    with a LIMIT on it, tens of milliseconds against a full store -- and it can
    only ever delay a write that is already happening after the turn has
    finished speaking.
    """

    def __init__(
        self,
        path: str | Path | None = None,
        *,
        enabled: bool = True,
        retention_days: int = RETENTION_DAYS,
        max_rows: int = MAX_ROWS,
        max_content_bytes: int = MAX_CONTENT_BYTES,
        seed_token_budget: int = SEED_TOKEN_BUDGET,
        seed_max_turns: int = SEED_MAX_TURNS,
        seed_max_age_hours: int = SEED_MAX_AGE_HOURS,
        # A CALLABLE, not a snapshot: MCP tools register minutes after this
        # store is built, and a snapshot taken at construction would file
        # every one of them under 'other'. Bound to ToolRegistry.names by
        # runtime.build, so the allowlist is always the live registry.
        tool_names: Callable[[], Sequence[str]] | None = None,
        clock: Callable[[], float] = time.time,
        local_tz=None,
    ) -> None:
        self.path = configured_path(path)
        self.enabled = bool(enabled)
        self.retention_days = _positive_int(retention_days, RETENTION_DAYS)
        self.max_rows = _positive_int(max_rows, MAX_ROWS, floor=MIN_ROWS)
        self.max_content_bytes = _positive_int(max_content_bytes, MAX_CONTENT_BYTES)
        self.seed_token_budget = _positive_int(seed_token_budget, SEED_TOKEN_BUDGET)
        self.seed_max_turns = _positive_int(seed_max_turns, SEED_MAX_TURNS)
        self.seed_max_age_hours = _positive_int(seed_max_age_hours, SEED_MAX_AGE_HOURS)
        self._tool_names = tool_names
        self._clock = clock
        self._local_tz = local_tz
        self._lock = threading.RLock()
        self._connection: sqlite3.Connection | None = None
        self._closed = False
        self._warned_database = False
        self._warned_clock = False
        self._database_errors = 0
        # Running totals, so the bounds check on every write costs no scan of
        # the whole table. Computed exactly once when the connection opens,
        # and thereafter carried forward: incremented by what a write adds,
        # decremented by what an eviction removes (_delete_where).
        self._rows = 0
        self._bytes = 0
        self._oldest_at: float | None = None
        # The newest row Atlas itself wrote -- the only evidence available for
        # deciding whether the wall clock is sane (_clock_is_trustworthy).
        self._newest_at: float | None = None

    # ---------------------------------------------------------------- writes

    def record_exchange(
        self,
        *,
        said: str,
        spoken: str,
        tools: Iterable[str] = (),
        tainted: bool = False,
    ) -> None:
        """Persist one exchange: what Daniel said, then what Atlas said back.

        The tool names ride on the ASSISTANT row -- they are what that reply
        did, not what the utterance was.

        `tainted` is the turn's own taint flag. The taint wall is per-turn and
        would not otherwise survive a restart; seed_text is where this is
        spent, and its docstring says what it buys.
        """
        if not self.enabled:
            return
        rows = []
        for role, text, names in (
            ("user", said, ()),
            ("assistant", spoken, tools),
        ):
            clean = self._clean_text(text)
            if clean:
                rows.append((role, clean, self._clean_tools(names)))
        if not rows:
            return
        at = float(self._clock())
        with self._lock:
            if self._closed:
                return
            try:
                connection = self._connect(create=True)
                if connection is None:
                    return
                # Captured BEFORE the insert: this row is stamped with the
                # clock the guard is there to judge (LOW-9).
                newest_before = self._newest_at
                mark = int(tainted is True)
                with connection:
                    connection.executemany(
                        "INSERT INTO turns (at, role, text, tools, tainted) "
                        "VALUES (?,?,?,?,?)",
                        [(at, role, text, tools_text, mark)
                         for role, text, tools_text in rows],
                    )
                for _role, text, _tools_text in rows:
                    self._rows += 1
                    self._bytes += len(text)
                if self._oldest_at is None:
                    self._oldest_at = at
                self._newest_at = at
                self._enforce_bounds(connection, newest_before)
            except Exception as exc:
                self._database_error(exc)

    def sweep(self) -> None:
        """Boot sweep: apply retention to a store that already exists.

        Deliberately does NOT create the file. A worker that starts with
        persistence on but has never recorded anything leaves nothing behind
        until the first exchange -- which is also what makes "flag off, no
        file" checkable as a property of the whole unit rather than of one
        branch.
        """
        if not self.enabled:
            return
        with self._lock:
            if self._closed:
                return
            try:
                connection = self._connect(create=False)
                if connection is not None:
                    self._enforce_bounds(connection)
            except Exception as exc:
                self._database_error(exc)

    def close(self) -> None:
        with self._lock:
            self._closed = True
            if self._connection is not None:
                try:
                    self._connection.close()
                except Exception as exc:
                    self._database_error(exc)
                self._connection = None

    # ----------------------------------------------------------------- reads

    def seed_text(self) -> str:
        """Render the recent tail as one plain block, or "" when there is none.

        Returns the CONVERSATION only. The framing that tells the model this
        is prior-session history and not a live request belongs to the brain
        (Brain.seed_prior_session), which owns every other word the model is
        given; this side owns what is true about the store.

        TAINTED EXCHANGES ARE EXCLUDED, and this is the security-relevant line
        in this file. The taint wall is per-turn: a turn that read a file or an
        MCP result cannot then act on a free-text target, and the wall comes
        down when the turn ends. Without this filter the seed would walk
        straight around that -- text laundered through Atlas's own spoken reply
        on a tainted turn would come back at the NEXT BOOT as an untainted
        prefix, with the wall already down and the whole tool surface open.
        Seeding is the one path where old content re-enters the model
        unprompted, so it takes only turns that were clean when they happened.
        Tainted turns are still SEARCHABLE: search_transcript is declared
        content_bearing, so anything it returns taints the turn it lands in,
        exactly as the original read did.
        """
        if not self.enabled:
            return ""
        cutoff = float(self._clock()) - self.seed_max_age_hours * 3600.0
        with self._lock:
            if self._closed:
                return ""
            try:
                connection = self._connect(create=False)
                if connection is None:
                    return ""
                rows = connection.execute(
                    "SELECT role, text FROM turns WHERE at >= ? AND tainted = 0 "
                    "ORDER BY id DESC LIMIT ?",
                    (cutoff, self.seed_max_turns),
                ).fetchall()
            except Exception as exc:
                self._database_error(exc)
                return ""
        budget = self.seed_token_budget * _CHARS_PER_TOKEN
        lines: list[str] = []
        spent = 0
        # Newest first, so what survives a tight budget is the most recent
        # conversation rather than the oldest thing inside the window.
        for role, text in rows:
            line = f"{self._speaker(role)}: {text}"
            if spent + len(line) > budget:
                break
            lines.append(line)
            spent += len(line) + 1
        lines.reverse()
        return "\n".join(lines)

    def search(
        self,
        query: str,
        *,
        hours: int | None = None,
        limit: int = SEARCH_DEFAULT_RESULTS,
    ) -> list[dict[str, Any]]:
        """Keyword search over the store, newest first, hard-bounded.

        Every whitespace-separated term (up to SEARCH_MAX_TERMS) must appear;
        matching is SQLite's ASCII-case-insensitive LIKE with % and _ escaped,
        so a query is a set of literal substrings and never a pattern the
        model gets to author.
        """
        if not self.enabled:
            return []
        terms = [term for term in str(query).split() if term][:SEARCH_MAX_TERMS]
        if not terms:
            return []
        capped = min(max(1, int(limit)), SEARCH_MAX_RESULTS)
        if hours is None:
            cutoff = 0.0
        else:
            # Clamped to the store's OWN retention, not to the module default:
            # a lookback wider than the window is not an error, it just cannot
            # mean more than "everything kept".
            span = min(max(1, int(hours)), self.retention_days * 24)
            cutoff = float(self._clock()) - span * 3600.0
        clauses = " AND ".join(["text LIKE ? ESCAPE '\\'"] * len(terms))
        parameters: list[Any] = [cutoff]
        parameters.extend(f"%{self._like_literal(term)}%" for term in terms)
        parameters.append(capped)
        with self._lock:
            if self._closed:
                return []
            try:
                connection = self._connect(create=False)
                if connection is None:
                    return []
                rows = connection.execute(
                    f"SELECT at, role, text, tools FROM turns WHERE at >= ? AND {clauses} "
                    "ORDER BY id DESC LIMIT ?",
                    parameters,
                ).fetchall()
            except Exception as exc:
                self._database_error(exc)
                return []
        return [
            {
                "when": self._when(at),
                "who": self._speaker(role),
                "text": self._snippet(text),
                **({"tools": tools.split(",")} if tools else {}),
            }
            for at, role, text, tools in rows
        ]

    # --------------------------------------------------------------- private

    def _connect(self, *, create: bool) -> sqlite3.Connection | None:
        if self._connection is not None:
            return self._connection
        if not create and not self.path.exists():
            return None
        if create:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=1.5, check_same_thread=False)
        # INCREMENTAL rather than the default NONE: eviction is the normal
        # case here, not an exception, and without it a store that evicted a
        # megabyte would keep the file that size forever.
        connection.execute("PRAGMA auto_vacuum=INCREMENTAL")
        connection.execute("PRAGMA journal_mode=WAL")
        connection.executescript(_SCHEMA)
        # CREATE TABLE IF NOT EXISTS leaves an EXISTING table untouched, so a
        # store written by an older build keeps its old columns and every
        # later INSERT fails on a name this code names. That failure is
        # swallowed (see _database_error), which would turn a schema change
        # into silent, total loss of persistence rather than a crash anyone
        # would notice. Same guard, same reason, as worker/traces.py's.
        columns = {row[1] for row in connection.execute("PRAGMA table_info(turns)")}
        for name, definition in (("tainted", "INTEGER NOT NULL DEFAULT 0"),):
            if name not in columns:
                connection.execute(f"ALTER TABLE turns ADD COLUMN {name} {definition}")
        connection.commit()
        self._connection = connection
        self._recount(connection)
        self._enforce_bounds(connection)
        return connection

    def _recount(self, connection: sqlite3.Connection) -> None:
        """Full COUNT(*)+SUM(LENGTH) scan. Only on open -- see _delete_where.

        This is the one place that pays for the whole table, and it is on the
        connection path rather than the write path deliberately: a boot pays
        it once (§10 of the amendment measures it), a turn never does.
        """
        row = connection.execute(
            "SELECT COUNT(*), COALESCE(SUM(LENGTH(text)),0), MIN(at), MAX(at) FROM turns",
        ).fetchone()
        self._rows, self._bytes = int(row[0]), int(row[1])
        self._oldest_at, self._newest_at = row[2], row[3]

    def _delete_where(
        self, connection: sqlite3.Connection, clause: str, parameters: tuple,
    ) -> bool:
        """Delete a range and DECREMENT the running totals from it.

        DD-4 rework, MEDIUM-5. This used to call _recount() after every
        eviction, which made a write at the row cap a full COUNT(*) +
        SUM(LENGTH(text)) scan of 20,000 rows -- tens to hundreds of
        milliseconds, on the asyncio loop carrying Daniel's audio, on a
        codebase with a freeze history. Nothing about that was O(1).

        The measurement instead of the scan: the same WHERE clause the DELETE
        uses, aggregated first. Both clauses this is called with are
        index-served -- `id <= ?` is a rowid range, `at < ?` uses turns_at --
        so it touches only the rows about to go, not the ones that stay. MIN
        and MAX are answered from turns_at the same way.
        """
        row = connection.execute(
            f"SELECT COUNT(*), COALESCE(SUM(LENGTH(text)),0) FROM turns WHERE {clause}",
            parameters,
        ).fetchone()
        if not row[0]:
            return False
        with connection:
            connection.execute(f"DELETE FROM turns WHERE {clause}", parameters)
        self._rows -= int(row[0])
        self._bytes -= int(row[1])
        # Two statements, not "SELECT MIN(at), MAX(at)": SQLite turns a LONE
        # MIN or MAX over an indexed column into an index seek, but asks for
        # both in one query and it scans the whole index instead.
        self._oldest_at = connection.execute("SELECT MIN(at) FROM turns").fetchone()[0]
        self._newest_at = connection.execute("SELECT MAX(at) FROM turns").fetchone()[0]
        return True

    def _clock_is_trustworthy(self, now: float, newest_at: float | None) -> bool:
        """Is the wall clock sane enough to delete a month of history on?

        DD-4 rework, LOW-9. Retention is wall-clock arithmetic, so a machine
        whose clock jumps forward -- a dead CMOS battery, a bad NTP step, a
        hand-typed date -- would compute a cutoff past every row in the store
        and empty it on the next write. There is no trusted clock to check
        against, so this checks the only other evidence there is: the newest
        row Atlas itself wrote.

        A gap larger than twice the retention window (and never less than 90
        days) is treated as a clock fault rather than as elapsed time, and
        retention is skipped for that pass. It self-corrects immediately: the
        very next recorded exchange lands at `now`, so the following pass sees
        a normal gap and applies retention as usual. A clock that reads BEFORE
        the newest stored row is the same kind of fault seen from the other
        side, and is skipped for the same reason.

        What this does NOT catch, and the amendment says so: a forward jump
        SMALLER than that threshold is indistinguishable from Atlas simply not
        having been opened for a month, and is honoured as elapsed time.

        `newest_at` is the newest row as of BEFORE the write that triggered
        this pass. It has to be: record_exchange has already inserted a row
        stamped with the faulty clock by the time bounds are enforced, so
        reading self._newest_at here would compare the bad clock against
        itself and always find it sane.
        """
        if newest_at is None:
            return True
        gap = now - float(newest_at)
        return -_CLOCK_BACKWARD_SLACK_S <= gap <= max(
            2 * self.retention_days * 86_400.0, _CLOCK_JUMP_FLOOR_S,
        )

    def _enforce_bounds(
        self,
        connection: sqlite3.Connection,
        newest_before: Any = _UNSET,
    ) -> None:
        """Retention first, then the two size caps, oldest row first.

        `newest_before` is the newest stored row as of before whatever
        triggered this pass -- record_exchange passes the value it saw BEFORE
        inserting, so the clock guard has something independent of the write
        to judge. Sweep and open pass nothing and use the stored value.
        """
        now = float(self._clock())
        cutoff = now - self.retention_days * 86_400.0
        if newest_before is _UNSET:
            newest_before = self._newest_at
        changed = False
        if self._oldest_at is not None and self._oldest_at < cutoff:
            if self._clock_is_trustworthy(now, newest_before):
                changed = self._delete_where(connection, "at < ?", (cutoff,))
            elif not self._warned_clock:
                logger.warning(
                    "conversation store: clock moved further than the retention "
                    "window in one step; deferring retention for this pass",
                )
                self._warned_clock = True
        # One statement per pass, then re-check: DELETE ... ORDER BY LIMIT
        # needs a compile option SQLite is not guaranteed to have, so the
        # excess is taken by id range instead, and a row's own length decides
        # how many rows the byte cap costs.
        while self._rows > self.max_rows or self._bytes > self.max_content_bytes:
            if self._rows > self.max_rows:
                # A BATCH, not the exact surplus (DD-4 rework, MEDIUM-5). At
                # the cap the exact surplus is 2 -- one exchange -- so every
                # single write from then on paid for an eviction. Taking 1% of
                # the cap instead means one write in a hundred does, and the
                # store still never sits above max_rows.
                surplus = self._rows - self.max_rows + max(1, self.max_rows // 100)
            else:
                # Bytes are not rows; evict a proportional slice and re-check
                # rather than guessing an exact row count from a byte deficit.
                surplus = max(1, (self._rows + 9) // 10)
            boundary = connection.execute(
                "SELECT id FROM turns ORDER BY id LIMIT 1 OFFSET ?", (surplus - 1,),
            ).fetchone()
            if boundary is None:
                with connection:
                    connection.execute("DELETE FROM turns")
                changed = True
                self._recount(connection)
                break
            if not self._delete_where(connection, "id <= ?", (boundary[0],)):
                break
            changed = True
        if changed:
            connection.execute("PRAGMA incremental_vacuum")
            connection.commit()

    def _clean_text(self, value: Any) -> str:
        """Redact, collapse, cap. The only door text goes through."""
        if not isinstance(value, str):
            return ""
        text = sanitize.redact_secrets(value).strip()
        if len(text) > MAX_TEXT_CHARS:
            text = text[:MAX_TEXT_CHARS - len(_TRUNCATED)] + _TRUNCATED
        return text

    def _clean_tools(self, names: Iterable[str]) -> str:
        """Names only, and only names the live registry actually has.

        A tool name in `tool_results` is the name the MODEL asked for, which
        is not the same as a name the host has -- a call that was refused
        still leaves its requested name behind. Checking against the registry
        rather than against a regex is what keeps a model-authored string out
        of a persistent file (rule 10); anything unrecognised is recorded as
        'other', so the fact that SOMETHING ran is not lost with it.
        """
        known = self._known_tools()
        out: list[str] = []
        for name in names:
            if len(out) >= _MAX_TOOLS_PER_TURN:
                break
            clean = (
                name
                if isinstance(name, str) and _TOOL_NAME.fullmatch(name) and name in known
                else _OTHER
            )
            if clean not in out:
                out.append(clean)
        return ",".join(out)

    def _known_tools(self) -> frozenset[str]:
        if self._tool_names is None:
            return frozenset()
        try:
            return frozenset(
                name for name in self._tool_names() if isinstance(name, str)
            )
        except Exception:
            return frozenset()

    def _when(self, at: Any) -> str:
        try:
            moment = datetime.fromtimestamp(float(at), self._local_tz)
        except (OSError, OverflowError, TypeError, ValueError):
            return "unknown"
        return moment.isoformat(timespec="minutes")

    @staticmethod
    def _speaker(role: Any) -> str:
        return "Daniel" if role == _ROLES[0] else "Atlas"

    @staticmethod
    def _snippet(text: Any) -> str:
        value = " ".join(str(text).split())
        return value if len(value) <= SEARCH_SNIPPET_CHARS else (
            value[:SEARCH_SNIPPET_CHARS - 3] + "..."
        )

    @staticmethod
    def _like_literal(term: str) -> str:
        return (
            term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        )

    @property
    def degraded(self) -> bool:
        """True once any database operation has failed.

        DD-4 rework, INFO-10. A corrupted store is permanent and, behind one
        once-only warning in a file nobody reads, silent: every search returns
        "nothing matched", which is the same answer a working store gives for
        a question it has no record of. That is the worst shape a failure can
        take -- indistinguishable from success. search_transcript reads this
        so the model says the store could not be read instead, which puts the
        failure in front of Daniel in the one channel he is actually using.
        """
        return self._database_errors > 0

    def _database_error(self, exc: Exception) -> None:
        """Swallow, but do not fall silent (INFO-10).

        Never raises at its callers -- losing conversation history is not a
        reason to lose the turn -- but the once-only warning was the ONLY
        signal a store had died. Now the count escalates by powers of ten, so
        a store failing every write says so again at 10, 100 and 1,000 rather
        than once at boot, and `degraded` gives the failure a voice.
        """
        self._database_errors += 1
        if not self._warned_database:
            logger.warning("conversation store database error: %s", type(exc).__name__)
            self._warned_database = True
        elif self._database_errors in _ERROR_REPORT_AT:
            logger.warning(
                "conversation store still failing after %d attempts: %s",
                self._database_errors,
                type(exc).__name__,
            )
