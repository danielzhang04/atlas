# Handoff 2026-08-27 - Atlas X-wave deployed (truthfulness, desktop control, kb bridge, GitHub, condense)

Live checkout `C:\Users\danie\Atlas` = `claude/atlas-streamline` = `4b9382d` + this handoff (deployed 00:53, 553 tests).
GitHub: https://github.com/danielzhang04/atlas (private; `main` PR-protected; branches pushed). kb bridge:
kb repo branch `claude/atlas-bridge` commit `61326059` (`dashboard/atlas-bridge/`, 34 vitest, pushed; no PR yet).

## What changed (Daniel's asks -> units)

| Ask | Unit | Result |
|---|---|---|
| "Spotify is open" said 3x with no tool call | X1 | Host truthfulness guard: a claim sentence ("I opened X", "X is open", "sent", "done"...) is spoken only if a RELEVANT tool succeeded this turn (per-claim table in `worker/claims.py`); otherwise the host substitutes "I did not actually do that - I have no tool result. Want me to?". Clause-local negation, factual sentences pass, streaming of safe sentences is unchanged. |
| Refused folder access it has | X1 | Capability refusals consult the registry; `count_mail` reports inbox + Primary. |
| Control all local desktop stuff, instant except delete | X2 | `worker/desktopcontrol.py` (pure ctypes): `list_windows` (paginated), `focus_window`, `window_action` (minimize/maximize/restore/close/move:left-half or right-half or center/resize), `media_key`, `click` (bounds + foreground checked), `type_text`, `press_keys` (chord allow-list). Deletion (`delete`, `shift+delete`, `ctrl+d`, `ctrl+x`) is confirm-only, bound to the exact HWND with a foreground re-check right before SendInput; single `backspace` stays instant. `open` focuses an already-running signed app instead of relaunching. CLAUDE.md rule 12. |
| Atlas <-> VM kb | X3a (kb) + X3b (Atlas) | Standalone MCP stdio bridge `dashboard/atlas-bridge` (32 tools: 20 READ instant, 12 MUTATION confirm; T3 refused server-side; no T4). Atlas registers it as a `command:` MCP server (fixed argv, env = 3 flags + PATH/SystemRoot), forwards a dashboard session over a private notification, "Atlas, unlock kb" opens a dashboard-origin window for Windows Hello (win32-desktop daemons only). Health shows `session: held/none/expired`. |
| GitHub repo like kb | X4 | Created, secrets audit clean, main protected. |
| No bloat | X5 | brain.py 769 -> 494 (ClaimGuard -> `worker/claims.py` 141), net -150 lines; importtime unchanged (~2.5 s); desktopcontrol lazy-loaded. |

## What works live right now (verified 00:55)

- `/health`: kb 32 tools, google 38, chrome-devtools 27; `/state` ready, wake model `hey_atlas`; frameless
  window style `0x16070000`, icon set.
- kb bridge against the CURRENT daemon (legacy, unauthenticated, 127.0.0.1:5317), no session needed:
  `kb_capabilities`, `kb_agents_list`/`kb_agent_get` (paginated, projected), `kb_workflows_list` (ids = legacy
  `ref`, e.g. `acceptance-run`, `email-triage`), `kb_repo_tree`/`kb_repo_file`/`kb_repo_history`/`kb_repo_search`,
  `kb_grades`, `kb_analytics_snapshot` (key summary of the 9.3 MB index, streamed), legacy mutations
  `kb_agent_create/update`, `kb_workflow_create/update/launch`, `kb_agent_launch`, `kb_schedule_*` (all behind
  the host confirm). `kb_runs_list`, `kb_inbox_list`, `kb_run_control`, `kb_human_respond`, `kb_terminal_list`
  return `capability_unavailable` (legacy 403/404) until the v3 workover lands.
- Desktop control verified on a real window: move right-half, minimize, close, with state readback.

## Try by voice

"Atlas, what agents are on kb?" / "list kb workflows" / "show the kb repo tree" / "what grades did
worker-desktop get?" (instant). "Atlas, launch the email triage workflow" (confirm readback). "Atlas, put
Spotify on the left half" / "minimize Chrome" / "next track" / "type hello" (instant). "Atlas, delete that"
(readback of the exact window, then yes/no).

## Open items / gates

- Rebase after the dashboard-v3 workover lands: re-run bridge negotiation against v1 (fakes already cover
  v1), repoint `config/atlas.yaml` `kb_bridge.path` from `C:/Users/danie/kb-worktrees/atlas-bridge/...` to
  `C:/Users/danie/kb/dashboard/atlas-bridge` (`npm ci && npm run build` there), exercise "unlock kb" on a
  `win32-desktop` daemon (Windows Hello prompt; 5-minute session TTL - `DASHBOARD_SESSION_TTL_MS`).
- Open a PR for `claude/atlas-bridge` into kb when the workover branch is ready to take it.
- Adversarial reviews returned REWORK on every unit; all blockers fixed (see `docs/plans/2026-08-26-atlas-xwave-plan.md`).
- Governance: CLAUDE.md rule 6 reworded (command servers exact env; from_claude_config servers keep mcp's
  default child env - a misreading of the first wording broke google/chrome-devtools for 25 minutes after
  the first deploy; fixed in X6 `4b9382d`), rule 13 added (T3 enforced in the bridge).
- Residue: `Atlas-worktrees\{x1,x2,x3,x5,x6}` dirs are ACL-locked sandbox leftovers (elevated delete), same
  as the u*/v* ones; git no longer tracks them.
