"""Run the standalone Atlas LiveKit voice worker."""
from __future__ import annotations

import asyncio
import inspect
import logging
import os
from pathlib import Path
import sys
import threading
from typing import Any

import yaml

from livekit.agents import Agent, AgentSession, JobContext, StopResponse, WorkerOptions, cli
from livekit.plugins import deepgram, elevenlabs, silero

from worker import brain as brain_mod
from worker import devicewatch
from worker import engagement as engagement_mod
from worker import envload, mcp_client as mcp_client_mod, router, runtime, sanitize, state, stateserver
from worker import tools as tools_mod
from worker import wakeword, work as work_mod
from worker.jobstore import JobState

__all__ = ["AtlasAgent", "entrypoint", "main"]

ATLAS = Path(__file__).resolve().parents[1]
logger = logging.getLogger("atlas.app")

TTS_VOICE = "aura-2-andromeda-en"
FOLLOW_SENTINEL = "follow"
TEXT_MODE = "--text" in sys.argv
DEFAULT_DISMISS = ["that's all", "go to sleep"]
WAKE_LINE = "Hey boss. What can I do for you?"
SLEEP_LINE = "Okay, sleeping. Wake me when you need something."
_BG_TASKS: set[asyncio.Task] = set()


def _build_tts(cfg: dict):
    entry = (cfg.get("voices") or {}).get(cfg.get("active_voice") or "")
    if not entry:
        return deepgram.TTS(model=TTS_VOICE)
    if entry["vendor"] == "deepgram":
        return deepgram.TTS(model=entry["model"])
    if entry["vendor"] == "elevenlabs":
        from livekit.agents.utils import http_context

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


def _apply_agent_state(engagement, publisher, agent_state: str) -> None:
    if engagement.state != engagement_mod.ENGAGED:
        return
    mapped = state.STATE_FROM_AGENT.get(agent_state)
    if mapped is not None:
        publisher.set_state(mapped)


def _boot_default_output_name() -> str | None:
    try:
        import sounddevice as sd

        return sd.query_devices(sd.default.device[1])["name"]
    except Exception:
        return None


def _output_device_status(
    cfg: dict,
    resolve=wakeword.resolve_output_device,
    boot_default=_boot_default_output_name,
) -> dict:
    configured = cfg.get("tts_output_device")
    if not configured:
        return {"configured": None, "resolved": None, "following": False}
    if configured == FOLLOW_SENTINEL:
        return {"configured": FOLLOW_SENTINEL, "resolved": boot_default(), "following": True}
    index = resolve(configured)
    if index is None:
        return {"configured": configured, "resolved": None, "following": False}
    try:
        import sounddevice as sd

        name = sd.query_devices(index)["name"]
    except Exception:
        name = str(index)
    return {"configured": configured, "resolved": name, "following": False}


def _console_singleton():
    from livekit.agents.cli._legacy import AgentsConsole

    return AgentsConsole.get_instance()


def _restart_worker(reason: str) -> None:
    logger.critical("output-follow requested worker restart: %s", reason)
    os._exit(21)


def _start_output_follow(
    cfg: dict,
    publisher,
    *,
    console_factory=_console_singleton,
    watcher_cls=devicewatch.DeviceWatcher,
    follower_cls=devicewatch.OutputFollower,
    probe=devicewatch.current_default_output,
):
    if cfg.get("tts_output_device") != FOLLOW_SENTINEL:
        return None
    try:
        probe_ok = probe() is not None
    except Exception:
        probe_ok = False
    if not probe_ok:
        logger.critical("output-follow probe is unavailable")
        publisher.set_output_device({
            "configured": FOLLOW_SENTINEL,
            "resolved": _boot_default_output_name(),
            "following": False,
        })
        return None
    try:
        console = console_factory()
    except Exception:
        logger.critical("output-follow console is unavailable", exc_info=True)
        publisher.set_output_device({
            "configured": FOLLOW_SENTINEL,
            "resolved": _boot_default_output_name(),
            "following": False,
        })
        return None
    try:
        import sounddevice as sd

        try:
            boot_index = sd.default.device[1]
        except Exception:
            boot_index = None
        follower = follower_cls(
            console,
            resolve_output=wakeword.resolve_output_device,
            sd_module=sd,
            initial_idx=boot_index,
            request_restart=_restart_worker,
        )

        def _on_change(name: str) -> None:
            publisher.set_output_device(follower.swap_to(name))

        watcher = watcher_cls(
            probe=probe,
            on_change=_on_change,
            period_s=1.5,
            initial_delay_s=10.0,
        )
        watcher.start()
        return watcher
    except Exception:
        logger.critical("output-follow wiring failed", exc_info=True)
        publisher.set_output_device({
            "configured": FOLLOW_SENTINEL,
            "resolved": _boot_default_output_name(),
            "following": False,
        })
        return None


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


async def _submit_voice_turn(
    text: str,
    *,
    brain: brain_mod.Brain,
    session,
    publisher: state.StatePublisher,
) -> str:
    if not isinstance(text, str) or not text.strip():
        return ""
    publisher.add_line("user", text)
    publisher.set_state(state.THINKING)
    spoken: list[str] = []

    async def _tee():
        async for chunk in brain.respond(text):
            spoken.append(chunk)
            yield chunk

    try:
        await session.say(_tee(), add_to_chat_ctx=False)
    finally:
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


