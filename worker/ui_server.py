r"""Run the standalone Atlas local UI without microphone or voice services.

From ``atlas/`` run ``.venv\Scripts\python -m worker.ui_server``. The voice worker
normally serves the same UI; this entry point is useful while the voice stack is stopped.
"""
from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from urllib.parse import quote
import webbrowser

import aiohttp
import yaml

from worker import actionauth, runtime, state, stateserver
from worker.jobstore import JobStore
from worker.guided_setup import GuidedSetupAdmission
from worker.payload_codec import WindowsCurrentUserDPAPICodec
from worker.voice_runtime import DEFAULT_JOB_STORE, _expanded_store_path, job_events_projection
from worker.worker_health_file import DEFAULT_HEALTH_FILE, health_path, read_health

ATLAS = Path(__file__).resolve().parents[1]


def _open_pairing_window(url: str) -> bool:
    """Best-effort GUI bootstrap; the read-only loopback UI remains usable if it fails."""
    return bool(webbrowser.open(url, new=1))


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Atlas standalone loopback UI")
    parser.add_argument("--port", type=int, default=None,
                        help="loopback port (default: atlas.yaml state_port)")
    parser.add_argument("--no-browser", action="store_true",
                        help="serve the loopback UI without opening a browser window")
    parser.add_argument("--mirror-port", type=int, default=None,
                        help="mirror live voice state from another Atlas loopback port")
    return parser.parse_args()


async def serve(port: int | None = None, *, open_browser: bool = True,
                mirror_port: int | None = None) -> None:
    cfg = yaml.safe_load((ATLAS / "config" / "atlas.yaml").read_text(encoding="utf-8")) or {}
    authorizer = actionauth.PairingAuthorizer()
    services = runtime.build_runtime(
        ATLAS, cfg, action_context_provider=authorizer.active_context)
    publisher = state.StatePublisher(voice=cfg.get("active_voice"))
    store = JobStore(
        _expanded_store_path(str(cfg.get("job_store_path", DEFAULT_JOB_STORE))),
        payload_codec=WindowsCurrentUserDPAPICodec(),
    )
    worker_health = lambda: read_health(health_path(str(
        cfg.get("subscription_health_path", DEFAULT_HEALTH_FILE))))
    guided_setup = GuidedSetupAdmission(store, worker_health)
    serving_port = int(port if port is not None else cfg.get("state_port", 4360))
    if mirror_port is not None and (not 1 <= mirror_port <= 65535 or mirror_port == serving_port):
        store.close()
        raise ValueError("mirror port must be a different valid loopback port")
    mirror_session = (aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=0.8))
                      if mirror_port is not None else None)

    async def mirrored_state():
        if mirror_session is None or mirror_port is None:
            return publisher.snapshot()
        async with mirror_session.get(f"http://{stateserver.HOST}:{mirror_port}/state") as response:
            if response.status != 200:
                raise RuntimeError("voice state mirror is unavailable")
            body = await response.content.read(65_537)
            if len(body) > 65_536:
                raise RuntimeError("voice state mirror response is oversized")
            payload = json.loads(body.decode("utf-8"))
            if not isinstance(payload, dict):
                raise RuntimeError("voice state mirror response is invalid")
            return payload

    async def mirrored_signal():
        if mirror_session is None or mirror_port is None:
            return publisher.audio_energy
        async with mirror_session.get(f"http://{stateserver.HOST}:{mirror_port}/signal") as response:
            if response.status != 200:
                return 0.0
            body = await response.content.read(1_025)
            if len(body) > 1_024:
                return 0.0
            payload = json.loads(body.decode("utf-8"))
            return payload.get("energy", 0.0) if isinstance(payload, dict) else 0.0
    try:
        server = await stateserver.start(
            publisher, serving_port,
            state_provider=mirrored_state if mirror_port is not None else None,
            catalog_provider=services.catalog_projection, action_broker=services.actions,
            action_authorizer=authorizer,
            receipt_provider=(lambda: services.receipts.read_latest(100))
            if services.receipts is not None else None,
            job_provider=lambda: [{
                "id": job.job_id,
                "status": job.state.value,
                "lane": job.lane.value,
                "operation": job.request.operation,
                "updated_at": str(job.updated_at),
                **({"code": job.public_payload["code"]}
                   if isinstance(job.public_payload.get("code"), str) else {}),
                **({"summary": job.public_payload["summary"]}
                   if isinstance(job.public_payload.get("summary"), str) else {}),
                **({"result_available": True}
                   if job.public_payload.get("result_available") is True else {}),
            } for job in store.recent_jobs(50)],
            job_event_provider=lambda job_id: job_events_projection(store, job_id),
            result_provider=store.get_protected_result,
            health_provider=worker_health,
            signal_provider=mirrored_signal if mirror_port is not None else None,
            guided_setup_provider=guided_setup.start,
            surface_mode="mirror" if mirror_port is not None else "observer",
        )
    except Exception:
        if mirror_session is not None:
            await mirror_session.close()
        store.close()
        raise
    # The one-time secret travels only in a URL fragment, which browsers do not send to HTTP.
    # The UI consumes it immediately and removes it from the address bar/history entry.
    bootstrap_url = (f"http://{stateserver.HOST}:{server.port}/"
                     f"#pair={quote(authorizer.pairing_token, safe='')}")
    print(f"Atlas UI: http://{stateserver.HOST}:{server.port}/", flush=True)
    opened = await asyncio.to_thread(_open_pairing_window, bootstrap_url) if open_browser else False
    if open_browser and not opened:
        print("Browser launch failed; the read-only Atlas UI remains available at the URL above.",
              flush=True)
    try:
        await asyncio.Event().wait()
    finally:
        await server.stop()
        if mirror_session is not None:
            await mirror_session.close()
        store.close()


def main() -> None:
    args = _arguments()
    try:
        asyncio.run(serve(args.port, open_browser=not args.no_browser,
                          mirror_port=args.mirror_port))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
