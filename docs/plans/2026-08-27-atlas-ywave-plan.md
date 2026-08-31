# Atlas Y-wave plan (2026-08-27) - the five approved reel-research items

Source: docs/research/2026-08-27-reel-research.md (Atlas SUGGEST list, approved by Daniel 2026-08-27; kb deferred).
Pipeline per unit: codex sol build in its own worktree (TDD, fakes) -> sol adversarial review (read-only) ->
fix round -> boss verification in the real environment -> merge into claude/atlas-streamline -> deploy -> live
probes -> handoff. Constraints: CLAUDE.md rules 1-13 (esp. 1 no credentials, 10 host-shaped bounded logs, 11 no
new eager import without importtime/RSS evidence, 9 methods-only js_api); no new UI_ASSETS files; CSP unchanged.

## Brainstorm -> decisions (YAGNI)

- Caching: both breakpoints get `ttl: "1h"` (voice idle >> 5 min; writes 2x once per hour, reads 0.1x). The Brain
  is built once after MCP servers settle and rebuilt only when the tool set actually changes, so late-connecting
  servers stop invalidating the cached system block. Haiku 4.5's 4,096-token cache floor is checked lazily on
  the first turn via count_tokens (one cheap call) and logged as a warning if unmet - never a startup API call.
  Rejected: per-server Brains (complexity), disabling capability text (truthfulness regression).
- Traces: append-only SQLite (stdlib sqlite3, WAL) at %LOCALAPPDATA%\Atlas\traces.db with `turns` and `steps`
  (ROUTE / GENERATE / TOOL_CALL / RESPOND) carrying latency, token usage incl. cache read/write, estimated cost,
  tool NAME and ok flag - metadata only, never prompts/args/transcripts (rule 10). Written off the turn loop via
  a thread executor so the voice path never blocks. /health exposes a bounded summary; scripts/traces.py prints
  daily rollups. 30-day retention. Rejected: JSONL (no queries), external DB (deps).
- Connector status: every MCP server and desktop app profile reports `state` in
  {connected, connecting, not_configured, error} + a bounded host-shaped `detail`; surfaced in /health, the
  Settings view, and the brain's capability text so refusals are truthful ("google is not configured").
- Edit-time feedback: ruff + pyright in requirements-dev; a stdlib PostToolUse hook script that checks ONLY the
  edited file, caps output at 2,000 chars, and emits hookSpecificOutput.additionalContext; registered for Claude
  Code (.claude/settings.json) and Codex (.codex/hooks.json, matcher apply_patch|Edit|Write) - Codex firing is
  verified by the boss in the real env. JS files get `node --check`.
- Assistant-loop UI: worker publishes the in-flight tool name to /state; UI adds a TOOL palette and a
  "TOOL - <name>" strip; time-of-day greeting with the configured name; the existing 14 outer ring segments
  become quick actions from config/quick_actions.yaml (polar hit-test, DOM label spans, executed host-side
  through the same registry + policy, confirm-tier still confirms); a transparent text input feeds the
  existing text-turn path; holo CSS keyframes (own, no fonts, reduced-motion respected). Rejected: WebGL,
  gestures, new asset files.

## Tasklist

- [x] Y1 caching: ttl 1h on tools[-1] and system[0]; Brain built after MCP settle, rebuilt on real tool-set
      change; lazy count_tokens floor warning. Tests with fakes. (worktree y1)
- [x] Y2 traces: worker/traces.py + wiring in the turn loop + /health summary + scripts/traces.py. (y2)
- [x] Y3 connector status: mcp_client + desktopapps state/detail; /health; Settings view; capability text. (y3)
- [x] Y4 hooks: requirements-dev, scripts/hooks/post_edit_check.py, .claude/settings.json, .codex/hooks.json,
      tests with fixture files; boss verifies Claude + Codex firing. (y4)
- [x] Y5a assistant loop: /state tool field, TOOL palette + strip, greeting. (y5a)
- [x] Y5b quick actions + text input + holo CSS. (y5b)
- [x] Per unit: adversarial review -> fix -> boss verify -> merge.
- [x] Full suite, importtime + RSS comparison vs 6882436, deploy, live /health + UI probe, handoff, ping Daniel.
