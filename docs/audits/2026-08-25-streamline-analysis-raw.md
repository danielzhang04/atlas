## 1. **Measured baseline**

Read-only analysis against commit `a720ab9` (`fix(desktop): keep the pywebview Window off WindowApi's public surface`). No files were changed.

### Import profile

The successful PowerShell-equivalent cumulative sort reported 6.444 seconds inside `worker.app` and 8.34 seconds wall time:

| Module | Self ms | Cumulative ms |
|---|---:|---:|
| `openai.types.responses.response_input_text` | 0.153 | 693.448 |
| `openai.types.graders.grader_inputs` | 2.836 | 696.283 |
| `openai.types.graders.label_model_grader` | 4.278 | 700.561 |
| `openai.types.graders.multi_grader` | 2.232 | 715.590 |
| `openai.types.graders` | 1.296 | 727.102 |
| `openai.types.graders.grader_inputs_param` | 0.057 | 727.159 |
| `openai.types.eval_create_params` | 2.906 | 751.382 |
| `mcp` | 2.633 | 956.975 |
| `worker.mcp_client` | 2.676 | 959.651 |
| `livekit.agents.telemetry` | 1.531 | 972.873 |
| `openai.types.beta` | 3.289 | 984.465 |
| `openai.lib.streaming._assistants` | 2.078 | 986.542 |
| `openai.lib.streaming` | 1.217 | 987.759 |
| `openai.types` | 4.472 | 1,150.704 |
| `livekit.agents.job` | 4.935 | 1,834.784 |
| `openai` | 2.700 | 2,219.160 |
| `livekit.agents.inference.llm` | 4.480 | 2,417.369 |
| `livekit.agents.inference` | 1.556 | 2,538.644 |
| `livekit.agents.voice.agent` | 4.350 | 2,653.570 |
| `livekit.agents.voice.io` | 11.398 | 2,793.069 |
| `livekit.agents.voice` | 1.794 | 2,961.487 |
| `livekit.agents.cli.cli` | 1.846 | 4,889.680 |
| `livekit.agents.cli` | 1.413 | 4,891.092 |
| `livekit.agents` | 2.129 | 4,911.444 |
| `worker.app` | 4.375 | 6,443.824 |

An earlier direct capture reached 14.763 seconds cumulative and exited nonzero without a retained non-import traceback. This is a noisy single-run baseline; future work should compare five fresh-process medians on the same machine.

Startup-path interpretation:

- `livekit.agents`, its CLI, voice stack, and transitive `openai` package are eager via `worker/app.py:15–16`. LiveKit is essential, but the transitive OpenAI import is unused because both session and agent pass `llm=None` (`worker/app.py:751–761`).
- MCP’s external package is eager via `worker/mcp_client.py:16–17`, despite MCP connection being scheduled as background work at `worker/app.py:740–744`. It can be imported inside connection/session setup to improve time to `/state`; eventual idle memory is unchanged once connected.
- Anthropic is absent from the import-only trace, but `_LazyAnthropicClient.__init__` starts a warm-up thread (`worker/runtime.py:25–57`) during `runtime.build()` before `/state`. Thus the actual worker imports and constructs Anthropic during startup.
- ElevenLabs, OpenWakeWord, sounddevice, pycaw, and comtypes are already conditionally/lazily imported.
- The August audit measured `worker.app` at 2.381 seconds cumulative, so today’s 6.444-second result warrants dependency-lock and repeated-run investigation ([overhead audit:41–62](</C:/Users/danie/Atlas-worktrees/streamline/docs/audits/2026-08-23-overhead.md:41>)).

### Line counts

Current post-fix output of the requested `wc -l` command:

| Worker file | LOC | Worker file | LOC |
|---|---:|---|---:|
| `__init__.py` | 0 | `addressing.py` | 60 |
| `app.py` | 956 | `brain.py` | 432 |
| `chat.py` | 69 | `claude_launcher.py` | 378 |
| `desktop.py` | 582 | `desktopapps.py` | 373 |
| `devicewatch.py` | 344 | `engagement.py` | 48 |
| `envload.py` | 21 | `jobobject.py` | 219 |
| `jobstore.py` | 358 | `localfiles.py` | 383 |
| `mcp_client.py` | 410 | `payload_codec.py` | 100 |
| `router.py` | 74 | `runtime.py` | 161 |
| `sanitize.py` | 48 | `state.py` | 188 |
| `stateserver.py` | 590 | `tools.py` | 512 |
| `wakeword.py` | 406 | `work.py` | 369 |
| **Worker subtotal** | **7,081** |  |  |

