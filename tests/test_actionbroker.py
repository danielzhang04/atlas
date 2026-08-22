import pytest

from worker.actionbroker import (ActionBroker, ActionError, ActionExpired, ActionNotConfirmed, ReplayDetected,
                                 SUCCEEDED, parameter_hash)
from worker.receipts import ReceiptJournal


def test_trusted_confirmation_binds_parameters_and_single_use():
    now = [10.0]
    broker = ActionBroker(clock=lambda: now[0], id_factory=lambda: "proposal-1")
    proposal = broker.propose("local_files.edit", {"path": "a.md", "text": "new"}, lambda p: {"ok": p["path"]})
    with pytest.raises(ActionNotConfirmed):
        broker.execute(proposal.proposal_id)
    with pytest.raises(Exception):
        broker.confirm(proposal.proposal_id, channel="model", parameters_hash=proposal.parameters_hash)
    with pytest.raises(Exception):
        broker.confirm(proposal.proposal_id, channel="ui", parameters_hash="wrong")
    broker.confirm(proposal.proposal_id, channel="ui", parameters_hash=proposal.parameters_hash)
    done = broker.execute(proposal.proposal_id, parameters_hash=proposal.parameters_hash)
    assert done.status == SUCCEEDED and done.receipt == {"ok": "a.md"}
    with pytest.raises(ReplayDetected):
        broker.execute(proposal.proposal_id)


def test_broker_persists_terminal_and_rejected_receipts(tmp_path):
    journal = ReceiptJournal(tmp_path / "receipts.jsonl")
    broker = ActionBroker(id_factory=lambda: "journal-1", receipt_journal=journal)
    proposal = broker.propose("local_files.create", {"path": "a.md"},
                              lambda _: {"secret": "not journaled"})
    broker.journal_rejection(proposal.proposal_id, reason_code="proposal_hash_mismatch",
                             channel="ui")
    broker.confirm(proposal.proposal_id, channel="ui", parameters_hash=proposal.parameters_hash)
    broker.execute(proposal.proposal_id)
    receipts = journal.read_latest()
    assert [item["status"] for item in receipts] == ["succeeded", "rejected"]
    assert "secret" not in (tmp_path / "receipts.jsonl").read_text(encoding="utf-8")


def test_expiry_idempotency_and_executor_failure_are_recorded():
    now = [0.0]
    ids = iter(["p", "q"])
    broker = ActionBroker(clock=lambda: now[0], id_factory=lambda: next(ids))
    first = broker.propose("x", {"a": 1}, lambda _: None, ttl_s=2, idempotency_key="same")
    assert broker.propose("x", {"a": 1}, lambda _: None, idempotency_key="same").proposal_id == first.proposal_id
    with pytest.raises(Exception):
        broker.propose("x", {"a": 2}, lambda _: None, idempotency_key="same")
    now[0] = 2
    with pytest.raises(ActionExpired):
        broker.confirm(first.proposal_id, channel="ui", parameters_hash=parameter_hash({"a": 1}))
    failed = broker.propose("y", {}, lambda _: (_ for _ in ()).throw(RuntimeError("nope")))
    broker.confirm(failed.proposal_id, channel="service", parameters_hash=failed.parameters_hash)
    assert broker.execute(failed.proposal_id).status == "failed"


def test_cancel_binds_hash_context_and_prevents_execution():
    broker = ActionBroker(id_factory=lambda: "p-cancel")
    proposal = broker.propose("files.write", {"path": "x"}, lambda _: "no",
                              session_id="s1", device_id="d1")
    with pytest.raises(ActionError):
        broker.cancel(proposal.proposal_id, channel="ui", parameters_hash=proposal.parameters_hash,
                      session_id="wrong", device_id="d1")
    cancelled = broker.cancel(proposal.proposal_id, channel="ui",
                              parameters_hash=proposal.parameters_hash,
                              session_id="s1", device_id="d1")
    assert cancelled.status == "cancelled"
    with pytest.raises(ReplayDetected):
        broker.execute(proposal.proposal_id)


def test_list_expires_and_can_filter_terminal():
    now = [10.0]
    broker = ActionBroker(clock=lambda: now[0], id_factory=lambda: "p-expire")
    broker.propose("files.write", {"path": "x"}, lambda _: "no", ttl_s=1)
    now[0] = 12.0
    assert broker.list()[0].status == "expired"
    assert broker.list(include_terminal=False) == []
