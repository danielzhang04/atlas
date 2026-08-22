"""Standalone Atlas LiveKit voice worker.

Pipeline: local wake gate -> Deepgram Flux STT -> conversational Claude response with an optional
hidden work proposal -> host-owned durable admission -> Deepgram/ElevenLabs TTS. The LiveKit agent
itself has no LLM or tools; every finalized turn terminates at ``VoiceFrontDesk``.

Run (from atlas/):
    .venv\\Scripts\\python -m worker.app console                 # desk mic/speaker
    .venv\\Scripts\\python -m worker.app console --text          # audio-free smoke
    .venv\\Scripts\\python -m worker.app console --list-devices  # enumerate audio devices
Console mode needs DEEPGRAM_API_KEY + ANTHROPIC_API_KEY in %USERPROFILE%\\.atlas\\env.
"""
import asyncio
import logging
import os
import sys
import threading
from pathlib import Path

import yaml

from livekit.agents import Agent, AgentSession, JobContext, StopResponse, WorkerOptions, cli
# elevenlabs imported at module level even though only some voices use it: livekit plugins
# self-register on import and MUST do so on the main thread (job tasks raise RuntimeError).
from livekit.plugins import deepgram, elevenlabs, silero

from worker import devicewatch
from worker import engagement as engagement_mod
from worker import (actionauth, envload, router, runtime, sanitize, state, stateserver, wakeword)
from worker.capability_runner import (
    FastCapabilityWorker,
    SharedCapabilityBroker,
)
from worker.contracts import JobState
from worker.guided_setup import GuidedSetupAdmission
from worker.voice_runtime import build_production_voice_runtime
from worker.worker_health_file import DEFAULT_HEALTH_FILE, health_path, read_health

ATLAS = Path(__file__).resolve().parents[1]
logger = logging.getLogger("atlas.app")

# Fallback voice if config has no voices/active_voice (pre-bake-off default).
TTS_VOICE = "aura-2-andromeda-en"

# Output-follow sentinel (docs/specs/2026-07-21-atlas-output-follow-design.md): tts_output_device
# set to exactly this lowercase string switches TTS from static-pin to hot-follow the Windows default
# output. Any other non-empty string is the existing static substring pin, byte-for-byte unchanged.
FOLLOW_SENTINEL = "follow"

def _build_tts(cfg: dict):
    """TTS from the config voice toggle: voices[active_voice] -> vendor-specific plugin.
    Daniel switches voices by editing active_voice and restarting the console (V1: spoken switch)."""
    entry = (cfg.get("voices") or {}).get(cfg.get("active_voice") or "")
    if not entry:
        return deepgram.TTS(model=TTS_VOICE)
    if entry["vendor"] == "deepgram":
        return deepgram.TTS(model=entry["model"])
    if entry["vendor"] == "elevenlabs":
        from livekit.agents.utils import http_context
        # plugin's env fallback is ELEVEN_API_KEY; our env file uses ELEVENLABS_API_KEY — pass explicitly.
        # http_session passed explicitly: the plugin otherwise creates it lazily on FIRST synthesis,
        # and our first synthesis ("Yes?") fires from the wake-thread callback via call_soon_threadsafe,
        # which runs outside the job-context ContextVar -> RuntimeError (desk traceback 2026-07-20).
        # _build_tts is only called from entrypoint, where the job context is active.
        return elevenlabs.TTS(voice_id=entry["voice_id"], model=entry["model"],
                              api_key=os.environ.get("ELEVENLABS_API_KEY"),
                              http_session=http_context.http_session())
    raise ValueError(f"unknown voice vendor: {entry['vendor']}")

# Text-mode console (`--text`) bypasses audio entirely, so wake gating doesn't apply — only the
# audio path is gated. Detected from argv because the CLI flag is parsed by livekit's typer app.
TEXT_MODE = "--text" in sys.argv

_BG_TASKS: set = set()   # strong refs to fire-and-forget worker tasks


