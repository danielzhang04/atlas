"""Trusted local runtime assembly for Atlas capabilities.

Local-file capabilities stay fail-closed until a strong Windows root-confinement backend exists.
Browser, Google, and desktop transports are assembled when paired/configured.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from worker import capabilities
from worker.actionbroker import ActionBroker, ActionError, ActionSnapshot, parameter_hash
from worker.connectors import BrowserConnector, GoogleBrokerConnector, urllib_json_transport
from worker.desktopapps import DesktopApps, TargetAlias, native_launcher
from worker.localfiles import LOCAL_FILES_UNAVAILABLE
from worker.receipts import ReceiptJournal


def _expand_path(value: str) -> Path:
    return Path(os.path.expanduser(os.path.expandvars(value))).resolve()


def _label(capability_id: str) -> str:
    return capability_id.replace("_", " ").replace(".", " / ").title()


def _snapshot_dict(snapshot: ActionSnapshot) -> dict[str, Any]:
    return {"id": snapshot.proposal_id, "status": snapshot.status,
            "receipt": _receipt_projection(snapshot)}


def _receipt_projection(snapshot: ActionSnapshot) -> dict[str, str] | None:
    """Expose audit metadata, never an executor's potentially sensitive response body."""
    if snapshot.receipt is None:
        return None
    receipt = {
        "outcome": snapshot.status,
        "capability_id": snapshot.capability_id,
        "proposal_hash": snapshot.parameters_hash,
    }
    if snapshot.confirmation_channel:
        receipt["confirmation_channel"] = snapshot.confirmation_channel
    if isinstance(snapshot.receipt, dict) and isinstance(snapshot.receipt.get("error_code"), str):
        receipt["error_code"] = snapshot.receipt["error_code"][:100]
    return receipt


PREVIEW_LIMIT = 4000


def _parameter_preview(snapshot: ActionSnapshot) -> tuple[str, bool]:
    """Return an exact preview or fail closed when exact review will not fit."""
    preview = json.dumps(snapshot.parameters, ensure_ascii=False, indent=2, sort_keys=True)
    if len(preview) <= PREVIEW_LIMIT:
        return preview, True
    return (f"Exact parameter preview exceeds {PREVIEW_LIMIT} characters. "
            "This proposal cannot be confirmed; cancel it and prepare a smaller action.", False)


class LoopbackActions:
    """Minimal UI projection over ActionBroker; no arbitrary command surface exists."""

    def __init__(self, broker: ActionBroker) -> None:
        self._broker = broker

    def list_actions(self) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        for snapshot in self._broker.list()[:50]:
            preview, confirmable = _parameter_preview(snapshot)
            items.append({
                "id": snapshot.proposal_id,
                "label": _label(snapshot.capability_id),
                "preview": preview,
                "proposal_hash": snapshot.parameters_hash,
                "status": snapshot.status,
                "created_at": str(snapshot.created_at),
                "risk": "T2" if snapshot.capability_id.startswith("local_files.") else "T3",
                "confirmable": confirmable,
                "receipt": (json.dumps(_receipt_projection(snapshot), ensure_ascii=False,
                                       sort_keys=True)[:2000]
                            if snapshot.receipt is not None else ""),
            })
        return items

    def run_action(self, proposal_id: str, proposal_hash: str, *, session_id: str | None = None,
                   device_id: str | None = None) -> dict[str, Any]:
        snapshot = self._broker.get(proposal_id)
        _preview, confirmable = _parameter_preview(snapshot)
        if not confirmable:
            raise ActionError("proposal is not confirmable; cancel it and prepare a smaller action")
        self._broker.confirm(proposal_id, channel="ui", parameters_hash=proposal_hash,
                             session_id=session_id, device_id=device_id)
        return _snapshot_dict(self._broker.execute(proposal_id, parameters_hash=proposal_hash))

    def cancel_action(self, proposal_id: str, proposal_hash: str, *, session_id: str | None = None,
                      device_id: str | None = None) -> dict[str, Any]:
        return _snapshot_dict(self._broker.cancel(
            proposal_id, channel="ui", parameters_hash=proposal_hash,
            session_id=session_id, device_id=device_id))

    def record_rejected(self, proposal_id: str, reason_code: str, *, session_id: str | None = None,
                        device_id: str | None = None) -> None:
        self._broker.journal_rejection(proposal_id, reason_code=reason_code, channel="ui",
                                       session_id=session_id, device_id=device_id)