| UI file | LOC |
|---|---:|
| `ui/app.js` | 999 |
| `ui/styles.css` | 876 |
| **Requested UI subtotal** | **1,875** |

`ui/index.html` is another 150 lines but was not part of the requested `wc` command.

| Test file | LOC | Test file | LOC |
|---|---:|---|---:|
| `conftest.py` | 6 | `test_addressing.py` | 87 |
| `test_app_turns.py` | 716 | `test_brain.py` | 984 |
| `test_claude_launcher.py` | 360 | `test_desktop.py` | 711 |
| `test_desktopapps.py` | 274 | `test_devicewatch.py` | 660 |
| `test_engagement.py` | 244 | `test_jobobject.py` | 136 |
| `test_jobstore.py` | 216 | `test_localfiles.py` | 414 |
| `test_mcp_client.py` | 713 | `test_no_heavy_api_path.py` | 11 |
| `test_payload_codec.py` | 28 | `test_process_entrypoints.py` | 73 |
| `test_reflex.py` | 73 | `test_runtime.py` | 207 |
| `test_sanitize.py` | 68 | `test_state.py` | 192 |
| `test_stateserver.py` | 544 | `test_tools.py` | 682 |
| `test_voice_production_cutover.py` | 85 | `test_wakeword.py` | 332 |
| `test_work.py` | 621 |  |  |
| **Test subtotal** | **8,437** |  |  |

Total requested scope: **17,393 LOC**. Production portion: **8,956 LOC**.

### Tests

The exact full-suite command could not initialize in the read-only sandbox. It failed after **4.636 seconds**, before collection, because pytest could not create its capture temporary file:

```text
FileNotFoundError: No usable temporary directory found
```

A write-free collection run found **385 tests in 6.013 seconds**. `node --check ui/app.js` and `git diff --check` both passed.

This is an environment constraint, not a test failure. The full suite still needs execution in a writable environment before any change is accepted.

### Handoff numbers

The handoff’s item 4 says:

> “Worker **600 MB → 308 MB** idle, **13 s → 3.7 s** to `/state`.”

It also notes WebView2’s approximately 400 MB Chromium footprint was unchanged ([handoff:19–22](</C:/Users/danie/Atlas-worktrees/streamline/handoffs/2026-08-23-atlas-wave3.md:19>)).

The accompanying audit gives the precise full-audio sample as **308.4 MB worker RSS**, **320.4 MB process-tree RSS**, and **3.687 seconds** to `/state`—the source of the 308–330 MB operating envelope ([audit:96–105](</C:/Users/danie/Atlas-worktrees/streamline/docs/audits/2026-08-23-overhead.md:96>)).

“Final numbers” records:

> “Full suite: **381 passed** (account-free).”

and:

> “Quick turns measure 1–4 s once MCP is connected; `python -m worker.chat` adds ~5 s per process for its own MCP connect.”

([handoff:71–80](</C:/Users/danie/Atlas-worktrees/streamline/handoffs/2026-08-23-atlas-wave3.md:71>)). The current tree has four additional collected tests from the pywebview reflection fix.

## 2. **Duplicate / near-duplicate code map**