# Reflex intents live in config/intents.yaml (design §7, loaded by router.load_intents). This
# constant is ONLY the fallback used when that file is missing, so sleep-by-voice never depends on
# a file that failed to ship — the phrases themselves have MIGRATED out of atlas.yaml into
# intents.yaml (the single source of reflex matching data).
DEFAULT_DISMISS = ["that's all", "go to sleep"]

# Canonical session-frame lines (conversation-rules design §3, Daniel-judged 2026-07-21).
WAKE_LINE = "Hey boss. What can I do for you?"
SLEEP_LINE = "Okay, sleeping. Wake me when you need something."


def _load_intents() -> dict:
    """Reflex intents from config/intents.yaml; if that file is absent, fall back to a
    dismiss-only intent set so voice-sleep still works (Task 10 fallback, documented)."""
    path = ATLAS / "config" / "intents.yaml"
    if path.is_file():
        return router.load_intents(path)
    logger.warning("intents.yaml missing — reflex lane limited to the dismiss fallback")
    return {"dismiss": {"phrases": DEFAULT_DISMISS}}


def _cfg() -> dict:
    return yaml.safe_load((ATLAS / "config" / "atlas.yaml").read_text(encoding="utf-8"))


def _stt_keyterms(cfg: dict) -> list[str]:
    """Return a bounded standalone STT vocabulary from trusted Atlas config."""
    values = cfg.get("stt_keyterms") or ["Atlas"]
    if not isinstance(values, list) or len(values) > 64:
        return ["Atlas"]
    terms = [value.strip() for value in values
             if isinstance(value, str) and 0 < len(value.strip()) <= 64]
    return terms[:64] or ["Atlas"]


def _apply_agent_state(engagement, publisher, agent_state: str) -> None:
    """Body of the agent_state_changed handler, extracted so the wiring is unit-testable.

    design §2: THINKING = LLM turn in flight, SPEAKING = TTS playing, else LISTENING. The session's
    own AgentState (agent_session.py:1757) gives these directly. Guarded by ENGAGED so session
    chatter while ASLEEP — the "Going to sleep." ack, warm-up ("initializing"->"listening") — never
    overrides the ASLEEP orb. Agent events never change the explicit wake/sleep lifecycle."""
    if engagement.state != engagement_mod.ENGAGED:
        return
    mapped = state.STATE_FROM_AGENT.get(agent_state)
    if mapped is not None:
        publisher.set_state(mapped)


def _boot_default_output_name() -> str | None:
    """Name of the output device livekit opened at boot (PortAudio's boot-time default)."""
    try:
        import sounddevice as sd
        return sd.query_devices(sd.default.device[1])["name"]
    except Exception:
        return None


def _output_device_status(cfg: dict, resolve=wakeword.resolve_output_device,
                          boot_default=_boot_default_output_name) -> dict:
    """{'configured', 'resolved', 'following'} for the TTS output, surfaced in GET /state (M4).

    Three modes: absent (system default, not following), a name substring (static pin,
    unchanged since 2026-07-21), or the sentinel 'follow' (output-follow design: the
    watcher moves the stream when the Windows default endpoint changes; `resolved` is
    updated live by the follower and starts as the boot default)."""
    configured = cfg.get("tts_output_device")
    if not configured:
        return {"configured": None, "resolved": None, "following": False}
    if configured == FOLLOW_SENTINEL:
        return {"configured": FOLLOW_SENTINEL, "resolved": boot_default(), "following": True}
    idx = resolve(configured)
    if idx is None:
        return {"configured": configured, "resolved": None, "following": False}
    try:
        import sounddevice as sd
        name = sd.query_devices(idx)["name"]
    except Exception:
        name = str(idx)
    return {"configured": configured, "resolved": name, "following": False}


def _console_singleton():
    """The live console object whose set_speaker_enabled hot-reopens the output stream.

    livekit.agents.cli._legacy is a PRIVATE module (import-guarded here): AgentsConsole is a
    singleton (get_instance, _legacy.py:285-293) and set_speaker_enabled (:597) is the same
    close-and-reopen primitive livekit's own console UI calls (:1441). If a livekit upgrade
    moves it, _start_output_follow degrades loudly instead of crashing the worker."""
    from livekit.agents.cli._legacy import AgentsConsole
    return AgentsConsole.get_instance()


