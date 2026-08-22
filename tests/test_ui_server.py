"""Static command-center and standalone server shape guards."""
from __future__ import annotations

from worker import ui_server


def test_standalone_ui_uses_the_loopback_server_and_fragment_pairing():
    source = (ui_server.ATLAS / "worker" / "ui_server.py").read_text(encoding="utf-8")
    client = (ui_server.ATLAS / "ui" / "app.js").read_text(encoding="utf-8")
    assert ui_server.stateserver.HOST == "127.0.0.1"
    assert "stateserver.pairing_url" in source
    assert "window.location.hash" in client
    assert "history.replaceState" in client
    assert "localStorage" not in client and "sessionStorage" not in client


def test_command_center_contains_only_the_revamp_panes():
    page = (ui_server.ATLAS / "ui" / "index.html").read_text(encoding="utf-8")
    for label in ("Atlas Engine", "Transcript", "Workers", "History", "Settings"):
        assert label in page
    for removed in ("Sources", "Guide me", "Receipts", "Capabilities"):
        assert removed not in page


def test_workers_poll_incremental_events_and_history_fetches_paired_results():
    client = (ui_server.ATLAS / "ui" / "app.js").read_text(encoding="utf-8")
    assert 'fetch("/jobs", {cache: "no-store"})' in client
    assert "/events?after=${lastSequence}" in client
    assert "if (!actionToken) return;" in client
    assert "pair to view output" in client
    assert "setInterval(refreshJobs, 1000)" in client
    assert "/jobs/${encodeURIComponent(jobId)}/result" in client
    assert "stateserver" not in client
    assert '"x-atlas-action-token": actionToken' in client


def test_settings_show_config_paths_mcp_and_voice_status_without_removed_doctrine():
    client = (ui_server.ATLAS / "ui" / "app.js").read_text(encoding="utf-8")
    combined = "\n".join(
        (ui_server.ATLAS / "ui" / name).read_text(encoding="utf-8")
        for name in ("index.html", "app.js", "styles.css")
    ).casefold()
    assert 'fetch("/mcp", {cache: "no-store"})' in client
    for path in ("config/atlas.yaml", "config/apps.yaml", "config/mcp.yaml", "config/intents.yaml", "config/persona.md"):
        assert path in client
    for removed in ("capabilities", "guided setup", "receipt", "proposal"):
        assert removed not in combined


def test_browser_launch_failure_is_best_effort(monkeypatch):
    monkeypatch.setattr(ui_server.webbrowser, "open", lambda *_args, **_kwargs: False)
    assert ui_server._open_pairing_window("http://127.0.0.1:4360/#pair=redacted") is False
