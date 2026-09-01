"""Metadata-only persistence for voice-turn traces."""
from __future__ import annotations

from datetime import datetime
from io import BytesIO
import logging
from pathlib import Path
import sqlite3
import struct
import threading
import time
from uuid import UUID
from zoneinfo import ZoneInfo

import pytest


PRICING = {"fast": {
    "input_per_mtok": 1.0, "output_per_mtok": 5.0,
    "cache_read_per_mtok": 0.1, "cache_write_per_mtok": 1.25,
}}


def _recorder(path, **kwargs):
    from worker.traces import TraceRecorder

    return TraceRecorder(
        path, pricing=PRICING, model_names=("fast",), tool_names=("find_file",),
        **kwargs,
    )


def _record_turn(recorder, *, tokens=(0, 0, 0, 0), total_ms=100,
                 wake_kind="wake", outcome="responded"):
    turn = recorder.begin_turn(wake_kind=wake_kind)
    recorder.route(turn, ms=5, ok=True)
    recorder.generate(
        turn, "fast", ms=total_ms - 15, ok=True, tokens_in=tokens[0],
        tokens_out=tokens[1], cache_read_tokens=tokens[2],
        cache_write_tokens=tokens[3],
    )
    recorder.tool_call(turn, "find_file", ms=5, ok=True)
    recorder.respond(turn, ms=5, ok=True)
    recorder.end_turn(
        turn, addressed=True, wake_kind=wake_kind, outcome=outcome, total_ms=total_ms,
    )
    return turn


def _new_york_2026() -> ZoneInfo:
    transitions = [
        int(datetime(2026, 3, 8, 7).timestamp()),
        int(datetime(2026, 11, 1, 6).timestamp()),
    ]
    header = b"TZif\0" + (b"\0" * 15) + struct.pack(">6l", 0, 0, 0, 2, 2, 8)
    body = struct.pack(">2l", *transitions) + bytes((1, 0))
    body += struct.pack(">lbb", -18_000, 0, 0) + struct.pack(">lbb", -14_400, 1, 4)
    return ZoneInfo.from_file(BytesIO(header + body + b"EST\0EDT\0"), key="Test/New_York")


def test_summary_math_uses_internal_uuid_and_kind_specific_steps(tmp_path: Path):
    recorder = _recorder(tmp_path / "traces.db")
    turn = _record_turn(recorder, tokens=(100, 20, 300, 40), total_ms=200)
    summary = recorder.summary(days=1)
    recorder.close()

    assert UUID(turn.turn_id).version == 4
    assert summary == {
        "turns": 1, "avg_ms": 200.0, "tool_calls": 1, "input_tokens": 100,
        "output_tokens": 20, "cache_read_tokens": 300, "cache_write_tokens": 40,
        "cache_hit_ratio": pytest.approx(300 / 440),
        "cost_usd": pytest.approx((100 + 20 * 5 + 300 * 0.1 + 40 * 1.25) / 1e6),
    }
    with sqlite3.connect(tmp_path / "traces.db") as connection:
        assert connection.execute(
            "SELECT kind FROM steps ORDER BY seq"
        ).fetchall() == [("ROUTE",), ("GENERATE",), ("TOOL_CALL",), ("RESPOND",)]


def test_unknown_host_metadata_is_other_with_one_warning(tmp_path, caplog):
    sentinel = "IdentifierSentinel94731"
    recorder = _recorder(tmp_path / "traces.db")
    with caplog.at_level(logging.WARNING, logger="atlas.traces"):
        turn = recorder.begin_turn(wake_kind=sentinel)
        recorder.generate(turn, sentinel, ms=1, ok=True)
        recorder.tool_call(turn, sentinel, ms=1, ok=False)
        recorder.end_turn(
            turn, addressed=True, wake_kind=sentinel, outcome=sentinel, total_ms=3,
        )
        recorder.close()

    with sqlite3.connect(tmp_path / "traces.db") as connection:
        row = connection.execute("SELECT wake_kind,outcome,model FROM turns").fetchone()
        names = connection.execute("SELECT name FROM steps ORDER BY seq").fetchall()
    assert row == ("other", "other", "other")
    assert names == [("other",), ("other",)]
    assert [record.message for record in caplog.records].count(
        "unknown trace metadata replaced with other"
    ) == 1
    assert sentinel.encode("ascii") not in (tmp_path / "traces.db").read_bytes()


def test_retention_removes_turns_older_than_thirty_days(tmp_path: Path):
    now = [1_800_000_000.0]
    old = _recorder(tmp_path / "traces.db", clock=lambda: now[0])
    now[0] -= 31 * 86_400
    _record_turn(old)
    old.close()

    now[0] += 31 * 86_400
    current = _recorder(tmp_path / "traces.db", clock=lambda: now[0])
    _record_turn(current)
    assert current.summary(days=365)["turns"] == 1
    current.close()


