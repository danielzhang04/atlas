# Atlas Wave 3 Implementation Plan

> One Codex worker per task in its own worktree; the boss runs the workflow tests. Spec:
> `docs/specs/2026-08-23-atlas-wave3-design.md` (interfaces win on conflict).

## Global Constraints

- Tests: `C:\Users\danie\Atlas\.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider --basetemp .pytest-tmp`
  (delete `.pytest-tmp`); account-free; `node --check ui/app.js`; `git diff --check`.
- Vanilla JS/CSS/Canvas only; no CDN, no build step. One statement per line in Python; no dead information.
- Workers never commit.

### Task V1: UI/UX (ui/index.html, ui/styles.css, ui/app.js; tests/test_stateserver.py for asset checks)
Spec §1 in full: 40 px top bar, working tabs with `location.hash`, new Canvas engine (96 bars, states,
no glyph), History and Settings views with real data. Consumes `/signal` `{energy, bands?}`,
`/state.audio`, `/jobs`, `/jobs/{id}/result`, `/mcp`, `/health`. Visual evidence: produce
`docs/audits/2026-08-23-engine-preview.png` by rendering the canvas headlessly if feasible (optional).

### Task V2: audio follows the system (worker/devicewatch.py, worker/wakeword.py, worker/app.py,
worker/state.py, worker/stateserver.py, worker/desktop.py, config/atlas.yaml, tests)
Spec §2 in full. `/signal` gains `bands` (24 log-spaced band energies from the wake loop's 80 ms frame
via `numpy.fft.rfft`, normalised 0..1, smoothed). `/state.audio` replaces `output_device`. Desktop
restarts the child on exit code 21 (rate-limited) with a "reconnecting audio" page.

### Task V3: overhead (after V2 merges — shares app.py/wakeword.py)
Spec §3: measure with `-X importtime` and RSS deltas, apply cuts, report before/after (`/state` time,
idle working set). Keep behaviour identical; tests for lazy-import seams.

### Task V4 (boss): workflow tests — spec §4, live, monitored; each failure → a fix dispatch.

### Task V5 (boss): Codex read-only adversarial review + Sonnet verifier + live UI/audio smokes +
handoff `handoffs/2026-08-23-atlas-wave3.md`.