def _restart_worker(reason: str) -> None:
    """Deliberate hard self-exit so pm2 revives the worker with a fresh PortAudio snapshot.

    Called from the devicewatch thread when a swap cannot cleanly open its device (stale
    snapshot after a Bluetooth topology change — live finding 2026-07-22). os._exit because
    sys.exit from a non-main thread only kills that thread; pm2 owns the restart. Exit code
    21 marks a device-table restart in pm2 logs, distinct from crashes."""
    logger.critical("output-follow requested worker restart: %s — exiting for pm2 revive", reason)
    import os
    os._exit(21)


def _start_output_follow(cfg: dict, publisher, *,
                         console_factory=_console_singleton,
                         watcher_cls=devicewatch.DeviceWatcher,
                         follower_cls=devicewatch.OutputFollower,
                         probe=devicewatch.current_default_output):
    """Start the output-follow watcher when configured; returns it, or None (pin/absent mode).

    Failure to reach the console (livekit internals moved, pycaw missing) is loud-but-running:
    CRITICAL log + /state shows following:false with the boot default — design 'fail loud,
    run anyway'."""
    if cfg.get("tts_output_device") != FOLLOW_SENTINEL:
        return None
    # Startup self-check (spec 'Dependencies'): a dead probe (pycaw missing, COM broken)
    # means the watcher would silently never fire — refuse to claim following:true.
    probe_ok = False
    try:
        probe_ok = probe() is not None
    except Exception:
        pass
    if not probe_ok:
        logger.critical(
            "output-follow configured but the default-endpoint probe returned nothing "
            "(pycaw/comtypes missing or COM failure) — TTS stays on the boot default and "
            "will NOT follow device changes. `pip install pycaw comtypes` into the worker venv.")
        publisher.set_output_device(
            {"configured": FOLLOW_SENTINEL, "resolved": _boot_default_output_name(),
             "following": False})
        return None
    try:
        console = console_factory()
    except Exception:
        logger.critical(
            "output-follow configured but the console audio object is unavailable — TTS stays "
            "on the boot default and will NOT follow device changes", exc_info=True)
        publisher.set_output_device(
            {"configured": FOLLOW_SENTINEL, "resolved": _boot_default_output_name(),
             "following": False})
        return None
    # Review finding #4 (2026-07-21): everything past the two guards must ALSO degrade
    # loudly rather than fail the whole voice job — same "fail loud, run anyway" envelope.
    try:
        import sounddevice as sd
        try:
            # Review finding #2: seed the follower with the boot output index livekit opened,
            # so even the first swap's open-failure has a known-good device to fall back to.
            boot_idx = sd.default.device[1]
        except Exception:
            boot_idx = None
        follower = follower_cls(
            console,
            resolve_output=wakeword.resolve_output_device,
            sd_module=sd,
            initial_idx=boot_idx,
            request_restart=_restart_worker)

        def _on_change(name: str) -> None:
            publisher.set_output_device(follower.swap_to(name))

        # Review finding #1: hold the first poll 10s past boot — livekit's own
        # set_speaker_enabled call at console-audio-mode entry is the only concurrent
        # caller, and it happens within seconds of startup.
        watcher = watcher_cls(probe=probe, on_change=_on_change, period_s=1.5,
                              initial_delay_s=10.0)
        watcher.start()
        logger.info("TTS output-follow active: tracking the Windows default output device")
        return watcher
    except Exception:
        logger.critical(
            "output-follow wiring failed — TTS stays on the boot default and will NOT follow "
            "device changes", exc_info=True)
        publisher.set_output_device(
            {"configured": FOLLOW_SENTINEL, "resolved": _boot_default_output_name(),
             "following": False})
        return None