@pytest.mark.parametrize("day", [(2026, 1, 15), (2026, 11, 1)])
def test_health_rollup_uses_local_midnight_across_normal_and_dst_days(tmp_path, day):
    zone = _new_york_2026()
    now = [datetime(*day, 23, 59, tzinfo=zone).timestamp()]
    recorder = _recorder(tmp_path / "traces.db", clock=lambda: now[0], local_tz=zone)
    _record_turn(recorder)
    assert recorder.summary(days=1)["turns"] == 1
    now[0] = datetime.fromtimestamp(now[0], zone).replace(
        day=datetime.fromtimestamp(now[0], zone).day + 1, hour=0, minute=1,
    ).timestamp()
    assert recorder.health["turns"] == 0
    _record_turn(recorder)
    assert recorder.summary(days=1)["turns"] == 2
    assert recorder.health["turns"] == 1
    recorder.close()


def test_unresolved_configured_path_falls_back_without_localappdata(monkeypatch, tmp_path):
    from worker import traces

    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    expected = tmp_path / "AppData" / "Local" / "Atlas" / "traces.db"
    assert traces.default_path() == expected
    assert traces.configured_path("%MISSING_ATLAS_HOME%/trace.db") == expected
    assert traces.configured_path("$MISSING_ATLAS_HOME/trace.db") == expected


def test_one_hour_cache_write_pricing_uses_double_base_input_rate(tmp_path):
    recorder = _recorder(tmp_path / "traces.db", cache_ttl="1h")
    _record_turn(recorder, tokens=(100, 20, 300, 40))
    summary = recorder.summary(days=1)
    recorder.close()
    assert summary["cost_usd"] == pytest.approx(
        (100 + 20 * 5 + 300 * 0.1 + 40 * 1.0 * 2) / 1e6
    )


def test_bounded_queue_drops_oldest_once_and_close_has_deadline(tmp_path, caplog):
    recorder = _recorder(tmp_path / "traces.db", queue_size=2)
    entered = threading.Event()
    release = threading.Event()
    original = recorder._write_turn

    def blocked(*args):
        entered.set()
        release.wait(2)
        original(*args)

    recorder._write_turn = blocked
    with caplog.at_level(logging.WARNING, logger="atlas.traces"):
        _record_turn(recorder)
        assert entered.wait(1)
        for _ in range(6):
            _record_turn(recorder)
        started = time.perf_counter()
        recorder.close(timeout_s=0.05)
        elapsed = time.perf_counter() - started
    late = recorder.begin_turn(wake_kind="wake")
    recorder.end_turn(
        late, addressed=True, wake_kind="wake", outcome="responded", total_ms=1,
    )
    release.set()
    recorder._thread.join(2)

    assert elapsed < 0.2
    assert sum("queue full" in record.message for record in caplog.records) == 1


def test_disabled_recorder_is_a_no_op(tmp_path: Path):
    recorder = _recorder(tmp_path / "traces.db", enabled=False)
    _record_turn(recorder)
    assert recorder.summary(days=1)["turns"] == 0
    assert not (tmp_path / "traces.db").exists()


def test_respond_step_persists_the_interrupted_flag(tmp_path: Path):
    from worker import traces

    recorder = _recorder(tmp_path / "traces.db")
    turn = recorder.begin_turn(wake_kind="wake")
    traces.mark_speech_interrupted(turn)
    recorder.respond(turn, ms=5, ok=True)
    recorder.end_turn(turn, addressed=True, wake_kind="wake", outcome="responded", total_ms=5)
    recorder.close()

    with sqlite3.connect(tmp_path / "traces.db") as connection:
        row = connection.execute(
            "SELECT interrupted FROM steps WHERE kind='RESPOND'"
        ).fetchone()
    assert row == (1,)


def test_respond_step_defaults_interrupted_to_false(tmp_path: Path):
    recorder = _recorder(tmp_path / "traces.db")
    turn = recorder.begin_turn(wake_kind="wake")
    recorder.respond(turn, ms=5, ok=True)
    recorder.end_turn(turn, addressed=True, wake_kind="wake", outcome="responded", total_ms=5)
    recorder.close()

    with sqlite3.connect(tmp_path / "traces.db") as connection:
        row = connection.execute(
            "SELECT interrupted FROM steps WHERE kind='RESPOND'"
        ).fetchone()
    assert row == (0,)


