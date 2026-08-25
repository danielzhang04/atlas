# Atlas streamline plan - 2026-08-25

Goal (Daniel): Atlas code as streamlined as possible, low running overhead, duplicate code removed, functions
condensed into their proper homes, infrastructure/governance sound for further building.
Success: identical behaviour, fewer production lines and modules, measurably lower startup work and idle CPU,
bounded diagnostics on disk, and rules that prevent today's hang class (js_api reflection walk) from recurring.

Inputs: `docs/audits/2026-08-25-streamline-analysis-raw.md` (Codex gpt-5.6-sol read-only analysis, accepted
with the rulings below). Hotfix `a720ab9` (pywebview reflection hang) is already deployed.

## Rulings on the analysis

- Units 1-7 accepted. Ownership is disjoint per the table below; a unit edits only its owned files.
- Unit 1: do NOT delete the Anthropic warm-up; expose `warm()` and let it run after the state server is up
  (first-turn latency matters more than ~50 MB). MCP package import becomes lazy (inside the connect path).
- The 90 s UI-URL wait stays. Wake inference cadence stays. Device polling stays at 1.5 s.
- Security boundaries stay: state-server validation, DPAPI codec, HTTPS-only direct `open`, signed desktop
  profiles, Job Object containment, host-owned confirm.
- No frameworks, no generic process manager, no standalone browser host.

## Waves and ownership

| Unit | Wave | Owns (exclusive) | Depends on |
|---|---|---|---|
| U1 startup laziness + trusted task-tree kill + `turn_ceiling_s` wiring | A | `worker/runtime.py`, `worker/mcp_client.py`, `worker/jobobject.py`, `config/atlas.yaml`, `tests/test_runtime.py`, `tests/test_mcp_client.py`, `tests/test_jobobject.py`, `tests/test_voice_production_cutover.py` | - |
| U3 audio ownership + asleep FFT skip | A | `worker/app.py`, `worker/devicewatch.py`, `worker/wakeword.py`, `tests/test_app_turns.py`, `tests/test_devicewatch.py`, `tests/test_wakeword.py` | - |
| U4 UI request/render consolidation + polling | A | `ui/app.js`, `ui/index.html`, `ui/styles.css` | - |
| U7 doctor + deps + deploy docs | A | `scripts/doctor.ps1` (new), `scripts/install_shortcut.ps1`, `README.md`, `requirements.txt`, `requirements-dev.txt` (new), `pyproject.toml` | - |
| U2 desktop shell dedupe + bounded log + CLAUDE.md rules | B | `worker/desktop.py`, `tests/test_desktop.py`, `CLAUDE.md` | U1 (task-tree helper) |
| U5 `/health` as the single settings payload + wire `runtime.warm()` | B | `worker/app.py`, `worker/stateserver.py`, `ui/app.js`, `tests/test_stateserver.py` | U3, U4, U1 |
| U6 `addressing.py` -> `router.py` | B | `worker/addressing.py` (delete), `worker/router.py`, `worker/app.py`, `tests/test_addressing.py`, `tests/test_reflex.py`, `tests/test_app_turns.py` | U5 |

Per unit: Codex build (sol) in its own worktree -> Codex adversarial review (sol, read-only) -> fix round ->
boss verification in the real env (full suite, `node --check`, `git diff --check`, benchmarks) -> merge into
`claude/atlas-streamline`. Final: live smoke (launch, hang probe, Live/History/Settings screenshots,
one spoken-equivalent text turn via `python -m worker.chat`), deploy by fast-forwarding `C:\Users\danie\Atlas`.

## Benchmarks (boss-measured, 5-run medians unless stated)

- Test count >= 385 and full suite green.
- `python -X importtime -c "import worker.app"` cumulative: must not exceed the pre-wave median.
- Time to `/state` from worker spawn (standalone `worker.app console`): must not exceed pre-wave median.
- Idle worker RSS at 60 s: must not exceed 330 MB.
- Production LOC (`worker/*.py` + `ui/*`): net negative across the wave.
- UI requests/min (live view, hidden tab): >= 45% and >= 85% lower respectively (U4).
- Hang probe: `IsHungAppWindow` false for 90 s after launch.
