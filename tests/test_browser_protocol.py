import json
from pathlib import Path

import pytest

from worker.browser_protocol import (
    BrowserOperation, BrowserProtocolError, ReplayGuard, canonical_origin,
    new_request, validate_request, validate_response,
)
from worker.browser_transport import (
    AuthenticatedBrowserTransport, BrowserTransportError, FakeBrowserEndpoint,
)


def test_manifest_is_mv3_dedicated_profile_and_has_no_sensitive_permissions():
    manifest = json.loads((Path(__file__).parents[1] / "browser_bridge" / "extension" / "manifest.json").read_text())
    assert manifest["manifest_version"] == 3
    assert manifest["incognito"] == "not_allowed"
    assert manifest["host_permissions"] == []
    assert "externally_connectable" not in manifest
    forbidden = {"cookies", "debugger", "webRequest", "history", "<all_urls>"}
    assert not forbidden.intersection(manifest.get("permissions", []))


def test_canonical_origin_requires_exact_web_origin():
    assert canonical_origin("HTTPS://Mail.Google.com:443/") == "https://mail.google.com"
    assert canonical_origin("http://[::1]:80") == "http://[::1]"
    for value in ("https://mail.google.com/path", "https://user:pass@mail.google.com",
                  "https://mail.google.com/?x=1", "file:///tmp/a", "javascript:alert(1)"):
        with pytest.raises(BrowserProtocolError):
            canonical_origin(value)


def test_typed_navigation_is_bound_to_origin_and_bounded():
    request = new_request(sequence=1, operation=BrowserOperation.NAVIGATE,
                          tab_id="tab-1", origin="https://mail.google.com",
                          document_id="doc-1", value="https://mail.google.com/inbox")
    assert validate_request(request).operation is BrowserOperation.NAVIGATE
    with pytest.raises(BrowserProtocolError):
        new_request(sequence=1, operation="navigate", tab_id="tab-1",
                    origin="https://mail.google.com", document_id="doc-1",
                    value="https://evil.example/")
    with pytest.raises(BrowserProtocolError):
        new_request(sequence=1, operation="type", tab_id="tab-1",
                    origin="https://mail.google.com", document_id="doc-1",
                    target="#email", value="x" * 20001)


def test_replay_guard_rejects_request_id_and_sequence_reuse():
    first = new_request(sequence=1, operation="inspect", tab_id="tab-1",
                        origin="https://mail.google.com", document_id="doc-1")
    guard = ReplayGuard()
    guard.accept(first)
    with pytest.raises(BrowserProtocolError, match="replayed"):
        guard.accept(first)
    second = new_request(sequence=1, operation="inspect", tab_id="tab-1",
                         origin="https://mail.google.com", document_id="doc-1")
    with pytest.raises(BrowserProtocolError, match="replayed"):
        guard.accept(second)


def test_authenticated_transport_fails_closed_and_fake_endpoint_is_bounded():
    endpoint = FakeBrowserEndpoint(lambda request: {"ok": True, "result": {
        "visible_text": "untrusted", "password": "must not cross"}},
        expected_auth="proof")
    transport = AuthenticatedBrowserTransport(endpoint, lambda: "proof", timeout_s=1)
    response = transport.request("inspect", tab_id="tab-1", origin="https://mail.google.com",
                                 document_id="doc-1")
    assert response.ok and response.result == {"visible_text": "untrusted"}
    assert "password" not in json.dumps(response.as_dict())
    assert endpoint.calls[0]["operation"] == "inspect"

    unauthenticated = AuthenticatedBrowserTransport(endpoint, lambda: None, timeout_s=1)
    with pytest.raises(BrowserTransportError, match="authentication unavailable"):
        unauthenticated.request("inspect", tab_id="tab-1", origin="https://mail.google.com",
                                document_id="doc-1")


def test_transport_rejects_mismatched_response():
    endpoint = lambda _payload, **_kwargs: {"version": 1, "request_id": "other",
                                             "sequence": 1, "ok": True, "result": {}}
    transport = AuthenticatedBrowserTransport(endpoint, lambda: "proof", timeout_s=1)
    with pytest.raises(BrowserTransportError, match="response"):
        transport.request("inspect", tab_id="tab-1", origin="https://mail.google.com",
                          document_id="doc-1")
