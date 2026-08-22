# Atlas Revamp Implementation Plan

> **For agentic workers:** each task is executed by ONE worker in its own git worktree/branch.
> Do only your task. Read the spec first. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Restore the three-lane voice split (reflex → streaming Claude-with-tools → background
`claude --bg` work) so "pull up Gmail" fires instantly, reads happen in-turn through Daniel's MCP
servers, and long work is launched with a spoken ack and a live Workers tab.

**Architecture:** `brain.py` runs one streaming Anthropic call with a tool registry
(`tools.py`) whose tools are built-ins (open/focus/confirm/launch_work/…) plus tools mirrored from
Daniel's MCP servers (`mcp_client.py`). `work.py` owns background Claude Code sessions over a
trimmed `jobstore.py` + `claude_launcher.py`. The proposal/broker/doctrine layers are deleted.

**Tech Stack:** Python 3.13 (`C:\Users\danie\Atlas\.venv`), `anthropic` 1.0 (async streaming),
`mcp` 1.29 (stdio client, in-memory test pair), `livekit-agents` 1.6.6, aiohttp, SQLite + DPAPI,
vanilla JS UI.

**Spec:** `docs/specs/2026-08-22-atlas-revamp-design.md` — every interface below is defined
there; the spec wins on conflict.

## Global Constraints

- Run tests with `C:\Users\danie\Atlas\.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider`
  from the worktree root. The suite must stay account-free: no network, no browser, no `claude`
  process, no model call.
- Never read, print, copy, log, or commit credential values (`%USERPROFILE%\.atlas\env`,
  `~/.claude.json` `env` blocks, tokens). Key *names* may appear in code/config.
- No `claude -p/--print`, no Agent SDK, no API fallback for heavy work; only `claude --bg`.
- Change core logic; do not bolt on. No compatibility shims, aliases, re-exports, or
  "legacy" branches for code this plan deletes. Every removed module's imports, tests, config
  keys, docs and UI references go with it — leave no dead information.
- Keep files slim: one responsibility per module; bounded inputs validated at the boundary that
  receives untrusted data, not re-validated at every layer.
