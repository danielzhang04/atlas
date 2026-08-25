## Findings

**BLOCKER — startup order defeats the lazy-start composition.**  
[worker/app.py](/C:/Users/danie/Atlas-worktrees/streamline/worker/app.py:486) schedules MCP/work before the first await, then connects and starts LiveKit at lines 492–509; the state server does not start until [line 535](/C:/Users/danie/Atlas-worktrees/streamline/worker/app.py:535), followed by `ATLAS_UI` and warm-up at lines 548–549.

Actual order:

`runtime.build → schedule MCP/work → LiveKit connect/session → state server → ATLAS_UI → warm`

Required order:

`runtime.build → state server → ATLAS_UI → warm → schedule MCP → LiveKit session`

Consequences:

- The deferred `mcp` import can still execute before `/state`, undermining U1’s startup improvement.
- MCP, LiveKit connection, STT/VAD, or session initialization can delay or prevent the desktop from receiving `ATLAS_UI`.
- The new test only records `build → server → UI → warm`, so it misses the earlier MCP and LiveKit operations.

Minimal fix: construct the publisher and start the state server first, emit `ATLAS_UI`, warm, then schedule MCP/work and initialize LiveKit. Make shutdown tolerate a not-yet-created session/task, and extend the test to record the complete order.

**LOW — dead redaction parameter carries a secret without doing anything.**  
[worker/desktop.py](/C:/Users/danie/Atlas-worktrees/streamline/worker/desktop.py:403) accepts `redactions=()` but never reads it, while [line 464](/C:/Users/danie/Atlas-worktrees/streamline/worker/desktop.py:464) passes the shutdown token into that unused argument. It does not currently leak, but implies nonexistent redaction behavior and is dead composition surface. Remove the parameter and token argument.

## Seam results

| Seam | Result |
|---|---|
| `Runtime.warm_model_client()` → app | Signature matches; no arguments; starts one daemon warm thread; provider errors are type-only logs. Call is after server/UI, but MCP has already started. |
| `kill_process_tree()` → desktop/MCP | Matches. Desktop explicitly uses `force=False`, then `True`; MCP relies on the correct `True` default. Desktop suppresses launch errors; MCP catches all cleanup errors. |
| `devicewatch.start_audio_follow()` → app | Matches required keyword-only `request_restart`; coalescer is thread-safe and watcher callbacks preserve the previous threading model. |
| `requestJson()` → `/health` | Matches `{claude, mcp}` from the state server; `/mcp` is removed; HTTP and JSON failures are handled. |
| `router` → brain/app | Matches. `Addressing`, vocabulary, normalization, and reflex routing are consolidated; no `worker.addressing` references remain. |

Security rules 1–11 otherwise hold in the merged tree: the `js_api` exposes only the three reviewed methods, persistent logs remain bounded and host-shaped, pairing removes the fragment before exchange and keeps only the bearer in memory/session storage, authenticated 401s clear pairing, `/health` sanitizes MCP status, host confirmation and the typed registry remain intact, and direct `open` remains HTTPS-only.

## Verification and benchmarks

- Import trace: `worker.app` cumulative **6.088086 s**, below the recorded 6.444 s reference. Top-level `anthropic` and `mcp` were absent. LiveKit still imports its internal `livekit.agents.llm._provider_format.anthropic` module.
- Test collection: **408 tests**, above the ≥385 target.
- Full suite: **not executed**. The authorized attempt failed before collection because the read-only sandbox offered no writable temporary directory.
- `node --check ui/app.js`: pass.
- `git diff --check a720ab9..HEAD`: pass.
- Production LOC: **9,106 → 8,992**, net **−114**.
- Worker modules: **24 → 23**.
- `desktop.py`: **582 lines**, exactly at the ceiling.
- UI arithmetic matches the implementation: visible Live is 690 base requests/min plus 30 per active job; hidden is 24/min, 88.2% below the former 204/min baseline.
- Deleted addressing assertions were rehomed in `test_reflex.py`; no governance test was deleted or materially weakened.

## Verdict

**REWORK**

Fix the startup ordering and run the full 408-test suite in a writable environment before reconsidering shipment.

A fresh worker should treat `desktop.py` as the window/process/logging owner, `jobobject.py` as process containment and tree termination, `app.py` as composition and voice lifecycle only, `runtime.py` as service construction and model warming, `stateserver.py` as loopback serving/pairing/public projections, `devicewatch.py` as all audio-device following, `wakeword.py` as capture/inference, `router.py` as normalization/reflex/addressing, `brain.py` as model-turn orchestration, `tools.py` as the typed policy/confirmation boundary, `mcp_client.py` as lazy MCP transport, and `ui/app.js` as request, polling, and rendering ownership.

--- codex-dispatch card 6a8e1e2d-0a232376 | model gpt-5.6-sol | exit 0 | 761s | ops publish: pushed | log: C:\Users\danie\AppData\Local\kb-codex-dispatch\logs\6a8e1b3c-c6c3efda.jsonl | session 01a03b1a-7ab1-79f3-9637-6b8b56278292 (follow up with --follow-up 01a03b1a-7ab1-79f3-9637-6b8b56278292)
