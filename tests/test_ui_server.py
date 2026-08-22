from worker import ui_server


def test_standalone_ui_entrypoint_uses_atlas_root_and_loopback_server():
    assert (ui_server.ATLAS / "config" / "atlas.yaml").is_file()
    assert ui_server.stateserver.HOST == "127.0.0.1"


def test_standalone_pairing_bootstrap_is_fragment_only():
    source = (ui_server.ATLAS / "worker" / "ui_server.py").read_text(encoding="utf-8")
    client = (ui_server.ATLAS / "ui" / "app.js").read_text(encoding="utf-8")
    assert "#pair=" in source and "pairing token" not in source.casefold()
    assert "window.location.hash" in client and "history.replaceState" in client


def test_browser_launch_failure_is_best_effort(monkeypatch):
    monkeypatch.setattr(ui_server.webbrowser, "open", lambda *_args, **_kwargs: False)
    assert ui_server._open_pairing_window("http://127.0.0.1:4360/#pair=redacted") is False


def test_headless_local_host_flag_is_available(monkeypatch):
    monkeypatch.setattr("sys.argv", ["ui_server", "--no-browser", "--port", "0", "--mirror-port", "4360"])
    args = ui_server._arguments()
    assert args.no_browser is True and args.port == 0 and args.mirror_port == 4360


def test_home_reads_durable_work_and_keeps_audio_and_result_boundaries_explicit():
    client = (ui_server.ATLAS / "ui" / "app.js").read_text(encoding="utf-8")
    page = (ui_server.ATLAS / "ui" / "index.html").read_text(encoding="utf-8")
    assert 'fetch("/jobs", { cache: "no-store" })' in client
    assert '/jobs/${encodeURIComponent(selectedJobId)}/events' in client
    assert '/jobs/${encodeURIComponent(jobId)}/result' in client
    assert 'fetch("/signal", {cache: "no-store"})' in client
    assert '/guided-setups/${encodeURIComponent(guideId)}' in client
    assert '"x-atlas-action-token": actionToken' in client
    assert "localStorage" not in client and "sessionStorage" not in client
    assert "snapshot.filed_cards" not in client
    assert "streams microphone audio to the configured speech-to-text provider" in client
    assert "The page does not capture audio" in page
    assert "Your local intelligence layer" not in page
    assert "polling every second" not in page.casefold()


def test_home_is_viewport_fixed_and_secondary_views_scroll_internally():
    styles = (ui_server.ATLAS / "ui" / "styles.css").read_text(encoding="utf-8")
    page = (ui_server.ATLAS / "ui" / "index.html").read_text(encoding="utf-8")
    assert "html, body" in styles and "overflow: hidden" in styles
    assert ".view--secondary" in styles and "overflow: auto" in styles
    assert 'class="live-panel transcript-panel"' in page
    assert 'class="live-panel task-panel"' in page
    assert 'id="task-tabs"' in page
    assert '<h1>Transcript</h1>' in page
    assert '<h1>Workers</h1>' in page
    assert 'id="atlas-visual"' in page
    assert 'id="atlas-engine"' in page
    assert 'href="/ui/favicon.svg"' in page
    assert 'class="brand-button is-active"' in page


def test_workers_only_keeps_live_jobs_and_completed_runs_move_to_history():
    client = (ui_server.ATLAS / "ui" / "app.js").read_text(encoding="utf-8")
    assert "ACTIVE_JOB_STATES.has" in client
    assert "TERMINAL_JOB_STATES.has" in client
    assert 'node("article", "history-item history-run")' in client
    assert 'setBadge(refs.workersBadge, workerAttention)' in client