class AtlasAgent(Agent):
    """LiveKit audio shell whose finalized turns always stop at the host front desk.

    `on_user_turn_completed` is the ONLY livekit-agents 1.6.6 hook that runs after the user turn is
    finalized but BEFORE the reply is generated, and whose `StopResponse` keeps the utterance out of
    the LLM chat context: raising it there returns from `_user_turn_completed_task`
    (agent_activity.py:2334) *before* the user ChatMessage is appended to chat_ctx and before
    `_generate_reply` (verified against the installed source). That is what lets a reflex utterance
    never reach Claude while a normal utterance falls straight through to the fast lane.

    ``turn_handler`` is installed by ``entrypoint``.  There is no autonomous LiveKit LLM fallback:
    a missing handler fails closed and a handled turn is always suppressed before chat insertion."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.turn_handler = None

    async def on_user_turn_completed(self, turn_ctx, new_message) -> None:
        handler = self.turn_handler
        if handler is None:
            raise StopResponse()
        text = getattr(new_message, "text_content", None) or ""
        await handler(text)
        # The host already spoke or failed closed. Suppress chat insertion and any reply generation.
        raise StopResponse()

    def tts_node(self, text, model_settings):
        # Voice-clean seam (rules design §2): every spoken string — LLM turns AND session.say
        # canned lines — passes through here; sanitize per chunk (split-safe by construction).
        async def _clean():
            async for chunk in text:
                yield sanitize.sanitize_for_tts(chunk)
        return Agent.default.tts_node(self, _clean(), model_settings)


async def entrypoint(ctx: JobContext) -> None:
    envload.load_private_environment()
    cfg = _cfg()
    heartbeat = health_path(str(cfg.get("subscription_health_path", DEFAULT_HEALTH_FILE)))
    action_authorizer = actionauth.PairingAuthorizer()
    runtime_services = runtime.build_runtime(
        ATLAS, cfg, action_context_provider=action_authorizer.active_context)
    shared_broker = SharedCapabilityBroker(runtime_services)
    voice_runtime = build_production_voice_runtime(cfg)
    guided_setup = GuidedSetupAdmission(voice_runtime.store, lambda: read_health(heartbeat))
    fast_worker = FastCapabilityWorker(
        voice_runtime.store, shared_broker)
    # Pairing secrets are never emitted through the service log. The interactive standalone
    # launcher owns the one-time local bootstrap ceremony.
    keyterms = _stt_keyterms(cfg)

    stt_kwargs: dict = {"model": "flux-general-en"}
    if keyterms:
        stt_kwargs["keyterm"] = keyterms

    await ctx.connect()  # console mode: connects the simulated room
    session = AgentSession(
        stt=deepgram.STTv2(**stt_kwargs),
        vad=silero.VAD.load(),                       # VAD barge-in; the AdaptiveInterruptionDetector WARNING at
                                                     # startup is expected+harmless (that path needs LiveKit-hosted
                                                     # inference / LIVEKIT_API_KEY, absent by design — falls back to VAD)
        llm=None,
        tts=_build_tts(cfg),
    )
    agent = AtlasAgent(
        instructions="Atlas voice I/O is host controlled; do not generate or execute model turns.",
        llm=None,
        tools=[],
    )
    await session.start(agent=agent, room=ctx.room)

    # --- Unified worker state (design §2): a pure observer of the voice loop. The HTTP
    # surface (Task 5), transcript ledger (Task 6), and done-watcher (Task 11) all consume
    # this ONE stream. `voice` mirrors the active config voice.
    publisher = state.StatePublisher(voice=cfg.get("active_voice"))
    engagement = engagement_mod.Engagement()
    pending_voice_jobs: set[str] = set()
    # M4 (2026-07-21): surface the TTS output-device pin in /state so a bad pin (configured but not
    # resolved) is visible on the dashboard after Daniel walks away, not just a scrolling log line.
    publisher.set_output_device(_output_device_status(cfg))

    # Output-follow (design 2026-07-21): when tts_output_device is 'follow', a watcher thread
    # tracks the Windows default output endpoint and hot-moves the TTS stream — headphones
    # connect, Atlas speaks there; disconnect, back to the speakers. No restart.
    _start_output_follow(cfg, publisher)

    async def _submit_voice_turn(text: str) -> None:
        """One conversational turn, with host-owned admission when Claude proposes work."""
        if not isinstance(text, str) or not text.strip():
            return
        publisher.add_line("user", text)
        publisher.set_state(state.THINKING)
        result = await voice_runtime.desk.handle(
            text,
            catalog=runtime_services.catalog_projection(),
            idempotency_key=f"voice:{publisher.session_id or 'text'}:{text}",
        )
        publisher.set_state(state.LISTENING)
        session.say(result.text, add_to_chat_ctx=False)
        publisher.add_line("atlas", result.text)
        if result.job_id is not None and result.status in {"queued", "running"}:
            pending_voice_jobs.add(result.job_id)

    # Text-console turns and the later audio handler both terminate here.  The Agent has no LLM
    # fallback, so an unavailable conversation model can only produce VoiceFrontDesk's honest,
    # bounded service-failure response.
    agent.turn_handler = _submit_voice_turn

    stop_fast_worker = asyncio.Event()

    async def _fast_worker_loop() -> None:
        while not stop_fast_worker.is_set():
            delay = 0.25
            try:
                completed = await asyncio.to_thread(fast_worker.run_once)
            except Exception:
                logger.exception("fast capability worker failed; retrying after a bounded delay")
                completed = None
                delay = 1.0
            if completed is None:
                try:
                    await asyncio.wait_for(stop_fast_worker.wait(), timeout=delay)
                except asyncio.TimeoutError:
                    pass

    async def _completion_loop() -> None:
        terminal = {
            JobState.SUCCEEDED, JobState.FAILED, JobState.CANCELLED, JobState.UNAVAILABLE,
        }
        while not stop_fast_worker.is_set():
            for job_id in tuple(pending_voice_jobs):
                try:
                    job = await asyncio.to_thread(voice_runtime.store.get, job_id)
                except Exception:
                    continue
                if job.state not in terminal:
                    continue
                pending_voice_jobs.discard(job_id)
                summary = job.public_payload.get("summary")
                if job.state is JobState.SUCCEEDED and isinstance(summary, str):
                    clean = " ".join(summary.split())
                    line = f"Done. {clean[:320]}" if clean else "Done."
                elif job.state is JobState.CANCELLED:
                    line = "That task was cancelled."
                elif job.state is JobState.UNAVAILABLE:
                    line = "That task couldn't start because Claude Code is unavailable."
                else:
                    line = "That task ran into a problem. Open History for the details."
                publisher.add_line("atlas", line)
                if engagement.state == engagement_mod.ENGAGED:
                    session.say(line, add_to_chat_ctx=False)
            try:
                await asyncio.wait_for(stop_fast_worker.wait(), timeout=0.5)
            except asyncio.TimeoutError:
                pass

    fast_task = asyncio.create_task(_fast_worker_loop())
    completion_task = asyncio.create_task(_completion_loop())
    _BG_TASKS.add(fast_task)
    _BG_TASKS.add(completion_task)
    fast_task.add_done_callback(_BG_TASKS.discard)
    completion_task.add_done_callback(_BG_TASKS.discard)

    async def _stop_work_plane() -> None:
        stop_fast_worker.set()
        await asyncio.gather(fast_task, completion_task)
        voice_runtime.close()
    ctx.add_shutdown_callback(_stop_work_plane)

    # --- Local read-only /state surface (design §3, Task 5). Started HERE on the job-context
    # event loop (same placement discipline as _build_tts) so it stays inside the job context and
    # off the wake thread — the V0 landmine. Bound to 127.0.0.1 only; serves publisher.snapshot()
    # + a request-time heartbeat. Started BEFORE the TEXT_MODE return so the --text REPL path also
    # exposes /state (the REPL benefits from the same surface), and its shutdown is registered for
    # both paths.
    state_srv = await stateserver.start(
        publisher,
        cfg["state_port"],
        catalog_provider=runtime_services.catalog_projection,
        action_broker=runtime_services.actions,
        action_authorizer=action_authorizer,
        receipt_provider=(lambda: runtime_services.receipts.read_latest(100))
        if runtime_services.receipts is not None else None,
        job_provider=voice_runtime.jobs_projection,
        job_event_provider=voice_runtime.job_events_projection,
        result_provider=voice_runtime.store.get_protected_result,
        health_provider=lambda: read_health(heartbeat),
        guided_setup_provider=guided_setup.start,
        surface_mode="voice",
    )

    async def _stop_state_server() -> None:
        await state_srv.stop()
    ctx.add_shutdown_callback(_stop_state_server)

    if TEXT_MODE:
        return  # audio-free smoke: no wake gate, no mic loop — text turns flow straight through

    # --- Explicit listening lifecycle ---------------------------------------------------------
    # The wake-word loop is always on locally and never streams audio anywhere; it only flips the
    # engagement state. The Deepgram STT audio input is detached (set_audio_enabled(False)) while
    # ASLEEP so no mic audio reaches Deepgram, and re-attached on wake. It remains attached for
    # the conversation and detaches only on an explicit dismiss phrase.
    loop = asyncio.get_running_loop()
    session.input.set_audio_enabled(False)  # start ASLEEP: no audio to STT until "hey jarvis"

    def _sleep(announce: bool = True) -> bool:
        """Close the mic; returns True only on a real transition. Audible cue (Daniel's ask:
        never leave him guessing whether Atlas is still listening)."""
        if not session.input.audio_enabled:
            return False
        session.input.set_audio_enabled(False)
        publisher.set_state(state.ASLEEP)
        logger.info("ASLEEP — mic detached, no audio leaves the PC (wake word to re-engage)")
        if announce:
            session.say(SLEEP_LINE, add_to_chat_ctx=False)
            publisher.add_line("atlas", SLEEP_LINE)  # audible, so it's mirrored
        return True

    def _engage() -> None:
        already = engagement.state == engagement_mod.ENGAGED
        engagement.wake()
        session.input.set_audio_enabled(True)  # open the STT stream — audio now leaves the PC
        logger.info("ENGAGED — listening until an explicit dismiss phrase")
        if not already:
            publisher.start_session()             # new wake-session id per wake
            publisher.set_state(state.LISTENING)
            session.say(WAKE_LINE, add_to_chat_ctx=False)
            publisher.add_line("atlas", WAKE_LINE)

    @session.on("agent_state_changed")
    def _on_agent_state(ev) -> None:
        _apply_agent_state(engagement, publisher, ev.new_state)

    def _on_wake() -> None:  # called from the wake-word thread; hop to the event loop
        loop.call_soon_threadsafe(_engage)

    def _on_audio_energy(value: float) -> None:
        # The wake thread supplies one normalized scalar per 80 ms frame. Keep all publisher
        # mutation on the event loop; raw microphone samples never cross this boundary.
        loop.call_soon_threadsafe(publisher.set_audio_energy, value)

    # --- Explicit sleep control. This is the sole pre-model speech classification after wake. ---
    intents = _load_intents()

    async def _handle_reflex(text: str) -> bool:
        """Handle an explicit dismiss; every other utterance flows to Claude unchanged.

        The host owns the wake/sleep boundary. It does not classify normal conversation, reject
        language, or infer intent from fixed phrase tables."""
        if not text or not text.strip():
            return False
        lane, intent = router.route(text, intents)
        if lane != "reflex" or intent != "dismiss":
            return False
        publisher.add_line("user", text)  # the reflex utterance, mirrored (won't arrive elsewhere)
        if intent == "dismiss":
            engagement.dismiss()
            _sleep()                       # speaks + mirrors "Going to sleep."
        return True

    async def _handle_audio_turn(text: str) -> None:
        if not await _handle_reflex(text):
            await _submit_voice_turn(text)

    agent.turn_handler = _handle_audio_turn

    # Quiet the "Atlas is DEAF" CRITICAL on Ctrl+C: flag teardown so the wake thread's error
    # path knows the stream tore down deliberately (a mid-run mic failure still logs loudly).
    async def _quiet_shutdown() -> None:
        wakeword.shutting_down.set()
    ctx.add_shutdown_callback(_quiet_shutdown)

    # daemon thread: blocking mic read + onnx wake scoring, off the event loop
    threading.Thread(target=wakeword.listen, args=(_on_wake, cfg["wake_model"]),
                     kwargs={"device": cfg.get("wake_input_device"),
                             "threshold": cfg.get("wake_threshold", wakeword.THRESHOLD),
                             "patience": cfg.get("wake_patience", wakeword.PATIENCE),
                             "on_energy": _on_audio_energy},
                     daemon=True).start()

def _console_output_args(argv: list[str], cfg: dict,
                         resolve=wakeword.resolve_output_device) -> list[str]:
    """Extra CLI args pinning the TTS OUTPUT device for `console` audio mode (Bug 2, 2026-07-21).

    livekit's console output falls back to `sd.default.device[1]` — the drifting Windows default
    output — and opens its OutputStream on it ONCE at startup (cli/_legacy.py set_speaker_enabled),
    so TTS can end up on an inaudible AirPods HFP sink and never follow Daniel switching to the main
    speaker. When atlas.yaml sets `tts_output_device` (a name substring, exactly like
    `wake_input_device`) and an audio console is running without an explicit --output-device, we
    resolve it to a device index and pass `--output-device <idx>`, which livekit hands straight to
    sounddevice. Returns [] (system default, unchanged) when: not a console run, --text/--list-devices,
    an explicit --output-device is already present, no config key, or the configured device is not
    found (resolve() logs a loud warning — never a silent swap)."""
    if "console" not in argv or "--text" in argv or "--list-devices" in argv:
        return []
    if any(a == "--output-device" or a.startswith("--output-device=") for a in argv):
        return []
    substring = cfg.get("tts_output_device")
    if not substring:
        return []
    if substring == FOLLOW_SENTINEL:
        # Output-follow mode: pass no flag. livekit opens on the boot-time default, which
        # IS the current Windows default at start; the devicewatch follower moves the
        # stream afterward (docs/specs/2026-07-21-atlas-output-follow-design.md).
        return []
    idx = resolve(substring)
    if idx is None:
        # Loud, non-fatal: Atlas still starts, but Daniel is told WHY he may hear nothing.
        logger.critical(
            "TTS output device %r not found — Atlas will fall back to the system default output, "
            "which on this machine drifts to an inaudible Bluetooth sink; you may hear nothing. "
            "Fix `tts_output_device` in atlas/config/atlas.yaml or connect the named speaker.",
            substring)
        return []
    try:
        import sounddevice as sd
        name = sd.query_devices(idx)["name"]
    except Exception:
        name = "?"
    logger.info("TTS output pinned to device [%d] %s (config tts_output_device=%r)",
                idx, name, substring)
    return ["--output-device", str(idx)]


def main() -> int:
    # Pin the TTS output device from config before livekit resolves the (drifting) system default.
    try:
        sys.argv.extend(_console_output_args(sys.argv, _cfg()))
    except Exception:
        logger.exception("could not resolve tts_output_device — using the system default output")
    # Desk finding (2026-07-21, pm2 rollout): the console's STT mic is a SEPARATE capture from
    # the wake listener and defaults to the OS default input — which drifts to AirPods HFP
    # (unusable) while indices reshuffle on BT connect (the V0 landmine). A raw name substring
    # can't ride the CLI flag: the same physical mic appears under MME/DirectSound/WASAPI and
    # sounddevice raises on multiple matches — so resolve the INDEX here at start time with the
    # wake listener's own resolver (first input device matching the name, same pin, same config
    # key). An explicit user-passed flag wins.
    if "console" in sys.argv and "--input-device" not in sys.argv:
        idx = wakeword.resolve_input_device(_cfg().get("wake_input_device"))
        if idx is not None:
            sys.argv += ["--input-device", str(idx)]
    cli.run_app(WorkerOptions(entrypoint_fnc=entrypoint))
    return 0


if __name__ == "__main__":
    sys.exit(main())