| Pair | Duplication | Proposed single home |
|---|---|---|
| `worker/desktop.py:43–58` vs `59–74` | Nearly identical stopped/reconnecting HTML document and CSS. `ui/index.html:1–10` repeats only the ordinary document head. | Private `_status_html(title, message)` in `desktop.py`. Keep it inline so fallback pages work when the UI server is dead. |
| `worker/desktop.py:301–303` vs `worker/mcp_client.py:92–94` | Exact `subprocess.run(... CREATE_NO_WINDOW)` taskkill wrapper. | `worker/jobobject.py`, which already owns Windows process-tree containment. |
| `worker/tools.py:470–473` vs `worker/desktopapps.py:139–146` | Same direct-HTTPS validation. | Public `desktopapps.is_direct_https`; `tools` delegates. Keep configured HTTP aliases separate from model-supplied direct URLs. |
| `worker/desktop.py:251–267` vs `312–319` | Request construction, timeout, bounded response read, and `urlopen`. | Private `_loopback_request` in `desktop.py`; importing `stateserver` into the shell would unnecessarily add aiohttp. |
| `worker/app.py:98–99` vs `worker/chat.py:36–39` | Same Atlas YAML loading and root derivation. | `runtime.load_config()` for worker entrypoints. Do not make `desktop.py` or `wakeword.py` import the heavy runtime just for a path. |
| `worker/app.py:29`, `chat.py:16`, `runtime.py:21`, `desktop.py:34`, `wakeword.py:39`, `stateserver.py:36` | Repeated repository-root calculation. | A cheap `ROOT` constant in the currently empty `worker/__init__.py`. Security-sensitive path resolution remains in its owning modules. |
| `worker/jobstore.py:57–67` vs `stateserver.py:188–206` vs `tools.py:481–487` | Three job payload projections. | `Job.to_public()` remains the full UI DTO home. Keep `stateserver._safe_job` as an independent untrusted-boundary validator; the compact model-facing status may remain separate. |
| `worker/mcp_client.py:244–252,362–374` vs `tools.py:241–251,508–512` | Mirrored MCP success text is cleaned/bounded and then bounded again by `ToolRegistry`. | Registry owns successful tool-result size. MCP keeps control cleaning and bounded error handling. |
| `worker/app.py:122–178,237–369` vs `devicewatch.py:29–155,195–344` | Device discovery, status shaping, follow wiring, retry/restart callbacks are split across orchestration and device ownership. | `devicewatch.py`; `app.py` should call one `start_audio_follow(...)`. |
| `router.py:21–27`, `addressing.py:23–26`, `brain.py:419–432` | Utterance normalization has one real implementation but is spread across two tiny modules. | Merge `addressing.py` into `router.py`, or rename the combined module to `utterance.py`. |
| `ui/app.js:434–481` vs `739–758` | View tabs and worker tabs independently toggle active state, ARIA selection, and handlers. | One small tab activation helper in `app.js`. |
| `ui/app.js:705–758` vs `811–844` | Worker and history rendering repeat job activity classification, title/status markup, and cancel-button behavior. | A `jobView(job)` projection plus `cancelButton(job)` helper in `app.js`. |
| `ui/app.js:559–593,684–703,846–975` | Every endpoint repeats `fetch`, status checks, JSON parsing, token handling, and catch behavior. | `requestJson(path, options)` in `app.js`, with explicit public/authenticated variants. |
| `tests/test_addressing.py:11–16`, `test_app_turns.py:54–59`, `test_engagement.py:4–9` | Exact `FakeClock`. | One `fake_clock` fixture in `tests/conftest.py`. |
| `tests/test_desktop.py:73–88` vs `test_jobobject.py:11–13` | Similar names, but the desktop fake models waits/exits while the job-object fake only supplies handles. | Do not merge; their contracts differ. The same applies to the two `FakeRegistry` and `FakeLauncher` implementations. |

Important false positives:

- `payload_codec.py`, `state.py`, and `stateserver.py` all shape “payloads,” but they respectively own DPAPI protection, in-process state, and hostile HTTP output validation. Combining them would erase useful security boundaries.
- `desktop.py`, `claude_launcher.py`, `jobobject.py`, `desktopapps.py`, and `work.py` all touch lifecycle concerns, but they represent distinct authorities: worker host, reviewed `claude --bg`, Windows containment, signed application profiles, and orchestration.
- `stateserver.py` is an HTTP server; `mcp_client.py` is stdio MCP; `work.py` calls a local reviewed CLI. There is no shared HTTP client to extract across those modules.
- TTS sanitization (`sanitize.py`), utterance normalization (`router.py`), file-search normalization (`localfiles.py`), and MCP control-character cleaning have different data-loss and security semantics.