def test_pre_existing_database_without_interrupted_column_is_migrated(tmp_path: Path):
    db_path = tmp_path / "traces.db"
    # Recreate the schema as it looked before this unit added the column.
    with sqlite3.connect(db_path) as connection:
        connection.executescript("""
            CREATE TABLE turns (
                turn_id TEXT PRIMARY KEY, started_at REAL NOT NULL, ended_at REAL NOT NULL,
                total_ms INTEGER NOT NULL, addressed INTEGER NOT NULL, wake_kind TEXT,
                outcome TEXT, input_tokens INTEGER NOT NULL, output_tokens INTEGER NOT NULL,
                cache_read_tokens INTEGER NOT NULL, cache_write_tokens INTEGER NOT NULL,
                cost_usd REAL NOT NULL, model TEXT
            );
            CREATE TABLE steps (
                turn_id TEXT NOT NULL REFERENCES turns(turn_id) ON DELETE CASCADE,
                seq INTEGER NOT NULL,
                kind TEXT NOT NULL CHECK(kind IN ('ROUTE','GENERATE','TOOL_CALL','RESPOND')),
                name TEXT, ms INTEGER NOT NULL, ok INTEGER NOT NULL,
                tokens_in INTEGER NOT NULL, tokens_out INTEGER NOT NULL,
                PRIMARY KEY (turn_id, seq)
            );
        """)

    recorder = _recorder(db_path)
    turn = _record_turn(recorder)
    recorder.close()

    with sqlite3.connect(db_path) as connection:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(steps)")}
        interrupted = connection.execute(
            "SELECT interrupted FROM steps WHERE turn_id=?", (turn.turn_id,)
        ).fetchall()
    assert "interrupted" in columns
    assert interrupted and all(value == 0 for (value,) in interrupted)


def test_active_turn_and_mark_speech_interrupted_round_trip(tmp_path: Path):
    from worker import traces

    assert traces.active_turn() is None

    recorder = _recorder(tmp_path / "traces.db")
    turn = recorder.begin_turn(wake_kind="wake")
    token = traces.activate(recorder, turn)
    try:
        active = traces.active_turn()
        assert active is not None
        active_recorder, active_turn = active
        assert active_recorder is recorder
        assert active_turn is turn
        assert active_turn.speech_interrupted is False
        traces.mark_speech_interrupted(active_turn)
        assert turn.speech_interrupted is True
    finally:
        traces.reset(token)

    assert traces.active_turn() is None
    recorder.close()


def test_configured_fast_model_has_a_pricing_row():
    # F8: traces.py prices a turn with self._pricing.get(model, {}) and
    # _price() treats a missing rate as 0.0, so switching fast_model to a lane
    # with no pricing row bills every turn at $0.00 in silence. Pin the two
    # together: a lane switch must fail here instead.
    import yaml

    config = yaml.safe_load(
        (Path(__file__).resolve().parents[1] / "config" / "atlas.yaml").read_text(
            encoding="utf-8",
        ),
    )
    pricing = config["pricing"]
    rates = pricing[config["fast_model"]]

    assert set(rates) == {
        "input_per_mtok", "output_per_mtok",
        "cache_read_per_mtok", "cache_write_per_mtok",
    }
    assert all(isinstance(rate, (int, float)) and rate > 0 for rate in rates.values())


def test_interrupted_is_a_first_class_outcome_not_replaced_with_other(tmp_path: Path):
    """A barge-in is its own outcome, distinct from a turn that failed.

    Without it in the vocabulary the row would be stored as "other", which
    reads the same as a bug in the host and buries the one signal that says
    Daniel simply talked over the answer.
    """
    recorder = _recorder(tmp_path / "traces.db")
    _record_turn(recorder, outcome="interrupted")
    _record_turn(recorder, outcome="empty")
    recorder.close()

    with sqlite3.connect(tmp_path / "traces.db") as connection:
        outcomes = connection.execute(
            "SELECT outcome FROM turns ORDER BY started_at"
        ).fetchall()
    assert sorted(outcomes) == [("empty",), ("interrupted",)]


def test_speech_was_interrupted_reads_the_active_turn(tmp_path: Path):
    from worker import traces

    recorder = _recorder(tmp_path / "traces.db")
    turn = recorder.begin_turn(wake_kind="wake")

    # No active turn at all (the text lane, tests): honest silence stays the
    # default, so the host still speaks its fallback.
    assert traces.speech_was_interrupted() is False

    token = traces.activate(recorder, turn)
    try:
        assert traces.speech_was_interrupted() is False
        traces.mark_speech_interrupted(turn)
        assert traces.speech_was_interrupted() is True
    finally:
        traces.reset(token)
        recorder.close()

    assert traces.speech_was_interrupted() is False
