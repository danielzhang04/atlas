from worker.engagement import Engagement


def test_wake_stays_engaged_until_explicit_dismiss():
    e = Engagement()
    assert e.state == "ASLEEP"
    e.wake()
    assert e.state == "ENGAGED"

    # There is deliberately no clock or tick transition. A pause between turns does not end the
    # conversation or require Daniel to address Atlas again.
    assert not hasattr(e, "tick")
    assert e.state == "ENGAGED"

    e.dismiss()
    assert e.state == "ASLEEP"


def test_resolve_input_device_pins_by_substring():
    from worker.wakeword import resolve_input_device
    devices = [
        {"name": "Headset (AirPods)", "max_input_channels": 1},
        {"name": "Speakers (out only)", "max_input_channels": 0},
        {"name": "Microphone Array (Intel Smart Sound)", "max_input_channels": 4},
    ]
    assert resolve_input_device("intel", devices) == 2
    assert resolve_input_device("Speakers", devices) is None  # output-only never matches
    assert resolve_input_device(None, devices) is None
    assert resolve_input_device("nope", devices) is None


def test_resolve_output_device_pins_by_substring():
    # Bug 2 (2026-07-21): the speaker analogue of resolve_input_device — matches OUTPUT devices only.
    from worker.wakeword import resolve_output_device
    devices = [
        {"name": "Speakers (Realtek High Definition Audio)", "max_output_channels": 2},
        {"name": "Microphone Array (Intel Smart Sound)", "max_output_channels": 0},
        {"name": "Headset Earphone (AirPods Hands-Free)", "max_output_channels": 2},
    ]
    assert resolve_output_device("speakers", devices) == 0
    assert resolve_output_device("airpods", devices) == 2
    assert resolve_output_device("Intel", devices) is None   # input-only never matches
    assert resolve_output_device(None, devices) is None
    assert resolve_output_device("nope", devices) is None


def test_console_output_args_pins_configured_speaker():
    # Bug 2: with tts_output_device set and an audio console running without an explicit
    # --output-device, the worker injects `--output-device <idx>` so TTS does not ride the
    # drifting system default. resolve is injected so the test needs no real audio hardware.
    from worker.app import _console_output_args
    cfg = {"tts_output_device": "Speakers"}
    assert _console_output_args(["app", "console"], cfg, resolve=lambda s: 3) == ["--output-device", "3"]


def test_console_output_args_respects_explicit_and_text_and_missing_config():
    from worker.app import _console_output_args
    cfg = {"tts_output_device": "Speakers"}
    boom = lambda s: (_ for _ in ()).throw(AssertionError("resolve must not be called"))
    # explicit --output-device already present -> leave it
    assert _console_output_args(["app", "console", "--output-device", "5"], cfg, resolve=boom) == []
    assert _console_output_args(["app", "console", "--output-device=5"], cfg, resolve=boom) == []
    # text mode / list-devices / non-console -> no audio to pin
    assert _console_output_args(["app", "console", "--text"], cfg, resolve=boom) == []
    assert _console_output_args(["app", "console", "--list-devices"], cfg, resolve=boom) == []
    assert _console_output_args(["app", "start"], cfg, resolve=boom) == []
    # no config key -> unchanged (system default)
    assert _console_output_args(["app", "console"], {}, resolve=boom) == []


def test_console_output_args_falls_back_loud_when_device_missing():
    # Configured device not found -> [] (system default) rather than a crash; resolve returns None
    # exactly as resolve_output_device does on no-match (which also logs a warning).
    from worker.app import _console_output_args
    cfg = {"tts_output_device": "NonexistentSpeaker"}
    assert _console_output_args(["app", "console"], cfg, resolve=lambda s: None) == []


# --- Agent UI state is observational; it cannot change the explicit lifecycle -------------------

def test_apply_agent_state_updates_ui_without_changing_engagement():
    from worker.app import _apply_agent_state
    from worker import state

    e = Engagement()
    e.wake()
    pub = state.StatePublisher()
    _apply_agent_state(e, pub, "speaking")
    assert pub.state == state.SPEAKING
    assert e.state == "ENGAGED"


