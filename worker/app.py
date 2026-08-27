"""Run the standalone Atlas LiveKit voice worker."""
from __future__ import annotations

import asyncio
import inspect
import logging
import os
from pathlib import Path
import sys
import threading
from typing import Any, Awaitable, Callable

import yaml

from livekit.agents import Agent, AgentSession, JobContext, StopResponse, WorkerOptions, cli
from livekit.plugins import deepgram, silero

from worker import brain as brain_mod
from worker import devicewatch
from worker import engagement as engagement_mod
from worker import envload, jobobject, router, runtime, sanitize, state, stateserver
from worker import tools as tools_mod
from worker import wakeword, work as work_mod
from worker.jobstore import JobState

__all__ = ["AtlasAgent", "entrypoint", "main"]

ATLAS = Path(__file__).resolve().parents[1]
logger = logging.getLogger("atlas.app")

TTS_VOICE = "aura-2-andromeda-en"
FOLLOW_SENTINEL = devicewatch.FOLLOW_SENTINEL
TEXT_MODE = "--text" in sys.argv
DEFAULT_DISMISS = ["that's all", "go to sleep"]
WAKE_LINE = "Hey boss. What can I do for you?"
SLEEP_LINE = "Okay, sleeping. Wake me when you need something."
_BG_TASKS: set[asyncio.Task] = set()
RESTART_EXIT_CODE = 21
_worker_exit_code = 0


def _build_tts(cfg: dict):
    entry = (cfg.get("voices") or {}).get(cfg.get("active_voice") or "")
    if not entry:
        return deepgram.TTS(model=TTS_VOICE)
    if entry["vendor"] == "deepgram":
        return deepgram.TTS(model=entry["model"])
    if entry["vendor"] == "elevenlabs":
        from livekit.agents.utils import http_context
        from livekit.plugins import elevenlabs

        return elevenlabs.TTS(
            voice_id=entry["voice_id"],
            model=entry["model"],
            api_key=os.environ.get("ELEVENLABS_API_KEY"),
            http_session=http_context.http_session(),
        )
    raise ValueError(f"unknown voice vendor: {entry['vendor']}")


def _load_intents() -> dict:
    path = ATLAS / "config" / "intents.yaml"
    if path.is_file():
        return router.load_intents(path)
    logger.warning("intents.yaml missing; reflex lane limited to dismiss")
    return {"dismiss": {"phrases": DEFAULT_DISMISS}}


def _cfg() -> dict:
    return yaml.safe_load((ATLAS / "config" / "atlas.yaml").read_text(encoding="utf-8"))


def _stt_keyterms(cfg: dict) -> list[str]:
    values = cfg.get("stt_keyterms") or ["Atlas"]
    if not isinstance(values, list) or len(values) > 64:
        return ["Atlas"]
    terms = [
        value.strip()
        for value in values
        if isinstance(value, str) and 0 < len(value.strip()) <= 64
    ]
    return terms[:64] or ["Atlas"]


def _addressing_from_config(cfg: dict, *, clock=None) -> router.Addressing:
    kwargs = {} if clock is None else {"clock": clock}
    return router.Addressing(
        float(cfg.get("addressed_window_s", 90)),
        router.vocabulary(cfg),
        **kwargs,
    )


def _apply_agent_state(engagement, publisher, agent_state: str) -> None:
    if engagement.state != engagement_mod.ENGAGED:
        return
    mapped = state.STATE_FROM_AGENT.get(agent_state)
    if mapped is not None:
        publisher.set_state(mapped)


def _restart_worker(reason: str, shutdown=None) -> None:
    global _worker_exit_code
    logger.critical("audio-follow requested worker restart: %s", reason)
    _worker_exit_code = RESTART_EXIT_CODE
    if shutdown is not None:
        shutdown(reason)


def _wake_model_callback(publisher, loop=None):
    def _changed(model_name: str) -> None:
        setter = getattr(publisher, "set_wake_model", None)
        if setter is None:
            logger.warning("wake-model state hook is unavailable")
            return
        if loop is None:
            setter(model_name)
            return
        loop.call_soon_threadsafe(setter, model_name)

    return _changed


