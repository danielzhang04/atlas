import pytest

from worker.actionauth import PairingAuthorizer
from worker.actionbroker import ActionBroker, ActionError


def test_pairing_token_mints_expiring_session_and_context():
    now = [1.0]
    auth = PairingAuthorizer(clock=lambda: now[0], token="pair-token", ttl_s=10)
    with pytest.raises(PermissionError):
        auth.pair("wrong")
    cookie, context = auth.pair("pair-token")
    assert auth.authorize(cookie) == context
    assert auth.active_context() == (context.session_id, context.device_id)
    with pytest.raises(PermissionError):
        auth.pair("pair-token")
    with pytest.raises(PermissionError):
        _ = auth.pairing_token
    now[0] = 12.0
    with pytest.raises(PermissionError):
        auth.authorize(cookie)


def test_context_provider_refuses_unpaired_proposal_and_binds_paired_one():
    auth = PairingAuthorizer(token="pair-token")
    broker = ActionBroker(context_provider=auth.active_context)
    with pytest.raises(ActionError, match="paired"):
        broker.propose("desktop.open", {"app": "vscode"}, lambda _: None)
    cookie, context = auth.pair("pair-token")
    proposal = broker.propose("desktop.open", {"app": "vscode"}, lambda _: {"ok": True})
    assert proposal.session_id == context.session_id and proposal.device_id == context.device_id
    broker.confirm(proposal.proposal_id, channel="ui", parameters_hash=proposal.parameters_hash,
                   session_id=context.session_id, device_id=context.device_id)
    assert broker.execute(proposal.proposal_id).status == "succeeded"
