"""Process-lifetime-authorized standalone worker for subscription Claude Code sessions.

The explicit startup flag is the human activation boundary. The process never reads Atlas's API-key
environment file and refuses to start when metered-provider credentials are inherited.
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import sys
import time

import yaml

from .jobstore import JobStore
from .capability_runner import SharedCapabilityBroker
from .payload_codec import WindowsCurrentUserDPAPICodec
from .runtime import RuntimeServices, build_runtime
from .subscription_supervisor import (
    AgenticRuntimeConfig,
    ClaudeBackgroundTransport,
    LocalCommandRunner,
    METERED_PROVIDER_ENV,
    SubscriptionAuthorization,
    SubscriptionSupervisor,
    SupervisorError,
)
from .contracts import JobState
from .subscription_worker import WorkerHealth, WorkerHealthStatus
from .voice_runtime import DEFAULT_JOB_STORE
from .worker_health_file import DEFAULT_HEALTH_FILE, health_path, publish_health


ATLAS = Path(__file__).resolve().parents[1]


def _available_knowledge_capabilities(services: RuntimeServices) -> frozenset[str]:
    if not isinstance(services, RuntimeServices):
        raise TypeError("knowledge capability projection requires RuntimeServices")
    available = set()
    if services.browser is not None:
        available.add("browser.inspect")
    if services.google is not None:
        available.update({
            "google.drive.list", "google.drive.read", "google.docs.read",
            "google.gmail.read", "google.calendar.read",
        })
    return frozenset(available)


def _arguments(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Atlas subscription-only background worker")
    parser.add_argument(
        "--confirm-subscription-auth", action="store_true",
        help="attest that the local claude CLI is signed in through a Claude subscription",
    )
    parser.add_argument("--workdir", default=str(ATLAS),
                        help="restricted local workspace for subscription jobs")
    parser.add_argument("--job-store", default=None,
                        help="override the encrypted SQLite store path")
    parser.add_argument("--health-file", default=None,
                        help="override the worker health-file path")
    parser.add_argument("--agentic-workspace", default=None,
                        help="override the isolated agent-workspace root")
    parser.add_argument("--connected-workspace", default=None,
                        help="override the connected Claude task-workspace root")
    parser.add_argument("--poll-seconds", type=float, default=2.0)
    return parser.parse_args(argv)


def _api_environment_absent(environment=os.environ) -> bool:
    normalized = {str(key).upper(): value for key, value in environment.items()}
    return not any(str(normalized.get(key, "")).strip() for key in METERED_PROVIDER_ENV)


def run(argv=None) -> int:
    args = _arguments(argv)
    if not args.confirm_subscription_auth:
        print("Refusing to start: --confirm-subscription-auth is required.", flush=True)
        return 2
    if not _api_environment_absent():
        print("Refusing to start: a metered API credential is present in the environment.", flush=True)
        return 2
    if not 0.25 <= args.poll_seconds <= 30:
        print("Refusing to start: --poll-seconds must be between 0.25 and 30.", flush=True)
        return 2

    cfg = yaml.safe_load((ATLAS / "config" / "atlas.yaml").read_text(encoding="utf-8")) or {}
    store_path = Path(os.path.expandvars(os.path.expanduser(str(
        args.job_store or cfg.get("job_store_path", DEFAULT_JOB_STORE),
    )))).resolve()
    heartbeat = health_path(str(
        args.health_file or cfg.get("subscription_health_path", DEFAULT_HEALTH_FILE),
    ))
    workdir = Path(args.workdir).resolve(strict=True)
    runtime_services = build_runtime(ATLAS, cfg)
    knowledge_capabilities = _available_knowledge_capabilities(runtime_services)
    workspace_root = Path(os.path.expandvars(os.path.expanduser(str(
        args.agentic_workspace
        or cfg.get("agentic_workspace_path", "%LOCALAPPDATA%/Atlas/agent-jobs"),
    )))).resolve()
    workspace_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    connected_workspace_root = Path(os.path.expandvars(os.path.expanduser(str(
        args.connected_workspace
        or cfg.get("connected_workspace_path", "%LOCALAPPDATA%/Atlas/connected-jobs"),
    )))).resolve()
    connected_workspace_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    agentic_runtime = AgenticRuntimeConfig(
        SharedCapabilityBroker(runtime_services), knowledge_capabilities,
        Path(sys.executable), ATLAS, workspace_root,
    )
    checked_at = time.time()
    authorization = SubscriptionAuthorization(
        "claude-subscription", checked_at=checked_at, api_environment_absent=True,
        human_confirmed=True,
    )
    publish_health(heartbeat, WorkerHealth(
        WorkerHealthStatus.UNAVAILABLE, "worker_starting",
        worker_id="atlas-subscription", checked_at=checked_at))
    with JobStore(store_path, payload_codec=WindowsCurrentUserDPAPICodec()) as store:
        supervisor = SubscriptionSupervisor(
            store,
            ClaudeBackgroundTransport(LocalCommandRunner(), environment=os.environ),
            workdir=workdir,
            authorization=authorization,
            agentic_runtime=agentic_runtime,
            connected_workspace_root=connected_workspace_root,
        )
        active = None
        supervisor.reconcile_after_restart()
        try:
            while True:
                now = time.time()
                store.recover_orphans()
                reconciling = bool(store.claimed_jobs(supervisor.worker_id)) and active is None
                status = (WorkerHealthStatus.AVAILABLE
                          if not reconciling else WorkerHealthStatus.DEGRADED)
                reason = "restart_reconciliation_wait" if reconciling else ""
                publish_health(heartbeat, WorkerHealth(
                    status, reason, worker_id=supervisor.worker_id, checked_at=now))
                if active is not None:
                    try:
                        if supervisor.poll(active) is not JobState.RUNNING:
                            active = None
                    except SupervisorError:
                        active = None
                        publish_health(heartbeat, WorkerHealth(
                            WorkerHealthStatus.DEGRADED, "subscription_worker_recovered",
                            worker_id=supervisor.worker_id, checked_at=time.time()))
                        print("Subscription worker recovered from a supervisor error.", flush=True)
                elif not reconciling:
                    try:
                        active = supervisor.start_next()
                    except SupervisorError:
                        active = None
                        publish_health(heartbeat, WorkerHealth(
                            WorkerHealthStatus.DEGRADED, "subscription_worker_recovered",
                            worker_id=supervisor.worker_id, checked_at=time.time()))
                        print("Subscription worker recovered from a supervisor error.", flush=True)
                time.sleep(args.poll_seconds)
        except KeyboardInterrupt:
            if active is not None:
                try:
                    store.request_cancel(active.claim.job_id)
                    supervisor.poll(active)
                except Exception:
                    pass
            return 0
        finally:
            publish_health(heartbeat, WorkerHealth(
                WorkerHealthStatus.UNAVAILABLE, "worker_stopped",
                worker_id=supervisor.worker_id, checked_at=time.time()))


def main() -> None:
    raise SystemExit(run())


if __name__ == "__main__":
    main()
