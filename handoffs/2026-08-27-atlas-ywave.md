# Handoff 2026-08-27 - Atlas Y-wave deployed (the five approved reel-research items)

Live checkout `C:\Users\danie\Atlas` = `claude/atlas-streamline` = `bfb7164` (+ this handoff), deployed 18:24, **632 tests**
(from 553), JS hit-test 2/2. Plan: `docs/plans/2026-08-27-atlas-ywave-plan.md`. Source: `docs/research/2026-08-27-reel-research.md`.

## What shipped

| Unit | What it does now | Verified live |
|---|---|---|
| Y1 prompt caching | Both cache breakpoints carry `ttl: "1h"`; the Brain prompt snapshot is built once after MCP servers settle and rebuilt only when the tool-name set changes (per-server tool replacement/removal on reconnect); a one-shot `count_tokens` cache-floor check runs after the first real turn and shows as `/health.cache_floor_ok` (null until then); usage records cache read/write counts. | `/health.cache_floor_ok: null` before the first turn; three servers settled without invalidating each other. |
| Y2 traces | `worker/traces.py`: append-only SQLite at `%LOCALAPPDATA%\Atlas\traces.db` with `turns` + steps ROUTE / GENERATE / TOOL_CALL / RESPOND (latency, tokens incl. cache counts, estimated cost, tool NAME, ok) - metadata only, allowlisted names, UUID turn ids, cached local-day rollup in `/health.traces`, 30-day retention, `scripts/traces.py --days N`. Lazy: `import worker.app` never imports it. | `/health.traces` = enabled, zeros before the first turn. |
| Y3 connector status | Every MCP server reports `state` in connecting / connected / not_configured / error + a closed-vocabulary `detail` (`worker/statusdetail.py`); desktop app profiles report configured / not_configured with a cached snapshot (refreshed every 600 s off the poll path, `as_of`); Settings view shows the list; capability text is sorted/deterministic and carries (server, state) so refusals are truthful. | kb / google / chrome-devtools = `connected | ready` (32/38/27 tools); apps: vscode, chrome, notepad configured; **wt and spotify `not_configured`** (no signed executable found at the configured path - see follow-ups). |
| Y4 edit-time hook | `scripts/hooks/post_edit_check.py` (ruff `F,E9,B` + pyright errors + `node --check`, 2,000-char cap, 15 s tree-killed deadline, tools resolved from the venv never CWD) wired for Claude Code (`.claude/settings.json`) and Codex (`codex-hooks.json` -> `scripts/install_codex_hooks.py` -> `.codex/hooks.json`, installed in the live checkout). ruff + pyright installed in the shared venv. | Probed by hand: bad file -> `F821 Undefined name` + pyright message; clean file -> `{}`. Codex firing on `apply_patch` NOT verified (payload key unknown; two upstream issues reported PostToolUse not firing on apply_patch). |
| Y5a assistant loop | `/state.tool = {name, since}` while a registry tool runs (token-tracked, newest shown); UI TOOL palette (amber) + `TOOL - <name>` strip; time-of-day greeting with `user.name` from config. | `/state.user.name = Daniel`, `tool: null` idle. Greeting/strip need a look at the window. |
| Y5b quick actions | 14 outer-ring segments = `config/quick_actions.yaml` (Spotify, Windows, Play/Pause, Next, Previous, Mute, Vol+/-, Gmail, Calendar, Terminal, VS Code, Notepad, GitHub) executed via `POST /actions/quick` through the same registry policy path (instant runs; confirm-tier creates the normal pending action; typed text via `POST /turn` goes through the spoken-turn guards as an addressed turn); holo CSS keyframes with reduced-motion respect. | Endpoint auth/policy covered by tests; click/keyboard paths need your hands on the window. |

Perf: importtime 2.1-2.3 s (baseline 2.3-3.0 s); engine +0.006 ms/frame for the TOOL palette (headless measurement); RSS after launch: worker.app=336 MB, worker.desktop=103 MB (idle, all MCP connected).
LOC: worker/ 8,741 -> 10,904 (traces 308, statusdetail, quick actions, hook); ui/app.js 1,505.

## Process record

Six codex builds in parallel worktrees; six adversarial reviews - all six returned REWORK with real findings
(Y1: settle hook overrode the existing on_server hook; Y2: /health blocked on the DB; Y3: Settings poll re-ran
Authenticode checks; Y4: repo-local ruff.cmd via shutil.which; Y5a: concurrent tool completion cleared state;
Y5b: typed text bypassed the addressing guards); six fix rounds; three merge resolutions by codex (no boss hand-edits).

## Try it

Look at the window: greeting above the state label. Click a ring segment (Play/Pause is safest). Type a message in
the input under the ring. Say "Atlas, open Spotify" - expect the TOOL strip during the call, and `/health.cache_floor_ok`
to flip to true/false after that first turn. `python scripts/traces.py --days 1` after a few turns.

## Follow-ups / your gates

- **Spotify and Windows Terminal profiles report `not_configured`** - the signed-exe path in `config/apps.yaml` does
  not resolve on this machine (Store apps live under WindowsApps). Either fix the paths or accept that `open spotify`
  falls back to the alias/URL path; the status is now honest instead of silent.
- Codex hook: run one codex session in the repo and edit a file; if `apply_patch` does not trigger the hook, the
  Claude Code hook still covers Claude workers.
- Ruff baseline on the tree with `F,E9,B`: 22 findings (15 B023, 5 F401, 1 B008, 1 B011) - left as is; a small
  cleanup unit if you want zero.
- kb-side SUGGEST list (autonomy ladder, herdr rules, dream --apply, FTS5, connector panel, plan role) is
  parked for the kb wave.