async def _handle_reflex(
    text: str,
    *,
    intents: dict,
    session,
    publisher: state.StatePublisher,
    dismiss,
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
    elif intent == "repeat" and repeated:
        await session.say(repeated, add_to_chat_ctx=False)
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


def _announce_terminal(job, publisher, session, engagement) -> None:
    line = _terminal_line(job)
    publisher.add_line("atlas", line)
    if engagement.state != engagement_mod.ENGAGED:
        return
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


def _health(mcp: mcp_client_mod.McpServers, launcher) -> dict:
    return {"claude": launcher.available, "mcp": mcp.status()}


async def entrypoint(ctx: JobContext) -> None:
    envload.load_private_environment()
    cfg = _cfg()
    authorizer = stateserver.PairingAuthorizer()
    server_box: dict[str, stateserver.StateServer] = {}

    def _paired_url() -> str | None:
        server = server_box.get("server")
        if server is None:
            return None
        return stateserver.pairing_url(authorizer, server.port)

    services = runtime.build(cfg, paired_url=_paired_url)
    publisher = state.StatePublisher(voice=cfg.get("active_voice"))
    engagement = engagement_mod.Engagement()
    publisher.set_output_device(_output_device_status(cfg))

    services.brain.on_tool = lambda name, result: _record_tool(publisher, name, result)
    loop = asyncio.get_running_loop()
    session_box: dict[str, Any] = {}
    pending_terminal = []

    def _deliver_terminal(job) -> None:
        current_session = session_box.get("session")
        if current_session is None:
            pending_terminal.append(job)
            return
        _announce_terminal(job, publisher, current_session, engagement)

    services.work.on_terminal(
        lambda job: loop.call_soon_threadsafe(_deliver_terminal, job)
    )
    stop_work = asyncio.Event()
    mcp_task = asyncio.create_task(services.mcp.connect(services.registry))
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
    )
    agent = AtlasAgent(
        instructions="Atlas voice I/O is host controlled.",
        llm=None,
        tools=[],
    )
    await session.start(agent=agent, room=ctx.room)
    session_box["session"] = session
    for terminal_job in pending_terminal:
        _announce_terminal(terminal_job, publisher, session, engagement)
    pending_terminal.clear()

    async def _submit(text: str) -> None:
        await _submit_voice_turn(
            text,
            brain=services.brain,
            session=session,
            publisher=publisher,
        )

    agent.turn_handler = _submit

    server = await stateserver.start(
        publisher,
        int(cfg["state_port"]),
        authorizer=authorizer,
        job_provider=lambda: _jobs_projection(services.work),
        job_event_provider=services.store.events,
        result_provider=services.store.result,
        cancel_provider=services.work.cancel,
        mcp_provider=services.mcp.status,
        health_provider=lambda: _health(services.mcp, services.work.launcher),
    )
    server_box["server"] = server

    async def _shutdown() -> None:
        stop_work.set()
        await asyncio.gather(work_task, return_exceptions=True)
        await asyncio.gather(mcp_task, return_exceptions=True)
        await services.mcp.close()
        await server.stop()
        services.store.close()

    ctx.add_shutdown_callback(_shutdown)
    if TEXT_MODE:
        return

    _start_output_follow(cfg, publisher)
    session.input.set_audio_enabled(False)

    def _sleep(announce: bool = True) -> bool:
        if not session.input.audio_enabled:
            return False
        session.input.set_audio_enabled(False)
        publisher.set_state(state.ASLEEP)
        if announce:
            session.say(SLEEP_LINE, add_to_chat_ctx=False)
            publisher.add_line("atlas", SLEEP_LINE)
        return True

    def _engage() -> None:
        already = engagement.state == engagement_mod.ENGAGED
        engagement.wake()
        session.input.set_audio_enabled(True)
        if not already:
            publisher.start_session()
            publisher.set_state(state.LISTENING)
            session.say(WAKE_LINE, add_to_chat_ctx=False)
            publisher.add_line("atlas", WAKE_LINE)

    @session.on("agent_state_changed")
    def _on_agent_state(event) -> None:
        _apply_agent_state(engagement, publisher, event.new_state)

    def _on_wake() -> None:
        loop.call_soon_threadsafe(_engage)

    def _on_audio_energy(value: float) -> None:
        loop.call_soon_threadsafe(publisher.set_audio_energy, value)

    intents = _load_intents()

    async def _handle_audio_turn(text: str) -> None:
        handled = await _handle_reflex(
            text,
            intents=intents,
            session=session,
            publisher=publisher,
            dismiss=lambda: (engagement.dismiss(), _sleep()),
        )
        if not handled:
            await _submit(text)

    agent.turn_handler = _handle_audio_turn

    async def _quiet_shutdown() -> None:
        wakeword.shutting_down.set()

    ctx.add_shutdown_callback(_quiet_shutdown)
    threading.Thread(
        target=wakeword.listen,
        args=(_on_wake, cfg["wake_model"]),
        kwargs={
            "device": cfg.get("wake_input_device"),
            "threshold": cfg.get("wake_threshold", wakeword.THRESHOLD),
            "patience": cfg.get("wake_patience", wakeword.PATIENCE),
            "on_energy": _on_audio_energy,
        },
        daemon=True,
    ).start()


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


def main() -> int:
    try:
        sys.argv.extend(_console_output_args(sys.argv, _cfg()))
    except Exception:
        logger.exception("could not resolve tts_output_device")
    if "console" in sys.argv and "--input-device" not in sys.argv:
        index = wakeword.resolve_input_device(_cfg().get("wake_input_device"))
        if index is not None:
            sys.argv += ["--input-device", str(index)]
    cli.run_app(WorkerOptions(entrypoint_fnc=entrypoint))
    return 0


if __name__ == "__main__":
    sys.exit(main())