## 3. **Consolidation candidates**

Prioritized candidates:

1. Move `AudioRestartCoalescer`, `_boot_default_device_name`, `_device_status`, `_audio_status`, `_audio_failure_callback`, and `_start_audio_follow` from `app.py` to `devicewatch.py`. Callers reduce to service construction at `app.py:697–711` and one start call at `827–832`. Estimated net reduction: **15–30 LOC**, with roughly 180 lines removed from the 956-line composition root.

2. Merge `addressing.py` into `router.py`. Update `app.py:18,700–703` and the addressing/reflex tests. Estimated reduction: **8–15 LOC and one production module**.

3. Make `/health` the single settings payload. It already includes MCP state at `stateserver.py:389–402`; `/mcp` repeats it at `378–387`, and the UI fetches both at `app.js:905–918`. Remove the separate endpoint/provider. Estimated reduction: **25–35 LOC and one request every five seconds**.

4. Add `runtime.load_config()` for `app.py` and `chat.py`. Estimated reduction: **7–12 LOC** and two YAML-loading implementations. Do not route the desktop shell through `runtime.py`; that would pull brain/MCP imports into the host.

5. Add `_status_html` and `_loopback_request` privately to `desktop.py`. Estimated reduction: **25–40 LOC** while keeping worker-down fallbacks independent.

6. Consolidate UI request handling, tab state, job view models, and static config-path markup. Estimated reduction: **60–100 LOC** in `app.js`; move `CONFIG_PATHS` to static `index.html`.

7. Make Anthropic actually lazy by removing `_LazyAnthropicClient._warm_thread`. Estimated reduction: **12–20 LOC** plus about **49.8 MB** avoided before the first conversational turn, based on the audit’s isolated Anthropic measurement.

8. Centralize trusted task-tree killing in `jobobject.py`. Estimated reduction: **5–10 LOC**, but its executable path must be resolved by host code and never be supplied by the model.

Boundary flags:

- Do not create a generic “process manager” shared by desktop apps and Claude. That would blur rules 2, 4, and 7 in `CLAUDE.md`.
- Do not remove state-server validation merely because `Job.to_public()` exists. HTTP providers are deliberately treated as untrusted.
- Do not unify configured HTTP aliases with direct URL validation: configured aliases may contain reviewed loopback HTTP URLs; model-supplied URLs must remain HTTPS-only.
- Do not put native pywebview objects in any public `js_api` attribute. The current private `_window` and regression tests at `tests/test_desktop.py:122–137` are the correct pattern.

## 4. **Dead / unreachable / vestigial**

- `config/atlas.yaml:4` defines `turn_ceiling_s`, but `runtime.build()` reads only `turn_timeout_s` and does not pass the ceiling to `Brain` (`runtime.py:93–101,153–159`). `Brain` therefore always uses its 30-second default. Wire and validate the key, or delete it; an ineffective safety setting is worse than either choice.
- `tests/test_voice_production_cutover.py:53–56` checks that the configuration text contains `turn_ceiling_s`, but does not prove it affects runtime behavior. Replace that presence check with a runtime construction assertion.
- `ui/app.js:552–557` still accepts `payload.config.active_voice`, `payload.active_voice`, and `payload.wake.model`; the current state snapshot serves `voice` and `wake_model` only (`state.py:161–175`). Those fallback shapes appear vestigial.
- `CONFIG_PATHS` at `ui/app.js:8–14` is static content assembled at runtime. It belongs directly in `ui/index.html`.
- `pytest>=8` is used only under `tests/`, so it is misclassified in production `requirements.txt`. A small `requirements-dev.txt` would keep runtime installation intent accurate.
- Conversely, `numpy` and `sounddevice` are directly imported by production code but are only installed transitively. They should be direct runtime requirements or covered by a locked constraints file.
- The import profile shows a large transitive `openai` import even though Atlas passes `llm=None`. With no lock/constraints file and several unpinned plugin requirements, dependency drift can change startup and memory without a source diff.
- The worktree contains **49 ignored `__pycache__/*.pyc` files** and no CSV, SQLite, or log residue found by the targeted inventory. They should not enter packaging.
- The handoff lists external smoke residue—Gmail drafts, CSV files, and History jobs—at `handoffs/2026-08-23-atlas-wave3.md:79–85`. Deletion should remain a deliberate human action.
- A static CSS-reference check found no clear dead selectors. The apparently unmatched `is-atlas`, `is-tool`, `is-system`, `is-ambient`, and `is-succeeded` classes are generated dynamically at `app.js:514,728,828`.
- `STOPPED_HTML`, `RECONNECTING_HTML`, `worker.chat`, and `_legacy_normalize` are reachable. The legacy normalizer supports the tested `"that's"`/`"thats"` ASR equivalence and should not be removed as “old code.”
- Every requirement has a production or test import. No grep-unused dependency was found.