class DesktopActions:
    """Prepare allowlisted desktop open/focus actions for the trusted loopback UI."""

    def __init__(self, apps: DesktopApps, broker: ActionBroker) -> None:
        self._apps = apps
        self._broker = broker

    def prepare_open(self, app_id: str, target_alias: str | None = None,
                     *, idempotency_key: str | None = None,
                     session_id: str | None = None,
                     device_id: str | None = None) -> ActionSnapshot:
        parameters = {"kind": "open", "app_id": app_id, "target": target_alias or ""}
        # Validate before proposing; execution validates again inside DesktopApps.open.
        self._apps.validate_open(app_id, target_alias)
        return self._broker.propose(
            "desktop.open", parameters,
            lambda p: self._apps.open(p["app_id"], p["target"] or None),
            idempotency_key=idempotency_key, session_id=session_id, device_id=device_id)

    def prepare_focus(self, app_id: str, *, idempotency_key: str | None = None,
                      session_id: str | None = None,
                      device_id: str | None = None) -> ActionSnapshot:
        parameters = {"kind": "focus", "app_id": app_id, "target": ""}
        self._apps.validate_focus(app_id)
        return self._broker.propose(
            "desktop.focus", parameters, lambda p: self._apps.focus(p["app_id"]),
            idempotency_key=idempotency_key, session_id=session_id, device_id=device_id)

    def target_kinds(self) -> dict[str, str]:
        return self._apps.target_kinds()


class BrowserActions:
    """Read a paired tab or prepare one exact, tab/origin-bound browser interaction."""

    def __init__(self, connector: BrowserConnector, broker: ActionBroker) -> None:
        self._connector = connector
        self._broker = broker

    def inspect(self, tab_id: str) -> Any:
        return self._connector.inspect_tab(tab_id)

    def prepare(self, tab_id: str, action: str, *, target: str = "", value: str = "",
                origin: str, idempotency_key: str | None = None) -> ActionSnapshot:
        validated = self._connector.validate_action(
            tab_id, action, target=target, value=value, origin=origin)
        state = self._connector.attest_tab_origin(tab_id, validated["origin"])
        document_id = state["evidence"]["document_id"]
        parameters = {"kind": action, "tab_id": tab_id, "document_id": document_id,
                      **validated}
        return self._broker.propose(
            f"browser.{action}", parameters,
            self._execute,
            idempotency_key=idempotency_key)

    def _execute(self, parameters: dict[str, Any]) -> Any:
        return self._connector.action(
            parameters["tab_id"], parameters["action"], target=parameters["target"],
            value=parameters["value"], origin=parameters["origin"],
            document_id=parameters["document_id"])


