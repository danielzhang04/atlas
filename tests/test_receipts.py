import json
from dataclasses import dataclass
from datetime import datetime, timezone

import pytest

from worker.receipts import ReceiptJournal


HASH = "a" * 64


@dataclass
class Snapshot:
    proposal_id: str = "proposal-1"
    capability_id: str = "local_files.edit"
    parameters_hash: str = HASH
    status: str = "succeeded"
    session_id: str | None = "session-1"
    device_id: str | None = "device-1"
    confirmation_channel: str | None = "ui"
    receipt: object = None
    failure: str | None = None


def _journal(tmp_path):
    return ReceiptJournal(tmp_path / "history" / "receipts.jsonl", clock=lambda: datetime(2026, 8, 20, tzinfo=timezone.utc))


def test_terminal_append_is_fixed_schema_and_redacts_executor_data(tmp_path):
    journal = _journal(tmp_path)
    snapshot = Snapshot(receipt={"error_code": "ignored", "token": "SECRET", "body": {"cookie": "x"}}, failure="SECRET traceback")
    record = journal.append_terminal(snapshot)
    assert set(record) == {"version", "timestamp", "proposal_id", "capability_id", "parameters_hash", "status", "session_id", "device_id", "confirmation_channel", "error_code"}
    assert "SECRET" not in (tmp_path / "history" / "receipts.jsonl").read_text(encoding="utf-8")
    assert record["error_code"] is None


@pytest.mark.parametrize("status", ["succeeded", "failed", "cancelled", "expired"])
def test_all_terminal_outcomes_are_appendable(tmp_path, status):
    journal = _journal(tmp_path)
    record = journal.append_terminal(Snapshot(status=status, receipt={"error_code": "runtimeerror"}))
    assert record["status"] == status
    assert record["error_code"] == ("runtimeerror" if status == "failed" else None)


def test_rejections_latest_order_and_corrupt_lines(tmp_path):
    journal = _journal(tmp_path)
    journal.append_terminal(Snapshot(proposal_id="proposal-1"))
    journal.append_rejected(proposal_id="proposal-2", capability_id="browser.submit", parameters_hash=HASH,
                            reason_code="untrusted_channel", session_id="session-1")
    path = tmp_path / "history" / "receipts.jsonl"
    with path.open("ab") as handle: handle.write(b"not-json\n")
    latest = journal.read_latest(2)
    assert [item["proposal_id"] for item in latest] == ["proposal-2", "proposal-1"]
    assert latest[0]["status"] == "rejected" and latest[0]["error_code"] == "untrusted_channel"


def test_rejects_nonterminal_and_invalid_history_limits(tmp_path):
    journal = _journal(tmp_path)
    with pytest.raises(ValueError): journal.append_terminal(Snapshot(status="confirmed"))
    with pytest.raises(ValueError): journal.append_rejected(proposal_id="bad value", capability_id="x", parameters_hash=HASH, reason_code="bad")
    with pytest.raises(ValueError): journal.read_latest(0)