## 5. **Overhead hot spots**

Startup sequence:

```text
desktop spawn
  worker/desktop.py:450–472
→ wait up to 90 seconds for ATLAS_UI
  worker/desktop.py:474–505
→ worker imports LiveKit + MCP
  worker/app.py:15–24
→ runtime.build starts Anthropic warm-up
  worker/app.py:697; worker/runtime.py:25–57
→ MCP/work tasks scheduled
  worker/app.py:740–744
→ LiveKit connect + STT + Silero VAD + TTS + session start
  worker/app.py:746–763
→ state server starts and URL is emitted
  worker/app.py:789–803
→ pywebview window is created
  worker/desktop.py:509–560
```

| Hot spot | Evidence | Expected win | Risk |
|---|---|---|---|
| LiveKit/CLI/transitive OpenAI imports | `app.py:15–16`; current trace: 4.911 s cumulative in `livekit.agents` | Dependency locking gives reproducibility; an upstream-supported lighter import could save seconds and substantial RSS | Do not bypass the reviewed LiveKit voice path or patch `.venv` internals |
| “Lazy” Anthropic warm-up | `runtime.py:25–57,150–151` | Avoid roughly 49.8 MB and startup contention until first real turn | First conversational turn pays the initialization cost |
| Eager MCP package import | `mcp_client.py:16–17`; audit: 43.2 MB isolated | Start `/state` sooner by importing inside the background connection path | Idle memory returns once MCP connects |
| State server starts after voice session | `app.py:746–803` | Moving it earlier could improve visible startup, especially on slow audio initialization | Must expose `ready:false`; otherwise it merely games `/state` timing |
| Desktop waits silently for 90 seconds | `desktop.py:434,474–480` | A measured 20–30 second failure budget would cut apparent hangs by 60–70 seconds | Validate against five cold starts and slow-device cases first |
| Wake-word loop | 80 ms frames and continuous inference at `wakeword.py:328–367` | Wake inference must remain always-on | Any reduction risks missed wake words |
| Unnecessary asleep spectrum work | Bands calculated every 80 ms at `wakeword.py:351–365`, while `state.py:83–85` always returns zeros when asleep | Skip the FFT/band callback while asleep: eliminate about 12.5 FFTs/sec during normal idle | Re-enable atomically on engagement so the first visible frame is not stale |
| Device watcher | Two COM probes every 1.5 s at `devicewatch.py:64–155` | Increasing to 3 s halves probes | Adds up to 1.5 s to audio-device following; current cadence is reasonable |
| Work polling | `work.py:221–266,348–365`; two Claude CLI calls every 2 s per active job | Adaptive 2→5 s backoff reduces roughly 60 to 24 child CLI calls/min/job | Slower progress and completion visibility |
| State-server provider offload | `_provide` uses `asyncio.to_thread` for sync providers (`stateserver.py:165–171`) | Reducing duplicate endpoint polling cuts thread-pool dispatch | Calling arbitrary providers inline could block the event loop; keep the boundary |
| UI polling | `app.js:17,580–607,987–998` | Current live view: about **1,404 requests/min**; hidden: **204/min**. A 10 Hz signal, 1 Hz state, 2 s jobs, and view-scoped settings roughly halves live traffic and cuts hidden traffic by over 90% | Signal smoothness and job freshness require a visual smoke test |
| Duplicate settings requests | `/mcp` and `/health` both expose MCP state; `app.js:909–917` | Remove one endpoint/request and one provider call | Internal API and tests change together |
| Diagnostics gap | No logging configuration; stdout is piped at `desktop.py:450–460`, then discarded at `415–420` | A bounded lifecycle log makes timeout/exit/restart phases diagnosable | Raw stdout contains the pairing URL and must never be written verbatim |