- Workers never commit. The boss reviews the diff and commits.
- Style: match the existing code (type hints, `from __future__ import annotations`, module
  docstring stating the module's one job, `__all__`). No narrating comments.

---

### Task 1: `tools.py` registry + built-in tools + `config/apps.yaml`

**Files:**
- Create: `worker/tools.py`, `config/apps.yaml`, `tests/test_tools.py`
- Modify: `worker/desktopapps.py` — add `code` (Visual Studio Code, publisher `Microsoft
  Corporation`, roots `%LOCALAPPDATA%\Programs\Microsoft VS Code\Code.exe` and
  `%ProgramFiles%\Microsoft VS Code\Code.exe`) and `wt` (Windows Terminal, publisher `Microsoft
  Corporation`, resolved via `%LOCALAPPDATA%\Microsoft\WindowsApps\wt.exe`) to `DEFAULT_PROFILES`
  the same way `chrome` is defined; add two thin functions `open_profile(app_id: str, url:
  str | None = None)` and `focus_profile(app_id: str)` that build a `DesktopApps` over
  `DEFAULT_PROFILES` with `native_launcher` and delegate to `open`/`focus`. Remove
  `TargetAlias`/`target_kinds`/alias plumbing only if nothing else in `desktopapps.py` needs it
  after Task 5 deletes `runtime.py` (leave a one-line note in your final message if you kept it).
- Test: `tests/test_desktopapps.py` — extend for the two new profiles and helper functions.

**Interfaces:**
- Produces: `Tool`, `ToolResult`, `ToolRegistry`, `PendingAction`, `load_apps(path) ->
  dict[str, AppEntry]`, `builtin(registry: ToolRegistry, apps: Mapping[str, AppEntry], work:
  "WorkLike") -> None` exactly as in spec §4. `WorkLike` is a `Protocol` with `launch(title,
  brief) -> Job`, `active() -> list[Job]`, `recent(n) -> list[Job]`, `cancel(job_id) -> Job`
  where `Job` exposes `.job_id .title .state .created_at` (Task 4 implements it; use a Protocol
  so this task does not import `work.py`). `opener: Callable[[str], None]` and
  `profile_opener`/`profile_focuser` are constructor-injected into `builtin` via keyword args
  defaulting to `os.startfile`, `desktopapps.open_profile`, `desktopapps.focus_profile`.

- [ ] **Step 1: Write failing tests** in `tests/test_tools.py` covering: register/duplicate,
  `schemas()` shape (`name`, `description`, `input_schema`), instant call ok/error/timeout
  (use an `async def` tool that sleeps past an injected `timeout_s=0.05`), confirm flow
  (`needs_confirmation` → `confirm` runs once → second `confirm` errors; expiry via injected
  clock; replacement of an older pending action), `cancel_pending`, `open` alias resolution
  (`"pull up gmail"` is NOT the input — the model passes `target="gmail"` or `"email"`; test
  `"Email"`, `"https://example.com/x"`, `"http://example.com"` → error, `"calc.exe"` → error,
  unknown → error), `open` exe profile path calls `profile_opener("code", None)`, `focus`,
  `launch_work` returns `{"job_id","status":"launching","title"}` via a fake `WorkLike`,
  `work_status`, `cancel_work`, and content bounding (a tool returning 10 000 chars →
  truncated with `…[truncated]`, control chars stripped).
- [ ] **Step 2: Run them; confirm they fail on import.**
- [ ] **Step 3: Implement `worker/tools.py` and `config/apps.yaml`** per spec §4. `ToolRegistry`
  takes `clock: Callable[[], float] = time.monotonic` and `timeout_s: float = 8.0`.
- [ ] **Step 4: Implement the `desktopapps.py` additions** and their tests. Authenticode
  verification must remain the only way an exe is accepted; tests inject a fake verifier
  exactly as the existing tests do.
- [ ] **Step 5: Run `tests/test_tools.py tests/test_desktopapps.py`; then the full suite.**
  Expected: all green (the rest of the tree is untouched by this task).
- [ ] **Step 6: `git diff --check`; report** the final line count of `tools.py` and any
  deviation from the spec with the reason.

---

### Task 2: `mcp_client.py` + `config/mcp.yaml`

**Files:**
- Create: `worker/mcp_client.py`, `config/mcp.yaml`, `tests/test_mcp_client.py`
- Read only: `worker/knowledge_mcp.py` (for the existing `mcp` 1.29 stdio usage pattern —
  do NOT import it; it is deleted in Task 5), `worker/tools.py` from Task 1 — if it is not yet
  on your branch, code against the `Tool`/`ToolRegistry` signatures in spec §4 and include a
  minimal fake registry in your tests.

**Interfaces:**
- Consumes: `Tool`, `ToolRegistry.register` (spec §4).
- Produces: `McpServers(config, *, claude_config_path=Path.home()/".claude.json",
  session_factory=None)`, `await connect(registry)`, `status() -> list[dict]`, `await close()`,
  `load_mcp_config(path) -> dict`, `policy_for(server_cfg, defaults, tool_name) -> Policy`.
  `session_factory(server_name, spec) -> AsyncContextManager[ClientSession]`; production
  default spawns `mcp.client.stdio.stdio_client(StdioServerParameters(command, args, env))` and
  enters `ClientSession` + `initialize()`.

- [ ] **Step 1: Write failing tests** using `mcp.server.fastmcp.FastMCP` with three tools
  (`get_events`, `send_gmail_message`, `search_drive_files`) and
  `mcp.shared.memory.create_connected_server_and_client_session` as the injected
  `session_factory`. Cover: tools registered as `google__get_events` etc. with the server's
  schema; policy from explicit `instant` list; policy from `instant_prefixes` default when no
  list; a call returns concatenated text content bounded to 4096 chars; a server whose factory
  raises is reported `connected: False, error: "<ExceptionClassName>"` and registers nothing;
  `from_claude_config` resolves `command/args/env` from a temp JSON file and the env values
  never appear in `status()` or logs (assert via `caplog`); `close()` is idempotent.
- [ ] **Step 2: Run; confirm failure.**
- [ ] **Step 3: Implement** per spec §5. Keep the module under 250 lines. Connection runs
  per-server concurrently with `connect_timeout_s`; one failure never blocks another.
- [ ] **Step 4: Write `config/mcp.yaml`** exactly as spec §5 (`google` from
  `google-workspace`).
- [ ] **Step 5: Run the new tests, then the full suite. `git diff --check`. Report.**

---

### Task 3: `brain.py`

**Files:**
- Create: `worker/brain.py`, `tests/test_brain.py`
- Read only: `worker/turn_interpreter.py` (being replaced — reuse its text-bounding and
  provider-error logging discipline, nothing else), `config/persona.md`.

**Interfaces:**
- Consumes: `ToolRegistry.schemas()`, `ToolRegistry.call(name, arguments) -> ToolResult`
  (spec §4). If Task 1 is not on your branch, write a 20-line fake registry in the tests.
- Produces: `Brain(client, registry, *, model, persona, google_account="", max_tokens=400,
  turn_timeout_s=12.0, history_exchanges=8, on_tool=None)`, `async def respond(transcript) ->
  AsyncIterator[str]`, `split_spoken(buffer: str) -> tuple[list[str], str]` (pure sentence
  chunker: returns complete chunks + remainder), `BASE_SYSTEM: str`.

- [ ] **Step 1: Build the fake client** in tests: `FakeStream` implementing
  `async with client.messages.stream(**kw) as s`, `s.text_stream` (async iterator of text
  deltas), `await s.get_final_message()` returning an object with `.content` (list of
  `SimpleNamespace(type="text", text=...)` / `SimpleNamespace(type="tool_use", id=..., name=...,
  input=...)`) and `.stop_reason`. The fake records every `kw` so tests assert the request
  shape.
- [ ] **Step 2: Write failing tests:** plain reply streams sentence chunks in order and
  history contains the exchange; `system` is a list with one block carrying
  `cache_control`; `tools` is present and the last tool carries `cache_control`; a `tool_use`
  final message triggers `registry.call`, then a second request whose messages end with the
  assistant tool_use content and a user `tool_result` block (with `is_error=True` for status
  error), and whose final text is yielded; text emitted before a tool call is yielded before
  the tool runs; the 5th round is sent with `tool_choice={"type":"none"}`; timeout → the
  fixed timeout sentence and nothing else; provider exception with `status_code=500` → the
  fixed provider sentence and a log line containing `status=500` and not the exception text;
  history trims to `history_exchanges`; transcript over 4096 chars → ValueError; `on_tool`
  invoked with `(name, ToolResult)`.
- [ ] **Step 3: Run; confirm failure.**
- [ ] **Step 4: Implement `brain.py`** per spec §6 including `BASE_SYSTEM` verbatim from the
  spec bullets (rewrite them as prose rules; include the `{google_account}` line only when
  non-empty). `split_spoken` yields on `. ? !` followed by space/end, on `\n`, or when the buffer
  exceeds 160 chars at the last space; a chunk shorter than 12 chars is held unless it is the
  final remainder.
- [ ] **Step 5: Run the new tests, then the full suite. `git diff --check`. Report** line count
  (target ≤ 300).

---

### Task 4: work lane — `jobstore.py` trim, `claude_launcher.py`, `work.py`

**Files:**
- Modify: `worker/jobstore.py` (rewrite to spec §7; keep the SQLite file layout idea and the
  `payload_codec` use; the schema may change freely — add a `schema_version` pragma and drop
  migration code for older layouts: a pre-revamp `jobs.sqlite3` is simply recreated if its
  version differs, after renaming the old file to `jobs.sqlite3.pre-revamp`).
- Create: `worker/claude_launcher.py` (from `worker/subscription_supervisor.py`), `worker/work.py`,
  `tests/test_claude_launcher.py`, `tests/test_work.py`
- Rewrite: `tests/test_jobstore.py`, `tests/test_no_heavy_api_path.py` (retarget at
  `claude_launcher.py` and `work.py`: assert no `-p`/`--print`, no `anthropic` import, no
  `claude_agent_sdk`, and that `launch` argv matches spec §7 exactly).
- Delete: `worker/subscription_supervisor.py`, `tests/test_subscription_supervisor.py`,
  `worker/subscription_worker.py`, `tests/test_subscription_worker.py`,
  `worker/worker_health_file.py`, `tests/test_worker_health_file.py`,
  `tests/test_protected_results.py`, `tests/test_heavy_loop.py`, `tests/test_agent_logic.py`,
  `tests/test_knowledge_mcp.py`, `tests/test_knowledge_workflow.py`, `tests/test_broker_ipc.py`,
  `worker/heavy_loop.py`, `worker/agent_logic.py`, `worker/knowledge_mcp.py`,
  `worker/knowledge_workflow.py`, `worker/broker_ipc.py`, `worker/subscription_cli.py`,
  `run-subscription-worker.js`. Other modules that still import `contracts.Job`/`JobState`
  (`frontdesk.py`, `stateserver.py`, `voice_runtime.py`, `app.py`) are NOT yours — Task 5
  rewires them; your branch's full suite will fail only in those files' tests, which is
  expected: run `pytest tests/test_jobstore.py tests/test_claude_launcher.py tests/test_work.py
  tests/test_no_heavy_api_path.py tests/test_payload_codec.py` as your gate and list the
  remaining failures by file in your report.

**Interfaces:**
- Produces: exactly spec §7 (`JobState`, `Job`, `JobEvent`, `JobStore`, `ClaudeLauncher`,
  `WorkManager`). `Job.to_public() -> dict` with keys `id,title,status,session_id,created_at,
  updated_at,summary,error` for the UI.

- [ ] **Step 1: Move, verbatim, from `subscription_supervisor.py` into `claude_launcher.py`:**
  `METERED_PROVIDER_ENV`, `_SENSITIVE_ENV`, `scrub_subscription_environment` (rename
  `scrubbed_environment`), `_BACKGROUND_ID`, `_ANSI_ESCAPE`, `_parse_background_session_id`,
  `_resolve_background_session`/`_background_sessions`/`named_sessions`, `inspect` (→ `status`
  returning the spec's literal strings), `logs` (→ returns `list[str]` after ANSI strip and
  byte-identical-redraw collapse, reuse the existing collapse logic), `stop` (→ `cancel`),
  `launch_connected` (→ `launch`, argv exactly per spec §7), `_connected_worker_prompt` (→
  `worker_prompt(job_id, nonce, brief)`), `parse_worker_result`/`_is_worker_result_template`
  (→ `parse_result(logs: list[str], *, nonce, job_id) -> str | None` returning the summary
  text). Drop everything else (agentic/safe-mode/review launches, profiles, brokers,
  `SubscriptionAuthorization`, `ActiveRun`, `SubscriptionSupervisor`).
- [ ] **Step 2: Write failing tests for `claude_launcher.py`** with a recording fake runner:
  argv exactness, session-id parse from `backgrounded · <id>` stdout and from the fallback
  session listing, `status` mapping, `logs` ANSI/redraw collapse, `parse_result` accepts the
  frame and rejects wrong job id / wrong nonce / template echo, env scrub removes every
  `METERED_PROVIDER_ENV` key and secret-shaped names.
- [ ] **Step 3: Rewrite `jobstore.py` + tests** per spec §7. Output lines are plain text
  (bounded); `brief` and `result` go through the injected `PayloadCodec`
  (`WindowsCurrentUserDPAPICodec` in production, the existing test codec in tests).
- [ ] **Step 4: Write failing tests for `work.py`:** `launch` returns a QUEUED job within 100 ms
  even when the fake launcher sleeps 1 s; thread transitions LAUNCHING → RUNNING with the
  session id; launcher exception → FAILED with `error="launch_failed"`; `run()` polls: new log
  lines become output events exactly once; `status="done"` + result frame → SUCCEEDED with
  summary and `result()`; `done` without a frame → FAILED `result_missing`; `needs_input` →
  FAILED `needs_input`; `cancel` → launcher.cancel called, CANCELLED; `on_terminal` fires once
  per job; restart re-attaches RUNNING rows and fails rows without a session id as
  `orphaned`.
- [ ] **Step 5: Implement `work.py`** per spec §7 (≤ 250 lines). Job directories:
  `workspace_root / job_id`, created on the launch thread.
- [ ] **Step 6: Run the gate tests; `git diff --check`; report** files deleted, remaining
  failing test files (expected: frontdesk/stateserver/voice_*/app-related only), line counts.

---

### Task 5: integration — `app.py`, `chat.py`, state server, UI, deletions, docs

**Files:**
- Modify: `worker/app.py` (spec §8), `worker/stateserver.py` (spec §9), `worker/ui_server.py`,
  `worker/state.py` (drop `add_filed_card`/`update_filed_card`/confirmed-action state if
  unused after the rewire), `ui/index.html`, `ui/app.js`, `ui/styles.css` (spec §9),
  `pm2.config.cjs` (single `atlas-worker` app), `config/atlas.yaml` (remove
  `local_file_roots`, `desktop_target_aliases`, `browser_*`, `google_broker_endpoint`,
  `receipt_journal_path`, `agentic_workspace_path`, `subscription_health_path`,
  `interpreter_*`; add `google_account: daniel.zhang.t1@gmail.com`, `work_workspace_path:
  "%LOCALAPPDATA%/Atlas/jobs"`, `turn_timeout_s: 12.0`, `max_tokens: 400`), `config/persona.md`
  (remove the "work-routing tool"/"backend catalog" paragraphs; the persona is voice and
  character only — behaviour rules live in `brain.BASE_SYSTEM`), `README.md`, `CLAUDE.md`,
  `requirements.txt` (nothing new; drop comments referencing deleted modules).
- Create: `worker/chat.py` (spec §8: `python -m worker.chat "<utterance>"` → builds the same
  registry/MCP/work/brain as `app.py` via a shared `worker/runtime.py` *rewritten* as the
  composition root `build(cfg) -> Runtime(registry, mcp, work, brain, store)`; prints chunks as
  they stream and `tool: <name> <status>` lines; `--no-mcp` flag skips MCP connect),
  `tests/test_runtime.py` (rewrite: composition builds with fakes, no network),
  `tests/test_app_turns.py` (reflex dismiss/cancel/repeat, streaming tee into `session.say`,
  transcript lines, completion callback lines).
- Delete: `worker/turn_interpreter.py`, `worker/voice_frontdesk.py`, `worker/voice_runtime.py`,
  `worker/frontdesk.py`, `worker/routing_policy.py`, `worker/contracts.py`,
  `worker/capabilities.py`, `worker/capability_runner.py`, `worker/connectors.py`,
  `worker/browser_protocol.py`, `worker/browser_transport.py`, `browser_bridge/`,
  `worker/localfiles.py`, `worker/guided_setup.py`, `worker/actionauth.py` (move the
  pairing bearer logic `HEADER`/`PairingAuthorizer` into `stateserver.py` if `/pair` needs it —
  it is ~70 lines — otherwise delete), `worker/actionbroker.py`, `worker/receipts.py`,
  `config/capabilities.yaml`, `docs/plans/2026-08-21-*.md`, `handoffs/2026-08-21-*.md`, and
  every test for a deleted module (`test_turn_interpreter`, `test_voice_frontdesk`,
  `test_frontdesk`, `test_routing_policy`, `test_capabilities`, `test_capability_runner`,
  `test_connectors`, `test_browser_protocol`, `test_localfiles`, `test_guided_setup`,
  `test_actionauth`, `test_actionbroker`, `test_receipts`, `test_router` if redundant with
  `test_reflex`, `test_voice_production_cutover` → rewrite as a small production-path scan
  asserting `app.py` imports `brain`/`work`/`tools`/`mcp_client` and none of the deleted
  modules exist under `worker/`).

**Interfaces:**
- Consumes: Tasks 1–4 exactly as specified.
- Produces: `runtime.build(cfg, *, client=None, launcher=None, session_factory=None) ->
  Runtime`; `stateserver` job projections from `Job.to_public()` and `JobStore.events(after)`.

- [ ] **Step 1: Write failing tests** (`test_app_turns.py`, `test_runtime.py`,
  `test_stateserver.py` adjustments for the new/removed routes, `test_ui_server.py`).
- [ ] **Step 2: Rewire `app.py`** per spec §8. `_submit_voice_turn` streams
  `brain.respond` into `session.say`. Reflex intents in `config/intents.yaml`: keep `dismiss`,
  add `cancel` (`phrases: [cancel, never mind, stop]`) and `repeat` (`phrases: [repeat that,
  say that again]`). MCP connect and `WorkManager.run` are background tasks started in
  `entrypoint`; `/health` reports `{"claude": launcher.available, "mcp": mcp.status()}`.
- [ ] **Step 3: Trim `stateserver.py` + UI** per spec §9; Workers tab polls
  `/jobs/{id}/events?after=<last>` every 1 s while a job is active; History shows
  `summary` and fetches `/jobs/{id}/result` with the paired bearer on click.
- [ ] **Step 4: Apply the deletions and config/doc rewrites.** `CLAUDE.md` becomes the new
  constitution: keep rules 1 (credentials), 2 (no heavy API path; `claude --bg` only), 3
  (models propose, host executes through the registry), 6→"MCP child environments come from
  `~/.claude.json`; never logged or served", 7→"`open` accepts aliases and https URLs only;
  executables only from signed profiles", 8 (tests + `git diff --check` + adversarial review
  before declaring done); replace 4–5 with the spec §2 authority model (instant/confirm/launch
  policies; readback + `confirm` turn for mutations). `README.md`: what Atlas is in five lines,
  setup, `python -m worker.app console`, `python -m worker.chat "..."`, `python -m
  worker.ui_server`, config files table (`atlas.yaml`, `apps.yaml`, `mcp.yaml`,
  `intents.yaml`, `persona.md`), verification section.
- [ ] **Step 5: Full suite green, `node --check ui/app.js`, `git diff --check`,
  `python -m compileall -q worker`, `wc -l worker/*.py` ≤ 4,500 total. Report** the deletion
  list and line totals.

---

### Task 6: adversarial review + live verification (boss + reviewers)

- [ ] Codex deep read-only review of the merged branch against spec §10/§11: prompt-injection
  via MCP content, `open` URL/exe boundary, confirm replay/expiry, env leakage in logs/routes,
  work-lane orphan/cancel races, streaming cancellation on barge-in, deleted-module residue.
- [ ] Claude (sonnet) independent verification: run the suite, grep for dead references to
  deleted modules/config keys, confirm line budget, confirm no credential strings in the diff.
- [ ] Boss live smoke on this machine: `python -m worker.chat "pull up gmail"`, `"what's on my
  calendar today"`, `"go to sleep"` (reflex path via `worker.app` unit test only), and one
  `launch_work` brief with a trivial task; record timings in the handoff.
- [ ] Handoff `handoffs/2026-08-22-atlas-revamp.md`: what changed, what was verified, the one
  remaining human gate (PM2 cutover + a spoken acceptance round), exact commands.
