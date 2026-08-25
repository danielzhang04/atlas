# Atlas handoff - 2026-08-25 (freeze fix + streamline wave)

**Status:** DONE and DEPLOYED. `C:\Users\danie\Atlas` (the canonical checkout the Start-menu/Desktop "Atlas"
shortcuts run) is fast-forwarded to `claude/atlas-streamline` HEAD (`git log -1` here). 410 tests.
**Previous:** `2026-08-23-atlas-wave3.md`. **Plan:** `docs/plans/2026-08-25-atlas-streamline-plan.md`.
**Audits:** `docs/audits/2026-08-25-streamline-analysis-raw.md` (read-only analysis, Codex gpt-5.6-sol),
`docs/audits/2026-08-25-streamline-wave-review.md` (whole-wave adversarial review; its BLOCKER is fixed by U8).

## 1. The freeze ("Atlas / Python not responding" on open) - root cause and fix

pywebview 6.2.1 `inject_pywebview()` walks every public, non-callable attribute of the `js_api` object
recursively (dedup by `id()`). The frameless title bar (08-23, `7bde3fb`) gave `WindowApi` a public `window`
attribute holding the live pywebview `Window`; the walk descended `window.native` (WinForms) ->
`AccessibilityObject.Bounds.Empty.Empty...` on the GUI thread - every CLR struct access boxes a fresh object,
so the dedup never trips, it recurses to RecursionError and keeps enumerating the CLR graph. Reproduced with
`IsHungAppWindow` = True from ~t+20 s and the stderr line `[pywebview] Error while processing
window.native.AccessibilityObject.Bounds.Empty...`. Fix `a720ab9`: the attribute is private (`_window`,
skipped by the walk) + two regression tests (public `WindowApi` names are methods only; a pywebview-shaped walk
terminates). CLAUDE.md rule 9 now codifies it. Verified live: window responsive for 95 s, no pywebview errors.

## 2. Streamline wave - what changed (all reviewed by Codex sol, fixed, boss-verified, merged)

| Unit | Result |
|---|---|
| U1 runtime/mcp/jobobject | Anthropic warm-up is an explicit `warm()` (idempotent, one thread) called after the state server is up; external `mcp` package imported lazily in the connect path; `turn_ceiling_s` from config finally reaches `Brain` (finite, positive, >= turn_timeout_s); `jobobject.kill_process_tree(pid, force=)` (absolute taskkill) replaces two local wrappers. |
| U2 desktop shell | `_status_html`/`_loopback_request` dedupes; bounded host-shaped lifecycle log `%LOCALAPPDATA%\Atlas\logs\desktop.log` (256 KiB x 2; spawn pid, UI URL received=bool, wait timeout, window created, child exit code, restart/burst, shutdown escalation; worker traceback markers stored as category+count only); two-step tree kill (non-forced after 20 s, forced after 10 more); CLAUDE.md rules 9-11. Still exactly 582 lines. |
| U3 audio | Device following, status shaping, restart coalescing live in `devicewatch.py` (`start_audio_follow`, `request_restart` required); `app.py` 956 -> 709; no visual FFT while ASLEEP (wake inference untouched), first awake frame computed fresh from a zero prior before engagement. |
| U4 UI | One `requestJson` (public/authenticated); shared tab activation and job view/cancel helpers; static config list in `index.html`; polling: signal 10 Hz only on Live, state 1 Hz, jobs 2 s, health only on Settings; hidden tab = 24 req/min flat. Measured headless: `/signal` only while Live visible, `/mcp` 0. UI 2025 -> 1943 lines. |
| U5 health | `/health` is the single Settings payload (`/mcp` gone); Settings renders MCP servers/tool counts from it (verified: "google - 38 tools"). |
| U6 router | `addressing.py` folded into `router.py` (24 -> 23 modules); composition-root leftovers removed. |
| U7 deploy | `scripts/doctor.ps1` (read-only checks: canonical root, .venv 3.13, runtime imports from requirements.txt, config keys, UI assets, configured state_port, claude on PATH, shortcut targets, runtime dir); `install_shortcut.ps1` refuses non-canonical checkouts unless `-Force`; pytest -> `requirements-dev.txt`; numpy/sounddevice pinned; Ruff py313; README "Deploy / promote" + dependency policy. |
| U8 startup order | `build -> publisher -> state server (ready:false) -> ATLAS_UI -> warm -> wake device/audio status -> MCP/work tasks -> LiveKit -> ready:true -> audio follow`; one idempotent cleanup registered right after the server starts and invoked on any later startup failure/early shutdown. |

## 3. Numbers (boss-measured, A/B back to back on the same machine state)

| Metric | Before (`a720ab9`) | After (`ede48fe`) |
|---|---|---|
| Tests | 385 | 410 |
| `import worker.app` cumulative, 5-run median | 2.76 s | 2.19 s (-21%) |
| Worker spawn -> `ATLAS_UI` | 4.38 s | 3.38 s (-23%) |
| Worker spawn -> `/state` 200 | 4.51 s | 4.16 s |
| Desktop: window present + responsive | t+11 s | t+5 s |
| Process-tree RSS at 60 s (worker + LiveKit job) | 349 MB | 349 MB (models dominate; warm-up deferred, not removed) |
| Production LOC (`worker/*.py` + `ui/*`) | 9,106 | 8,992 (-114) |
| Worker modules | 24 | 23 |
| UI requests/min, Live visible / hidden tab | 1,404 / 204 | 690 (+30 per active job) / 24 |
| Hang probe (`IsHungAppWindow`) | hung from ~20 s | never |

## 4. Where things live now (for the next worker)

`desktop.py` window/process/log owner - `jobobject.py` containment + tree kill - `app.py` composition and
voice lifecycle only - `runtime.py` service construction + model warming - `stateserver.py` loopback
serving/pairing/public projections - `devicewatch.py` all audio-device following - `wakeword.py`
capture/inference - `router.py` normalization/reflex/addressing - `brain.py` model-turn orchestration -
`tools.py` typed policy/confirmation boundary - `mcp_client.py` lazy MCP transport - `ui/app.js` requests,
polling, rendering. A new tool = implementation in its owning module + one typed registration in `tools.py` +
policy + a test beside the owner. A new UI view = static structure in `index.html` (routes derive from
`[data-view]`), CSS section, JS only for dynamic behavior. Promotion sequence is in README ("Deploy / promote");
`scripts\doctor.ps1` from the canonical root must exit 0 before reinstalling shortcuts.

## 5. Not done / residue

- Live spoken acceptance is Daniel's (machine was PIN-locked all evening; verification was programmatic:
  tests, hang probe, headless browser smoke of Live/History/Settings, request tally, doctor).
- Test residue from the smoke: none created (no jobs launched, no drafts).
- `Atlas-worktrees\` unit dirs u1-u8 and `revamp` were swept; any dir still present is ACL-locked sandbox
  residue (`.pytest-tmp`, `__pycache__`) needing an elevated delete - listed in the boss summary.