The smallest safe logging addition is:

- `%LOCALAPPDATA%\Atlas\logs\desktop.log`, `RotatingFileHandler`, 256 KiB, two backups.
- Log only host-owned lifecycle events: spawn PID, URL-received boolean, wait timeout, exit code, restart, shutdown escalation.
- Never log the `ATLAS_UI` URL, pairing token, shutdown token, child environment, MCP command environment, prompts, or raw worker stdout.
- If worker detail is needed, give only `atlas.*` loggers a separate bounded handler; do not persist third-party stdout.

Memory remains dominated by reviewed voice components: the audit measured OpenWakeWord at 121 MB fresh, LiveKit agents at 110.1 MB, Silero at 126.4 MB, and MCP at 43.2 MB isolated. Full audio settled at 308.4 MB worker RSS/320.4 MB process tree. WebView2’s approximately 400 MB belongs to the desktop window and is outside the worker target.

## 6. **Infrastructure and governance for further building**

### Module boundaries

| Boundary | Owns | Rule |
|---|---|---|
| Host shell | `desktop.py`, `desktopapps.py`, `jobobject.py` | Window, signed desktop profiles, worker lifecycle, containment, bounded host diagnostics. Must not import brain/MCP/runtime composition. |
| Worker voice runtime | `app.py`, `runtime.py`, `devicewatch.py`, `wakeword.py`, `engagement.py`, `router.py`, `sanitize.py`, `state.py`, `stateserver.py`, `envload.py` | Compose services, voice/audio state, local UI server. `app.py` should contain orchestration, not device implementations. |
| Brain and capabilities | `brain.py`, `tools.py`, `mcp_client.py`, `localfiles.py`, `work.py`, `claude_launcher.py`, `jobstore.py`, `payload_codec.py` | Typed schemas, host execution, confirmation, external-content taint, protected background work. |
| UI | `ui/index.html`, `ui/app.js`, `ui/styles.css` | Static structure, local state rendering, no authority decisions. |

A new built-in tool should require:

- Its implementation in the module that owns the capability.
- One typed `Tool` registration in `tools.py`.
- One explicit `instant` or `confirm` policy.
- A focused test beside that owning module.
- No model-supplied executable path. Mutating MCP remains host-confirmed.

A new UI view should require:

- Static navigation and panel structure in `index.html`.
- Styles in the view’s CSS section.
- JavaScript only for dynamic behavior. Derive valid routes from `[data-view]` so a static view does not also require editing a hard-coded route set.

### Tests

Keep the existing module-mirroring layout. Consolidate only exact shared fakes such as `FakeClock`; do not create a large magical test harness. Merge tiny governance tests into a single `test_governance.py` only if their ownership remains clear. Never delete or weaken security tests to reduce LOC.

### `.venv` and shortcut deployment

The worktree has no `.venv`; measurements use `C:\Users\danie\Atlas\.venv`. However, [install_shortcut.ps1:3–24](</C:/Users/danie/Atlas-worktrees/streamline/scripts/install_shortcut.ps1:3>) binds the shortcut to whichever checkout runs the script and expects that checkout’s `.venv`.

Therefore:

- Treat `C:\Users\danie\Atlas` as the canonical deployed checkout.
- Never install shortcuts from disposable worktrees.
- The shortcut’s `TargetPath` and `WorkingDirectory` must resolve to the same canonical root.
- Document the promotion sequence: update canonical checkout, update its `.venv`, run doctor, run full tests, then reinstall/refresh the shortcut.

### `CLAUDE.md` gaps

Add these concise rules:

> Objects passed to pywebview as `js_api` expose only explicitly reviewed public methods. Native objects, handles, locks, windows, callbacks, and mutable state remain private. Every `js_api` class has a reflection-walk regression test.

