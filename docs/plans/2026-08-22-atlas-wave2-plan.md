# Atlas Wave 2 Implementation Plan

> **For agentic workers:** one worker per task in its own worktree/branch. Do only your task. Read the
> spec first. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Atlas stops answering the room, sleeps silently, reaches files/apps on the desk, counts mail
correctly, and runs as a native window that is on when open and off when closed.

**Spec:** `docs/specs/2026-08-22-atlas-wave2-design.md` (interfaces win on conflict).

## Global Constraints

- Tests: `C:\Users\danie\Atlas\.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider --basetemp .pytest-tmp`
  from the worktree root; delete `.pytest-tmp` after. Account-free: no network, browser, model, `claude`,
  real `taskkill`, real `os.startfile`, or real window.
- Never read/print/copy/log credential values. No `claude -p`, no Agent SDK.
- Change core logic; no shims, re-exports, or "legacy" branches; delete everything a change makes dead
  (imports, config keys, docs, tests). One statement per line; module docstring; `__all__`.
- Workers never commit.

---

### Task W1: listening discipline (engagement + addressing + silent auto-sleep)

**Files:** Modify `worker/engagement.py`, `worker/app.py`, `config/atlas.yaml`, `tests/test_engagement.py`,
`tests/test_app_turns.py`. Create `worker/addressing.py`, `tests/test_addressing.py`.
**Interfaces:** `Engagement(timeout_s: float, clock=time.monotonic)` with `state`, `wake()`,
`interacted()`, `dismiss()`, `tick() -> str`. `Addressing(window_s: float, vocab: Iterable[str],
clock=time.monotonic)` with `mark_activity()`, `is_addressed(norm: str) -> bool`; `router.normalize`
is the normalizer. `app.py`: `_handle_audio_turn` order reflex → `tick` → addressed → brain; a 5 s
`_sleep_watch` task; `_sleep(announce=False)` for auto-sleep; transcript roles `ambient` and the line
`auto-sleep`. Vocabulary = all `words` from `config/apps.yaml` + `atlas.yaml` `address_vocab`.

- [ ] Tests first (both modules pure over an injected clock; app turn tests with fakes).
- [ ] Implement; wire; config keys `engagement_timeout_s: 120`, `address_window_s: 30`,
  `address_vocab: [email, emails, inbox, mail, calendar, file, files, folder, open, close, launch,
  cancel, status, workers, job, jobs, research, summary, write, draft, remind, timer]`.
- [ ] Full suite; `git diff --check`; report.

### Task W2: desk reach + mail count

**Files:** Create `worker/localfiles.py`, `tests/test_localfiles.py`. Modify `worker/tools.py`,
`worker/desktopapps.py`, `worker/mcp_client.py`, `worker/runtime.py`, `worker/brain.py` (BASE_SYSTEM
rules only), `config/atlas.yaml` (`file_roots`), `tests/test_tools.py`, `tests/test_desktopapps.py`,
`tests/test_mcp_client.py`, `tests/test_brain.py`.
**Interfaces:** `localfiles.LocalFiles(roots: Sequence[Path], *, clock=time.monotonic)` with
`find(query, limit=20, budget_s=2.0) -> list[dict]`, `open(path) -> dict` (injectable `opener`),
`read(path, max_bytes=16384) -> dict`, and `resolve(path) -> Path` raising `ValueError("outside roots")`.
`desktopapps.close_profile(app_id, *, killer=...)` → `taskkill /IM <exe>` without `/F`.
`tools.builtin(..., files: LocalFiles | None = None)` registers `find_file`, `open_file`, `read_file`,
`close`. `McpServers.connect(registry, *, on_server=None)`: after a server connects, `on_server(name,
registry)` runs; `runtime.build` passes a hook that registers `count_mail` when `name == "google"`
(implemented in `tools.py` as `register_count_mail(registry, search: Callable[[dict], Awaitable[ToolResult]],
account: str)`). Parser for `Found N messages` and `Next page token: <tok>` lives with `count_mail`.

- [ ] Tests first (tmp roots; fake opener/killer; fake search returning paged `Found N` text).
- [ ] Implement per spec §2–§3; `BASE_SYSTEM` gains the two rules from the spec.
- [ ] Full suite; `git diff --check`; report.

### Task W3: the Atlas app (after W1 merges — touches `app.py`)

**Files:** Create `worker/desktop.py`, `tests/test_desktop.py`, `scripts/install_shortcut.ps1`. Modify
`worker/app.py` (print `ATLAS_UI <url>` after `stateserver.start`; nothing else), `requirements.txt`
(`pywebview>=6.2`), `README.md`, `CLAUDE.md` (app on/off sentence), `ui/index.html`/`ui/app.js` (only if
a "stopped" page is needed). Delete `pm2.config.cjs`, `run-worker.js`, `tests/test_pm2_config.py`.
**Interfaces:** `desktop.run(*, spawn=subprocess.Popen, window_factory=webview.create_window,
start=webview.start, terminate=..., wait_url_timeout_s=90) -> int`; `desktop.read_ui_url(stream) ->
str | None` (parses the `ATLAS_UI` line); `desktop.stop_child(proc, *, killer)` (tree kill, `/F` after
10 s). All injectable; tests never spawn or open anything.

- [ ] Tests first. Implement. `pip install pywebview` into the Atlas venv (allowed: it is the app's
  dependency); verify `import webview` works. Full suite; report.

### Task W4: review + live verification + handoff (boss)

- [ ] Codex deep read-only adversarial review of the merged branch against spec §1–§5 (ambient gating
  bypasses, file-root escapes, `close` abuse, `count_mail` injection via tool text, child-process
  lifecycle, window close with active jobs).
- [ ] Sonnet independent verifier (suite, dead references, secrets, docs vs code).
- [ ] Boss live smokes: `worker.chat` for `count_mail`, `find_file`/`open_file`/`close`; the desktop app
  opened and closed with process check; addressed-gate unit evidence.
- [ ] Handoff `handoffs/2026-08-22-atlas-wave2.md`; the PM2 cutover note is replaced by "open the app".