class GoogleActions:
    """Scoped Google reads plus broker-confirmed Gmail and Calendar mutations."""

    def __init__(self, connector: GoogleBrokerConnector, broker: ActionBroker) -> None:
        self._connector = connector
        self._broker = broker

    def list_drive(self, query: str = "") -> Any:
        return self._connector.bind_connection()(self._connector.list_drive_files, query)

    def read_drive(self, file_id: str) -> Any:
        return self._connector.bind_connection()(self._connector.read_drive_file, file_id)

    def read_doc(self, document_id: str) -> Any:
        return self._connector.bind_connection()(self._connector.read_doc, document_id)

    def count_gmail(self, query: str = "") -> int:
        return self._connector.bind_connection()(self._connector.count_gmail, query)

    def list_calendar(self, calendar_id: str = "primary", *, max_results: int = 100,
                      time_min: str | None = None, time_max: str | None = None) -> Any:
        return self._connector.bind_connection()(
            self._connector.list_calendar_events, calendar_id, max_results=max_results,
            time_min=time_min, time_max=time_max,
        )

    @staticmethod
    def _snapshot(resource: Any, label: str) -> tuple[dict[str, Any], str, str | None]:
        """Bind review to the exact resource and, where available, its remote ETag."""
        if not isinstance(resource, dict):
            raise ValueError(f"{label} response was not reviewable")
        etag = resource.get("etag")
        if etag is not None and (not isinstance(etag, str) or not etag or len(etag) > 1000
                                 or any(ord(char) < 32 for char in etag)):
            raise ValueError(f"{label} returned an invalid version")
        return resource, parameter_hash(resource), etag

    @staticmethod
    def _require_unchanged(parameters: dict[str, Any], current: Any, label: str) -> None:
        if not isinstance(current, dict) or parameter_hash(current) != parameters["snapshot_hash"]:
            raise ValueError(f"{label} changed after review; prepare it again")

    def prepare_gmail_draft(self, to: str, subject: str, body: str,
                            *, idempotency_key: str | None = None) -> ActionSnapshot:
        # Validate headers/body before showing the proposal.
        bound = self._connector.bind_connection()
        self._connector.gmail_rfc822(to, subject, body)
        parameters = {"kind": "gmail_draft", "to": to, "subject": subject, "body": body}
        return self._broker.propose(
            "google.gmail.draft", parameters,
            lambda p: bound(
                self._connector.create_gmail_draft, p["to"], p["subject"], p["body"]),
            idempotency_key=idempotency_key)

    def prepare_gmail_send(self, draft_id: str,
                           *, idempotency_key: str | None = None) -> ActionSnapshot:
        bound = self._connector.bind_connection()
        draft = bound(self._connector.read_gmail_draft, draft_id)
        if not isinstance(draft, dict):
            raise ValueError("Gmail draft response was not reviewable")
        parameters = {"kind": "gmail_send", "draft_id": draft_id,
                      "draft_snapshot": draft, "draft_hash": parameter_hash(draft)}
        return self._broker.propose(
            "google.gmail.send", parameters,
            lambda p: self._send_gmail_if_unchanged(p, bound),
            idempotency_key=idempotency_key)

    def _send_gmail_if_unchanged(self, parameters: dict[str, Any], bound) -> Any:
        current = bound(self._connector.read_gmail_draft, parameters["draft_id"])
        if not isinstance(current, dict) or parameter_hash(current) != parameters["draft_hash"]:
            raise ValueError("Gmail draft changed after review; prepare it again")
        return bound(self._connector.send_gmail_draft, parameters["draft_id"])

    def prepare_calendar_create(self, event: dict[str, Any], calendar_id: str = "primary",
                                *, idempotency_key: str | None = None) -> ActionSnapshot:
        """Prepare a calendar creation; no remote write occurs until trusted UI confirmation."""
        bound = self._connector.bind_connection()
        self._connector.validate_calendar_event(event, calendar_id)
        parameters = {"kind": "calendar_create", "calendar_id": calendar_id, "event": event}
        return self._broker.propose(
            "google.calendar.create", parameters,
            lambda p: bound(
                self._connector.create_calendar_event, p["event"], p["calendar_id"]),
            idempotency_key=idempotency_key)

    def prepare_calendar_update(self, event_id: str, event: dict[str, Any],
                                calendar_id: str = "primary", *,
                                idempotency_key: str | None = None) -> ActionSnapshot:
        bound = self._connector.bind_connection()
        self._connector.validate_calendar_event(event, calendar_id)
        snapshot, snapshot_hash, etag = self._snapshot(
            bound(self._connector.read_calendar_event, event_id, calendar_id), "Calendar event")
        if etag is None:
            raise ValueError("Calendar event did not provide a version; cannot prepare update")
        parameters = {"kind": "calendar_update", "calendar_id": calendar_id,
                      "event_id": event_id, "event": event, "event_snapshot": snapshot,
                      "snapshot_hash": snapshot_hash, "expected_etag": etag}
        return self._broker.propose(
            "google.calendar.update", parameters,
            lambda p: self._update_calendar_if_unchanged(p, bound),
            idempotency_key=idempotency_key)

    def _update_calendar_if_unchanged(self, parameters: dict[str, Any], bound) -> Any:
        current = bound(self._connector.read_calendar_event, parameters["event_id"],
                        parameters["calendar_id"])
        self._require_unchanged(parameters, current, "Calendar event")
        return bound(
            self._connector.update_calendar_event,
            parameters["event_id"], parameters["event"], parameters["calendar_id"],
            expected_etag=parameters["expected_etag"])

    def prepare_calendar_delete(self, event_id: str, calendar_id: str = "primary", *,
                                idempotency_key: str | None = None) -> ActionSnapshot:
        bound = self._connector.bind_connection()
        snapshot, snapshot_hash, etag = self._snapshot(
            bound(self._connector.read_calendar_event, event_id, calendar_id), "Calendar event")
        if etag is None:
            raise ValueError("Calendar event did not provide a version; cannot prepare delete")
        parameters = {"kind": "calendar_delete", "calendar_id": calendar_id,
                      "event_id": event_id, "event_snapshot": snapshot,
                      "snapshot_hash": snapshot_hash, "expected_etag": etag}
        return self._broker.propose(
            "google.calendar.delete", parameters,
            lambda p: self._delete_calendar_if_unchanged(p, bound),
            idempotency_key=idempotency_key)

    def _delete_calendar_if_unchanged(self, parameters: dict[str, Any], bound) -> Any:
        current = bound(self._connector.read_calendar_event, parameters["event_id"],
                        parameters["calendar_id"])
        self._require_unchanged(parameters, current, "Calendar event")
        return bound(
            self._connector.delete_calendar_event,
            parameters["event_id"], parameters["calendar_id"],
            expected_etag=parameters["expected_etag"])