> Persistent logs are bounded and host-shaped. They never contain pairing or shutdown tokens, credentials, private environment values, MCP child environments, prompts, or raw child stdout.

> No new eager third-party import may be added to the desktop or worker startup path without an `importtime` comparison and idle-RSS check.

The first rule directly codifies the repaired class at `desktop.py:77–146` and tests at `test_desktop.py:122–137`.

### Scripts

Add one `scripts/doctor.ps1`, with no network or account activity, that checks:

- Canonical root and adjacent `.venv`.
- Python 3.13 and importability of declared direct dependencies.
- YAML syntax and required non-secret config keys.
- UI assets and `node --check ui/app.js`.
- State port availability.
- `claude` executable presence without launching it.
- Shortcut target and working directory.
- Writable `%LOCALAPPDATA%\Atlas` runtime directories.
- No reading of `~/.atlas/env` or `~/.claude.json`; never print environment values.

Also align `pyproject.toml`’s Ruff target (`py311`) with the deployed Python 3.13 runtime and add a minimal constraints/lock process to stop transitive import drift.

## 7. **Proposed build units**

Global acceptance baseline for every unit:

- Current collected count never falls below **385**; new tests increase it.
- Full suite passes in a writable environment.
- `node --check` and `git diff --check` pass.
- Five-process median `worker.app` cumulative import does not exceed **6.444 s** or its freshly recorded pre-unit median.
- Measured production scope starts at **8,956 LOC**; refactor units should be net-negative unless explicitly marked infrastructure.

### Unit 1 — Startup laziness and lifecycle primitive

- Files: `worker/runtime.py`, `worker/mcp_client.py`, `worker/jobobject.py`, `config/atlas.yaml`, `tests/test_runtime.py`, `tests/test_mcp_client.py`, `tests/test_jobobject.py`.
- Changes: remove Anthropic warm thread; lazy-import external MCP; wire `turn_ceiling_s`; add one trusted task-tree killer.
- Tests: focused three test modules; assert `runtime.build()` does not load `anthropic`; assert MCP environment values never enter logs.
- Benchmark: lower time to `/state`; pre-turn RSS expected about 50 MB lower; owned production LOC ≤ current 790.
- Effort: **1–1.5 days**.
- Parallelism: can run alongside Units 3, 4, and 7.

### Unit 2 — Desktop shell and bounded diagnostics

- Files: `worker/desktop.py`, `tests/test_desktop.py`, `CLAUDE.md`.
- Changes: status-page generator, loopback request helper, bounded lifecycle log, consume Unit 1’s task-tree helper, retain private `_window`.
- Tests: reflection walk, token-redaction, rollover, EOF/timeout, graceful/forced shutdown.
- Benchmark: `desktop.py` ≤582 LOC; worker import unchanged; failed startup diagnosed within the measured timeout.
- Effort: **1 day**.
- Dependency: after Unit 1.

### Unit 3 — Audio ownership and asleep CPU cut

- Files: `worker/app.py`, `worker/devicewatch.py`, `worker/wakeword.py`, `tests/test_app_turns.py`, `tests/test_devicewatch.py`, `tests/test_wakeword.py`.
- Changes: move audio-follow construction out of `app.py`; skip visual-band FFT while state is asleep; preserve wake inference and device polling.
- Tests: all three focused modules plus a counter proving no asleep band calculation and immediate re-enable on wake.
- Benchmark: combined production LOC ≤1,680 from 1,706; no import regression; idle CPU lower in a 60-second sample.
- Effort: **1–1.5 days**.
- Parallelism: independent of Units 1, 4, and 7.

### Unit 4 — UI request/render consolidation

- Files: `ui/app.js`, `ui/index.html`, `ui/styles.css`.
- Changes: request helper, generic tab activation, job view model, static config list, visibility/view-aware polling, 10 Hz signal.
- Tests: `node --check`; manual Live/History/Settings keyboard and visibility smoke; request-counter smoke.
- Benchmark: UI LOC ≤1,945 from 2,025 including `index.html`; live requests at least 45% lower and hidden requests at least 85% lower.
- Effort: **1 day**.
- Parallelism: independent of Units 1 and 3.