def _should_cancel_active_jobs(shutdown_jobs_requested: bool) -> bool:
    return not shutdown_jobs_requested and _worker_exit_code != RESTART_EXIT_CODE


async def _flush_store_and_stop_state_server(store, server) -> None:
    try:
        store.close()
    except Exception:
        logger.exception("could not flush the job store during shutdown")
    await server.stop()


class AtlasAgent(Agent):
    """Suppress autonomous LiveKit replies after the host handles a finalized turn."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.turn_handler = None

    async def on_user_turn_completed(self, turn_ctx, new_message) -> None:
        handler = self.turn_handler
        if handler is not None:
            await handler(getattr(new_message, "text_content", None) or "")
        raise StopResponse()

    def tts_node(self, text, model_settings):
        async def _clean():
            async for chunk in text:
                yield sanitize.sanitize_for_tts(chunk)

        return Agent.default.tts_node(self, _clean(), model_settings)


class TurnOwnership:
    """Identify the response task that owns brain and TTS work."""

    def __init__(self) -> None:
        self._task: asyncio.Task | None = None

    @property
    def in_flight(self) -> bool:
        return self._task is not None and not self._task.done()

    async def run(self, operation: Callable[[], Awaitable[Any]]) -> Any:
        task = asyncio.current_task()
        if task is None:
            raise RuntimeError("voice turn has no asyncio task")
        previous = self._task
        if previous is not None and previous is not task and not previous.done():
            previous.cancel()
        self._task = task
        try:
            return await operation()
        finally:
            if self._task is task:
                self._task = None

    def cancel(self) -> bool:
        task = self._task
        if task is None or task.done():
            return False
        task.cancel()
        return True


async def _submit_voice_turn(
    text: str,
    *,
    brain: brain_mod.Brain,
    session,
    publisher: state.StatePublisher,
    engagement: engagement_mod.Engagement,
    context: str | None = None,
) -> str:
    if not isinstance(text, str) or not text.strip():
        return ""
    publisher.add_line("user", text)
    publisher.set_state(state.THINKING)
    spoken: list[str] = []

    async def _tee():
        response = brain.respond(text, context=context) if context is not None else brain.respond(text)
        async for chunk in response:
            spoken.append(chunk)
            yield chunk

    try:
        await session.say(_tee(), add_to_chat_ctx=False)
    finally:
        if engagement.state == engagement_mod.ENGAGED:
            publisher.set_state(state.LISTENING)
    response = "".join(spoken)
    if response:
        publisher.add_line("atlas", response)
    return response


def _last_atlas_line(publisher: state.StatePublisher) -> str | None:
    for line in reversed(publisher.snapshot()["transcript"]):
        if line.get("role") == "atlas" and isinstance(line.get("text"), str):
            return line["text"]
    return None


def _address_window_open(addressing: router.Addressing) -> bool:
    """Probe only the activity window; an empty utterance cannot hit vocabulary."""
    return addressing.is_addressed("")


async def _handle_reflex(
    text: str,
    *,
    intents: dict,
    session,
    publisher: state.StatePublisher,
    dismiss,
    cancel_turn=None,
    on_spoken=None,
) -> bool:
    if not isinstance(text, str) or not text.strip():
        return False
    lane, intent = router.route(text, intents)
    if lane != "reflex":
        return False
    repeated = _last_atlas_line(publisher) if intent == "repeat" else None
    publisher.add_line("user", text)
    if intent == "dismiss":
        dismiss()
    elif intent == "cancel":
        session.interrupt()
        if cancel_turn is not None:
            cancel_turn()
    elif intent == "repeat" and repeated:
        await session.say(repeated, add_to_chat_ctx=False)
        if on_spoken is not None:
            on_spoken()
    return True


async def _handle_audio_turn(
    text: str,
    *,
    intents: dict,
    brain: brain_mod.Brain,
    session,
    publisher: state.StatePublisher,
    engagement: engagement_mod.Engagement,
    addressing: router.Addressing,
    sleep,
    turn_ownership: TurnOwnership | None = None,
) -> str:
    if not isinstance(text, str) or not text.strip():
        return ""

    ownership = turn_ownership or TurnOwnership()
    lane, intent = router.route(text, intents)

    def _dismiss() -> None:
        engagement.dismiss()
        session.interrupt()
        ownership.cancel()
        sleep()

    if lane == "reflex" and intent == "dismiss":
        await _handle_reflex(
            text,
            intents=intents,
            session=session,
            publisher=publisher,
            dismiss=_dismiss,
        )
        return ""
    if lane == "reflex" and intent == "cancel":
        cancel_allowed = ownership.in_flight or _address_window_open(addressing)
        if cancel_allowed:
            await _handle_reflex(
                text,
                intents=intents,
                session=session,
                publisher=publisher,
                dismiss=_dismiss,
                cancel_turn=ownership.cancel,
            )
            return ""
    previous = engagement.state
    if engagement.tick() != engagement_mod.ENGAGED:
        if previous == engagement_mod.ENGAGED:
            sleep(announce=False)
        return ""
    normalized = router.normalize(text)
    if lane == "reflex" and intent == "cancel":
        publisher.add_line("ambient", text)
        return ""
    if not addressing.is_addressed(normalized):
        publisher.add_line("ambient", text)
        return ""
    engagement.interacted()
    if lane == "reflex" and intent == "repeat":
        def _repeated() -> None:
            if engagement.state != engagement_mod.ENGAGED:
                return
            engagement.interacted()
            addressing.mark_activity()

        await ownership.run(lambda: _handle_reflex(
            text,
            intents=intents,
            session=session,
            publisher=publisher,
            dismiss=_dismiss,
            cancel_turn=ownership.cancel,
            on_spoken=_repeated,
        ))
        return ""
    async def _respond() -> str:
        context = publisher.ambient_context(text)
        response = await _submit_voice_turn(
            text,
            brain=brain,
            session=session,
            publisher=publisher,
            engagement=engagement,
            context=context,
        )
        if response and engagement.state == engagement_mod.ENGAGED:
            engagement.interacted()
            addressing.mark_activity()
        return response

    return await ownership.run(_respond)


async def _sleep_watch(
    engagement: engagement_mod.Engagement,
    sleep,
    *,
    interval_s: float = 5.0,
    turn_ownership: TurnOwnership | None = None,
) -> None:
    while True:
        await asyncio.sleep(interval_s)
        previous = engagement.state
        in_flight = turn_ownership is not None and turn_ownership.in_flight
        current = engagement.tick(turn_in_flight=in_flight)
        if previous == engagement_mod.ENGAGED and current == engagement_mod.ASLEEP:
            sleep(announce=False)


def _sleep_session(
    session,
    publisher: state.StatePublisher,
    *,
    announce: bool = True,
) -> bool:
    if not session.input.audio_enabled:
        return False
    session.input.set_audio_enabled(False)
    publisher.set_state(state.ASLEEP)
    if announce:
        session.say(SLEEP_LINE, add_to_chat_ctx=False)
        publisher.add_line("atlas", SLEEP_LINE)
    else:
        publisher.add_line("system", "auto-sleep")
    return True


def _record_tool(
    publisher: state.StatePublisher,
    name: str,
    result: tools_mod.ToolResult,
) -> None:
    publisher.add_line("tool", f"{name}: {result.status}")


def _terminal_line(job) -> str:
    if job.state is JobState.SUCCEEDED:
        summary = " ".join((job.summary or "").split())
        return (f"Done — {summary}" if summary else "Done.")[:320]
    if job.state is JobState.CANCELLED:
        return "Cancelled."
    return "That task hit a problem; it's in History."


async def _await_speech(value) -> None:
    await value


def _announce_terminal(job, publisher, session, engagement, addressing=None) -> None:
    line = _terminal_line(job)
    publisher.add_line("atlas", line)
    if engagement.state != engagement_mod.ENGAGED:
        return
    engagement.interacted()
    if addressing is not None:
        addressing.mark_activity()
    speech = session.say(line, add_to_chat_ctx=False)
    if not inspect.isawaitable(speech):
        return
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        asyncio.run(_await_speech(speech))
    else:
        asyncio.ensure_future(speech)


def _jobs_projection(work: work_mod.WorkManager) -> list[dict]:
    return [job.to_public() for job in [*work.active(), *work.recent(50)]]


def _emit_ui_url(authorizer: stateserver.PairingAuthorizer, port: int) -> str:
    url = stateserver.pairing_url(authorizer, port)
    if url is None:
        raise RuntimeError("Atlas UI pairing is unavailable")
    print(f"ATLAS_UI {url}", flush=True)
    return url


def _engage_wake(
    session: Any | None,
    *,
    session_started: bool,
    publisher: state.StatePublisher,
    engagement: engagement_mod.Engagement,
    addressing: router.Addressing,
) -> None:
    """Publish a wake immediately; use session audio only once it is ready."""
    already = engagement.state == engagement_mod.ENGAGED
    engagement.wake()
    addressing.mark_activity()
    if not already:
        publisher.start_session()
        publisher.set_state(state.LISTENING)
        publisher.add_line("atlas", WAKE_LINE)
    if not session_started or session is None:
        return
    session.input.set_audio_enabled(True)
    if not already:
        session.say(WAKE_LINE, add_to_chat_ctx=False)


async def _connect_mcp_and_settle(mcp, registry, brain: brain_mod.Brain) -> None:
    """Coalesce initial MCP arrivals into one prompt snapshot rebuild."""
    settled = False

    def _on_server(_name: str, _registry) -> None:
        if settled:
            brain.refresh_tools()

    await mcp.connect(registry, on_server=_on_server)
    settled = True
    brain.refresh_tools()
    brain.mark_tools_settled()


async def entrypoint(ctx: JobContext) -> None:
    jobobject.assign_current_process()
    wakeword.shutting_down.clear()
    envload.load_private_environment()
    cfg = _cfg()
    authorizer = stateserver.PairingAuthorizer()
    server: stateserver.StateServer | None = None

    def _paired_url() -> str | None:
        if server is None:
            return None
        return stateserver.pairing_url(authorizer, server.port)

    services = runtime.build(cfg, paired_url=_paired_url)
    user = cfg.get("user")
    user_name = user.get("name") if isinstance(user, dict) else None
    publisher = state.StatePublisher(voice=cfg.get("active_voice"), user_name=user_name)
    services.registry.set_execution_observer(publisher.set_tool)
    loop = asyncio.get_running_loop()
    session: Any | None = None
    session_started = False
    mcp_task: asyncio.Task | None = None
    work_task: asyncio.Task | None = None
    engagement: engagement_mod.Engagement | None = None
    turn_ownership: TurnOwnership | None = None
    wake_switch: Any | None = None
    audio_watcher: Any | None = None
    restart_coalescer: devicewatch.AudioRestartCoalescer | None = None
    audio_failure = None
    stop_work = asyncio.Event()
    sleep_task: asyncio.Task | None = None
    shutdown_jobs_requested = False
    shutdown_started = False
    shutdown_done = asyncio.Event()

    async def _request_shutdown() -> None:
        nonlocal shutdown_jobs_requested
        shutdown_jobs_requested = True
        if engagement is not None:
            engagement.dismiss()
        if session is not None:
            session.interrupt()
        if turn_ownership is not None:
            turn_ownership.cancel()
        await services.work.cancel_active(timeout_s=15.0)
        loop.call_later(0.05, ctx.shutdown, "desktop requested shutdown")

    async def _shutdown() -> None:
        nonlocal shutdown_started
        if shutdown_started:
            await shutdown_done.wait()
            return
        shutdown_started = True
        try:
            wakeword.shutting_down.set()
            if audio_watcher is not None:
                audio_watcher.stop()
            if session is not None:
                session.interrupt()
            if turn_ownership is not None:
                turn_ownership.cancel()
            if sleep_task is not None:
                sleep_task.cancel()
                await asyncio.gather(sleep_task, return_exceptions=True)
            if _should_cancel_active_jobs(shutdown_jobs_requested):
                await services.work.cancel_active(timeout_s=15.0)
            stop_work.set()
            if mcp_task is not None:
                mcp_task.cancel()
            if work_task is not None:
                await asyncio.gather(work_task, return_exceptions=True)
            if mcp_task is not None:
                await asyncio.gather(mcp_task, return_exceptions=True)
            await services.mcp.close()
            if server is not None:
                await _flush_store_and_stop_state_server(services.store, server)
        finally:
            shutdown_done.set()

    server = await stateserver.start(
        publisher,
        int(cfg["state_port"]),
        authorizer=authorizer,
        job_provider=lambda: _jobs_projection(services.work),
        job_event_provider=services.store.events,
        result_provider=services.store.result,
        cancel_provider=services.work.cancel,
        health_provider=lambda: {
            "claude": services.work.launcher.available,
            "mcp": services.mcp.status(),
            "cache_floor_ok": services.brain.cache_floor_ok,
        },
        shutdown_token=os.environ.get("ATLAS_SHUTDOWN_TOKEN"),
        shutdown_provider=_request_shutdown,
    )
    try:
        ctx.add_shutdown_callback(_shutdown)
        _emit_ui_url(authorizer, server.port)
        services.warm_model_client()

        engagement = engagement_mod.Engagement(float(cfg["engagement_timeout_s"]))
        addressing = _addressing_from_config(cfg)
        turn_ownership = TurnOwnership()
        wake_device = cfg.get("wake_input_device")
        initial_wake_device = None
        if wake_device and wake_device != FOLLOW_SENTINEL:
            initial_wake_device = wakeword.resolve_input_device(wake_device)
        wake_switch = wakeword.InputDeviceSwitch(initial_wake_device)
        publisher.set_audio(devicewatch.audio_status(cfg))
        restart_coalescer = devicewatch.AudioRestartCoalescer(
            lambda reason: _restart_worker(reason, ctx.shutdown),
            loop=loop,
        )
        audio_failure = devicewatch.audio_failure_callback(
            publisher,
            "input",
            restart_coalescer.request,
        )

        def _engage() -> None:
            _engage_wake(
                session,
                session_started=session_started,
                publisher=publisher,
                engagement=engagement,
                addressing=addressing,
            )

        def _on_wake() -> None:
            loop.call_soon_threadsafe(_engage)

        def _on_audio_signal(value: float, bands: list[float]) -> None:
            loop.call_soon_threadsafe(publisher.set_audio_signal, value, bands)

        threading.Thread(
            target=wakeword.listen,
            args=(_on_wake, cfg["wake_model"]),
            kwargs={
                "device": cfg.get("wake_input_device"),
                "threshold": cfg.get("wake_threshold", wakeword.THRESHOLD),
                "patience": cfg.get("wake_patience", wakeword.PATIENCE),
                "on_signal": _on_audio_signal,
                "bands_enabled": lambda: publisher.state != state.ASLEEP,
                "device_switch": wake_switch,
                "on_failure": audio_failure,
                "on_model": _wake_model_callback(publisher, loop),
            },
            daemon=True,
        ).start()

        services.brain.on_tool = lambda name, result: _record_tool(publisher, name, result)
        pending_terminal = []

        def _deliver_terminal(job) -> None:
            if session is None:
                pending_terminal.append(job)
                return
            _announce_terminal(job, publisher, session, engagement, addressing)

        services.work.on_terminal(
            lambda job: loop.call_soon_threadsafe(_deliver_terminal, job)
        )

        mcp_task = asyncio.create_task(
            _connect_mcp_and_settle(services.mcp, services.registry, services.brain)
        )
        work_task = asyncio.create_task(services.work.run(stop_work))
        _BG_TASKS.update((mcp_task, work_task))
        mcp_task.add_done_callback(_BG_TASKS.discard)
        work_task.add_done_callback(_BG_TASKS.discard)

        await ctx.connect()
        stt_kwargs: dict[str, Any] = {"model": "flux-general-en"}
        keyterms = _stt_keyterms(cfg)
        if keyterms:
            stt_kwargs["keyterm"] = keyterms
        session = AgentSession(
            stt=deepgram.STTv2(**stt_kwargs),
            vad=silero.VAD.load(),
            llm=None,
            tts=_build_tts(cfg),
            turn_detection="stt",
        )
        agent = AtlasAgent(
            instructions="Atlas voice I/O is host controlled.",
            llm=None,
            tools=[],
        )
        await session.start(agent=agent, room=ctx.room)
        session_started = True
        publisher.ready = True
        for terminal_job in pending_terminal:
            _announce_terminal(terminal_job, publisher, session, engagement, addressing)
        pending_terminal.clear()

        async def _submit(text: str) -> None:
            await turn_ownership.run(lambda: _submit_voice_turn(
                text,
                brain=services.brain,
                session=session,
                publisher=publisher,
                engagement=engagement,
            ))

        agent.turn_handler = _submit

        if TEXT_MODE:
            return

        audio_watcher = devicewatch.start_audio_follow(
            cfg,
            publisher,
            wake_switch,
            request_restart=restart_coalescer.request,
        )
        session.input.set_audio_enabled(False)

        def _sleep(announce: bool = True) -> bool:
            return _sleep_session(session, publisher, announce=announce)

        @session.on("agent_state_changed")
        def _on_agent_state(event) -> None:
            _apply_agent_state(engagement, publisher, event.new_state)

        intents = _load_intents()

        async def _audio_turn(text: str) -> None:
            await _handle_audio_turn(
                text,
                intents=intents,
                brain=services.brain,
                session=session,
                publisher=publisher,
                engagement=engagement,
                addressing=addressing,
                sleep=_sleep,
                turn_ownership=turn_ownership,
            )

        agent.turn_handler = _audio_turn
        sleep_task = asyncio.create_task(_sleep_watch(
            engagement,
            _sleep,
            turn_ownership=turn_ownership,
        ))
        _BG_TASKS.add(sleep_task)
        sleep_task.add_done_callback(_BG_TASKS.discard)

    except BaseException:
        try:
            await asyncio.shield(_shutdown())
        finally:
            raise


def _console_output_args(
    argv: list[str],
    cfg: dict,
    resolve=wakeword.resolve_output_device,
) -> list[str]:
    if "console" not in argv or "--text" in argv or "--list-devices" in argv:
        return []
    if any(value == "--output-device" or value.startswith("--output-device=") for value in argv):
        return []
    configured = cfg.get("tts_output_device")
    if not configured or configured == FOLLOW_SENTINEL:
        return []
    index = resolve(configured)
    if index is None:
        logger.critical("configured TTS output device was not found")
        return []
    return ["--output-device", str(index)]


def _console_input_args(
    argv: list[str],
    cfg: dict,
    resolve=wakeword.resolve_input_device,
) -> list[str]:
    if "console" not in argv or "--text" in argv or "--list-devices" in argv:
        return []
    if any(value == "--input-device" or value.startswith("--input-device=") for value in argv):
        return []
    configured = cfg.get("wake_input_device")
    if not configured or configured == FOLLOW_SENTINEL:
        return []
    index = resolve(configured)
    if index is None:
        logger.critical("configured wake input device was not found")
        return []
    return ["--input-device", str(index)]


def main() -> int:
    global _worker_exit_code
    _worker_exit_code = 0
    jobobject.assign_current_process()
    try:
        cfg = _cfg()
        sys.argv.extend(_console_output_args(sys.argv, cfg))
        sys.argv.extend(_console_input_args(sys.argv, cfg))
    except Exception:
        logger.exception("could not resolve configured audio devices")
    cli.run_app(WorkerOptions(entrypoint_fnc=entrypoint))
    return _worker_exit_code


if __name__ == "__main__":
    sys.exit(main())