@dataclass
class RuntimeServices:
    catalog: dict[str, capabilities.Capability]
    broker: ActionBroker
    local_files: None
    desktop: DesktopActions | None
    browser: BrowserActions | None
    google: GoogleActions | None
    google_connected: bool
    actions: LoopbackActions
    receipts: ReceiptJournal | None

    def catalog_projection(self) -> list[dict[str, str]]:
        result: list[dict[str, str]] = []
        for item in self.catalog.values():
            if item.id in {"google.drive.share", "google.drive.delete"}:
                status = "configuration-needed"
                detail = ("Unavailable: Drive v3 does not provide the atomic body-version "
                          "precondition required by this adapter")
            elif item.domain == "local_files":
                status = "configuration-needed"
                detail = LOCAL_FILES_UNAVAILABLE
            elif item.domain in {"desktop", "spotify"} and item.availability == "local_agent":
                target_kinds = self.desktop.target_kinds() if self.desktop is not None else {}
                connected = self.desktop is not None and (
                    item.domain == "desktop" or "spotify_uri" in target_kinds.values())
                status = "connected" if connected else "configuration-needed"
                if connected and item.domain == "desktop":
                    aliases = ", ".join(f"{name} ({kind})" for name, kind in target_kinds.items())
                    detail = f"Approved targets: {aliases}" if aliases else "Named desktop profiles"
                elif connected:
                    detail = "Approved Spotify target"
                else:
                    detail = "Add desktop_target_aliases"
            elif item.availability == "paired_browser":
                connected = self.browser is not None
                status = "connected" if connected else "configuration-needed"
                detail = "Paired browser bridge" if connected else "Configure and pair browser_bridge_url"
            elif item.availability == "oauth":
                is_google = item.required_connection == "google_oauth"
                status = "connected" if is_google and self.google_connected else "configuration-needed"
                detail = ("Scoped Google OAuth transport" if status == "connected" else
                          f"Implementation ready; {item.required_connection or 'OAuth'} is not configured")
            else:
                status, detail = "available", "Local adapter is available"
            result.append({"id": item.id, "label": _label(item.id), "status": status,
                           "detail": detail, "kind": item.domain})
        return result


def build_runtime(atlas_root: str | Path, cfg: dict[str, Any], *,
                  action_context_provider=None,
                  google_connector: GoogleBrokerConnector | None = None) -> RuntimeServices:
    """Build reviewed adapters from trusted config; local files remain unavailable."""
    root = Path(atlas_root)
    catalog = capabilities.load_catalog(root / "config" / "capabilities.yaml")
    journal_path = cfg.get("receipt_journal_path")
    receipt_journal = (ReceiptJournal(_expand_path(journal_path))
                       if isinstance(journal_path, str) and journal_path else None)
    broker = ActionBroker(context_provider=action_context_provider,
                          receipt_journal=receipt_journal)
    # Do not inspect or retain configured local roots. Path-based validation cannot bind a
    # reviewed file identity to a later Windows read/replace without a raceable reparse window.
    local_files = None
    aliases: dict[str, TargetAlias] = {}
    for alias, entry in (cfg.get("desktop_target_aliases") or {}).items():
        if isinstance(entry, dict) and isinstance(entry.get("kind"), str) and isinstance(entry.get("value"), str):
            value = entry["value"]
            if entry["kind"] == "path":
                value = str(_expand_path(value))
            aliases[str(alias)] = TargetAlias(entry["kind"], value)
    desktop = DesktopActions(DesktopApps(aliases, launcher=native_launcher), broker) if aliases else None
    bridge_url = cfg.get("browser_bridge_url")
    browser = None
    origins = {str(value) for value in (cfg.get("browser_allowed_origins") or [])}
    if isinstance(bridge_url, str) and bridge_url and origins:
        browser = BrowserActions(BrowserConnector(
            bridge_url, urllib_json_transport, allowed_origins=origins), broker)
    # OAuth material belongs to a separate local credential broker.  Production config cannot
    # name an environment variable or inject a bearer token into this process.  A reviewed broker
    # client may be supplied explicitly by the host after the end-review activation gate.
    if google_connector is not None and not isinstance(google_connector, GoogleBrokerConnector):
        raise TypeError("google_connector must be a credential-free GoogleBrokerConnector")
    google = GoogleActions(google_connector, broker) if google_connector is not None else None
    google_connected = google is not None
    return RuntimeServices(catalog, broker, local_files, desktop, browser, google,
                           google_connected, LoopbackActions(broker), receipt_journal)