### Unit 5 — One settings health endpoint

- Files: `worker/app.py`, `worker/stateserver.py`, `ui/app.js`, `tests/test_stateserver.py`.
- Changes: return Claude and MCP state only from `/health`; remove `/mcp` provider, handler, route, and fetch.
- Tests: state-server projection/security tests and a UI smoke.
- Benchmark: net **25+ production LOC removed**; one fewer request per settings refresh; no import regression.
- Effort: **0.5 day**.
- Dependency: after Units 3 and 4 because it overlaps `app.py` and `app.js`.

### Unit 6 — Utterance module consolidation

- Files: `worker/addressing.py` (delete), `worker/router.py`, `worker/app.py`, `tests/test_addressing.py`, `tests/test_reflex.py`, `tests/test_app_turns.py`.
- Changes: move `Addressing` and vocabulary handling into `router.py`; retain modern and ASR-compatibility normalization.
- Tests: all three focused modules.
- Benchmark: worker module count **24→23**; net production LOC reduction; no change to reflex/addressing behavior.
- Effort: **0.5 day**.
- Dependency: after Units 3 and 5.

### Unit 7 — Canonical deployment doctor

- Files: `scripts/doctor.ps1` (new), `scripts/install_shortcut.ps1`, `README.md`, `requirements.txt`, `requirements-dev.txt` (new), `pyproject.toml`.
- Changes: canonical-root/venv/shortcut checks, dependency classification, explicit direct runtime imports, Python 3.13 alignment.
- Tests: doctor check-only run, shortcut dry validation, dependency import probe, existing verification commands.
- Benchmark: worker/UI production LOC and import time unchanged; doctor ≤150 LOC and no network/private-config access.
- Effort: **0.5–1 day**.
- Parallelism: independent of all code units.

## 8. **Risks / do-not-touch**

- Preserve all eight rules in [CLAUDE.md](</C:/Users/danie/Atlas-worktrees/streamline/CLAUDE.md:1>), especially the prohibition on heavy/fallback execution paths.
- Keep the typed `ToolRegistry`, single pending action, expiring confirmation ID, and later host-owned confirmation at `tools.py:109–251`.
- Never log, serve, or copy MCP child environments read at `mcp_client.py:225–235`.
- Keep direct `open` requests HTTPS-only and configured aliases distinct (`tools.py:291–305,462–473`).
- Keep desktop executables confined to signed profiles in `desktopapps.py`; do not replace this with arbitrary `Popen` or PATH lookup.
- Keep DPAPI protection fail-closed. Do not merge `payload_codec.py` into general serialization or add plaintext/non-Windows fallbacks.
- Keep pairing secrets in the URL fragment, exchange them once, and retain the bearer only in tab memory/session storage. Preserve CSP, same-origin validation, body limits, and output bounds.
- Keep state-server validation even after DTO consolidation; it is a security boundary, not accidental duplication.
- Keep Silero VAD, Flux STT end-of-turn, the configured single OpenWakeWord model, and retry/fallback behavior unless live audio benchmarks justify a change.
- Do not optimize the always-on wake inference cadence. Only skip visual calculations whose output is already suppressed while asleep.
- Keep Job Object containment and Claude’s explicit breakaway/background semantics. Never replace them with unbounded shell commands.
- Keep stopped/reconnecting fallback HTML available without the worker state server.
- Do not reduce the 90-second UI wait until repeated cold-start evidence establishes a safe lower ceiling.
- Do not delete governance/security tests to achieve a LOC target.
- Do not introduce a frontend framework, a standalone browser-host process, a generic service container, or a generalized process-execution abstraction.

--- codex-dispatch card 6a8e0a20-ecc7599b | model gpt-5.6-sol | exit 0 | 1556s | ops publish: pushed | log: C:\Users\danie\AppData\Local\kb-codex-dispatch\logs\6a8e0413-30d7db27.jsonl | session 01a03ac0-013e-74d2-ad6c-b489fb7fe940 (follow up with --follow-up 01a03ac0-013e-74d2-ad6c-b489fb7fe940)