def test_apply_agent_state_is_noop_while_asleep():
    from worker.app import _apply_agent_state
    from worker import state

    e = Engagement()
    pub = state.StatePublisher()
    _apply_agent_state(e, pub, "speaking")
    assert e.state == "ASLEEP"
    assert pub.state == state.ASLEEP


# --- M4: output-device pin status surfaced in /state --------------------------------------------

def test_output_device_status_reports_configured_and_resolution():
    from worker.app import _output_device_status
    # no config -> both null (following:false — output-follow contract, 2026-07-21)
    assert _output_device_status({}, resolve=lambda s: None) == {
        "configured": None, "resolved": None, "following": False}
    # configured but unresolved -> visible bad pin (resolved null)
    assert _output_device_status({"tts_output_device": "Ghost"},
                                 resolve=lambda s: None) == {
        "configured": "Ghost", "resolved": None, "following": False}
    # configured + resolved -> configured echoed, resolved is a device name (idx 0 here)
    st = _output_device_status({"tts_output_device": "Speakers"}, resolve=lambda s: 0)
    assert st["configured"] == "Speakers" and st["resolved"] is not None


def test_resolve_model_custom_path_loads_by_file_and_stem_key():
    # config/hey_atlas.onnx exists in the repo -> load by full path, predict-key = file stem.
    # openwakeword keys a path-loaded single-output model by os.path.splitext(basename)[0].
    from worker.wakeword import _resolve_model, ATLAS
    arg, key = _resolve_model("hey_atlas")
    assert arg == str(ATLAS / "config" / "hey_atlas.onnx")
    assert (ATLAS / "config" / "hey_atlas.onnx").exists()   # the arg is a real file, so oww loads it as a path
    assert key == "hey_atlas"                                # == wake_model, so listen()'s lookup fires


def test_resolve_model_pretrained_name_passes_through():
    # No config/hey_jarvis.onnx -> treated as a pretrained NAME, keyed by that bare name.
    from worker.wakeword import _resolve_model, ATLAS
    assert not (ATLAS / "config" / "hey_jarvis.onnx").exists()
    arg, key = _resolve_model("hey_jarvis")
    assert arg == "hey_jarvis"
    assert key == "hey_jarvis"


def test_ensure_models_custom_needs_only_feature_models(monkeypatch):
    # A path-loaded custom model must NOT require a pretrained <name>_v0.1.onnx; only the shared
    # feature models. Since those are already cached, ensure_models() must early-return (no download).
    import worker.wakeword as ww
    called = []
    monkeypatch.setattr(ww, "download_models",
                        lambda names: called.append(names), raising=False)
    import openwakeword.utils as owwu
    monkeypatch.setattr(owwu, "download_models", lambda names: called.append(names))
    ww.ensure_models("hey_atlas")
    assert called == []          # feature models present -> no fetch attempted for the custom model


def test_dismiss_phrases_route_reflex():
    # Only an explicit sleep phrase bypasses Claude. Gratitude and all other ordinary language
    # remain in the active conversation.
    from worker import router
    intents = {"dismiss": {"phrases": ["that's all", "go to sleep"]}}
    assert router.route("That's all.", intents) == ("reflex", "dismiss")
    assert router.route("thats all", intents) == ("reflex", "dismiss")   # Deepgram may drop the apostrophe
    assert router.route("Go to sleep", intents) == ("reflex", "dismiss")
    assert router.route("Thanks, Atlas!", intents) == ("fast", None)
    assert router.route("Something went wrong.", intents) == ("fast", None)
    assert router.route("that's all I know about it", intents) == ("fast", None)
    assert router.route("what's in the queue?", intents) == ("fast", None)


def test_build_tts_voice_toggle_config():
    from worker.app import _build_tts
    import worker.app as app
    # config selects matilda (elevenlabs) — verify vendor routing without constructing plugins
    cfg = {"active_voice": "x", "voices": {"x": {"vendor": "nope"}}}
    try:
        _build_tts(cfg)
        assert False, "unknown vendor should raise"
    except ValueError as e:
        assert "unknown voice vendor" in str(e)
