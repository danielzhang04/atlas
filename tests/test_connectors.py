import pytest

from worker.connectors import BrowserConnector, ConnectorError, GoogleBrokerConnector


def _transport(calls, *, status=200, body=None):
    def call(**kwargs):
        calls.append(kwargs)
        return {"status": status, "body": body if body is not None else {"access_token": "never", "ok": True}}
    return call


def test_browser_validates_origins_urls_credentials_and_bounds_without_network():
    calls = []
    browser = BrowserConnector("http://127.0.0.1:4567", _transport(calls), allowed_origins={"https://mail.google.com"})
    assert browser.type("tab-1", "#email", "hello", origin="https://mail.google.com", document_id="doc-1")["ok"] is True
    assert calls[0]["json"] == {"action": "type", "target": "#email", "value": "hello", "expected_origin": "https://mail.google.com", "expected_document_id": "doc-1"}
    assert browser.navigate("tab-1", "https://example.com/x", origin="https://mail.google.com", document_id="doc-1")["ok"] is True
    for origin in ("file:///tmp/x", "https://u:p@example.com", "https://example.com/path"):
        with pytest.raises(ValueError): browser.click("tab-1", "#x", origin=origin, document_id="doc-1")
    with pytest.raises(ValueError): browser.navigate("tab-1", "javascript:alert(1)", origin="https://mail.google.com", document_id="doc-1")
    with pytest.raises(ValueError): browser.upload("tab-1", "#x", "../../secret", origin="https://mail.google.com", document_id="doc-1")
    with pytest.raises(ValueError): browser.type("tab-1", "x" * 1001, "v", origin="https://mail.google.com", document_id="doc-1")
    with pytest.raises(ValueError, match="loopback"):
        BrowserConnector("http://bridge.example", _transport([]))
    empty = BrowserConnector("http://127.0.0.1:4567", _transport([]))
    with pytest.raises(ValueError, match="allowlist"):
        empty.click("tab-1", "#x", origin="https://mail.google.com", document_id="doc-1")


class BrokerInvoker:
    def __init__(self):
        self.calls = []
        self.binding = "account-generation-1"
        self.count = 7

    def __call__(self, operation, parameters, *, binding):
        self.calls.append((operation, parameters, binding))
        if operation == "connection.bind":
            return {"binding": self.binding, "access_token": "never-return-this"}
        if binding != self.binding:
            raise RuntimeError("credential-bearing broker diagnostic")
        if operation == "gmail.count":
            return self.count
        return {"ok": True, "authorization": "never-return-this"}


def test_google_broker_contracts_use_closed_operations_and_opaque_binding():
    invoke = BrokerInvoker()
    google = GoogleBrokerConnector(invoke)
    bound = google.bind_connection()
    assert bound(google.list_drive_files, "name contains 'atlas'")["ok"] is True
    bound(google.read_drive_file, "file-1")
    bound(google.read_doc, "doc-1")
    bound(google.update_doc, "doc-1", [{"insertText": {"text": "Atlas"}}])
    bound(google.create_gmail_draft, "a@example.com", "Hello", "Body")
    bound(google.read_gmail_draft, "draft-1")
    bound(google.send_gmail_draft, "draft-1")
    assert bound(google.count_gmail, "newer_than:1d") == 7
    assert [call[0] for call in invoke.calls[1:]] == [
        "drive.list", "drive.read", "docs.read", "docs.update", "gmail.draft.create",
        "gmail.draft.read", "gmail.draft.send", "gmail.count",
    ]
    assert all(call[2] == "account-generation-1" for call in invoke.calls[1:])
    assert "access_token" not in repr(bound)
    assert not hasattr(google, "share_drive_file")
    assert not hasattr(google, "delete_drive_file")


def test_google_calendar_contracts_validate_before_local_broker_call():
    invoke = BrokerInvoker()
    google = GoogleBrokerConnector(invoke)
    bound = google.bind_connection()
    event = {"summary": "Atlas review", "start": {"dateTime": "2026-08-20T10:00:00Z"}}
    bound(google.list_calendar_events, time_min="2026-08-20T00:00:00Z",
          time_max="2026-08-21T00:00:00Z")
    bound(google.read_calendar_event, "event-1")
    bound(google.create_calendar_event, event)
    bound(google.update_calendar_event, "event-1", event, expected_etag='"calendar-v1"')
    bound(google.delete_calendar_event, "event-1", expected_etag='"calendar-v1"')
    assert [call[0] for call in invoke.calls[-5:]] == [
        "calendar.list", "calendar.read", "calendar.create", "calendar.update", "calendar.delete"]
    with pytest.raises(ValueError, match="version"):
        google.update_calendar_event("event-1", event)
    with pytest.raises(ValueError, match="version"):
        google.delete_calendar_event("event-1", expected_etag="bad\nversion")
    with pytest.raises(ValueError, match="Gmail"):
        google.create_gmail_draft("a@example.com\nBcc:x@y", "subject", "body")


def test_google_broker_failure_is_sanitized_and_no_bearer_api_exists():
    invoke = BrokerInvoker()
    google = GoogleBrokerConnector(invoke)
    bound = google.bind_connection()
    invoke.binding = "account-generation-2"
    with pytest.raises(ConnectorError, match="credential broker request failed") as exc:
        bound(google.create_calendar_event, {"summary": "Must not be created"}, "primary")
    assert "credential-bearing" not in str(exc.value)
    assert not hasattr(google, "_token_provider")
    assert not hasattr(google, "_oauth_snapshot")
