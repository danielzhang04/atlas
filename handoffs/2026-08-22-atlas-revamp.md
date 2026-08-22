# Atlas revamp handoff — 2026-08-22

**Status:** built, tested, adversarially reviewed, and live-smoked on a work branch while Daniel was away.
**Branch:** `claude/atlas-revamp` in worktree `C:\Users\danie\Atlas-worktrees\revamp`
(baseline `130c4a7` on `codex/atlas-standalone-bootstrap` = the 2026-08-21 tree, committed verbatim).
**Spec:** `docs/specs/2026-08-22-atlas-revamp-design.md` · **Plan:** `docs/plans/2026-08-22-atlas-revamp-plan.md`
**One human gate left:** PM2 cutover + a spoken acceptance round (§ "Exact next step").

## What changed (one paragraph)

The single `atlas_delegate_to_claude` tool, the 14-field `Request`, regex routing, the encrypted outbox,
the separate attested `atlas-subscription` process, and the Fable/Haiku/Sonnet/Opus "doctrine" heavy
loop are gone. In their place: `router.py` (reflex: dismiss / cancel / repeat) → `brain.py` (one streaming
Claude call with a tool registry, sentence-chunked straight into TTS) → `tools.py` built-ins
(`open`, `focus`, `confirm`, `cancel_pending`, `launch_work`, `work_status`, `cancel_work`) plus every
tool of Daniel's own MCP servers mirrored by `mcp_client.py` (`config/mcp.yaml`, env from
`~/.claude.json`) → `work.py` (background `claude --bg` session launched on a thread, output streamed
into the job store, spoken completion). `worker/` went from 11,915 to 4,233 lines; tests from 406 to
203 (all account-free). 22 commits on the branch.

## Housekeeping

- Task worktrees `t1`–`t4` are pruned from git; the `Atlas-worktrees\t3` and `t4` directories remain only
  because the Codex sandbox left locked `.pytest-tmp`/`.pytest_cache` residue — safe to delete.
- Smoke jobs (`haiku.txt` under `%LOCALAPPDATA%\Atlas\jobs\<id>`) and their History rows are test residue.
- Keep-awake stayed armed throughout (`kb\scripts\keep_awake.ps1 -Status`).

## Live measurements on this machine (`python -m worker.chat`, real API/MCP/CLI)

| Turn | What happened | Time to first spoken chunk |
|---|---|---|
| "hey, how's it going" | plain reply | 0.82 s |
| "pull up gmail" | `open` fired (Gmail opened in the default browser), "Done." | 1.58 s |
| "what's on my calendar today" | `google__get_events` through the real google-workspace MCP server, spoken summary | ~2 s after MCP connect |
| "research … write me a summary" | "Launching that now. It'll show up in Workers." spoken **before** `launch_work` ran | 0.97 s |
| `launch_work` haiku job | QUEUED → LAUNCHING → RUNNING with a real `claude --bg` session in 7 s; Claude finished in ~15 s; output lines streamed into `/jobs/{id}/events`; **SUCCEEDED with the spoken summary** detected by a *fresh* process (restart path) at +36 s | — |

`worker.chat` wall-clock includes a cold MCP connect (uvx spawn, up to ~25 s on a cold cache); the voice
worker connects MCP once at startup in the background, so voice turns do not pay that.

## Defects found live and fixed (all on the branch)

- Model had no notion of "today" → calendar queried 2024-12-19. Now a per-turn, uncached date/time
  system block follows the cached rules block.
- MCP connect timed out at 20 s on a cold `uvx` cache → 60 s; MCP child stderr no longer floods the log.
- `ClaudeLauncher` looked sessions up by `Path.cwd()` after a restart → explicit job cwd everywhere.
- `claude logs` wraps the result frame at terminal width, echoes the prompt's template frame, and Claude
  prints Windows paths with raw backslashes inside the JSON → wrapped-frame decoding, template excluded by
  status value, frame contract shrunk to `job_id/status/summary` (no paths), backslash-tolerant decoding,
  `}}` typo in the prompt fixed. Verified live: job `1796a614…` → SUCCEEDED with summary.
- `WorkManager.cancel` could resurrect a cancelled job or re-fire callbacks → atomic with launch/poll.
- Poll failures were silently swallowed → logged by exception class; terminal chrome filtered from output.
- Stale pre-revamp `worker.ui_server --port 4361` mirror (started 2026-08-21, not under PM2) held the old
  `jobs.sqlite3` open and blocked the schema migration; it was stopped. The old DB is kept as
  `%LOCALAPPDATA%\Atlas\jobs.sqlite3.pre-revamp`.

## Review

- Sonnet independent verifier: PASS (suite, dead-reference sweep, secret scan of the whole diff, argv
  exactness, docs vs code).
- Codex `gpt-5.6-sol` adversarial review (read-only, xhigh): 10 findings. Fixed on the branch: taint
  after external content (no `confirm`/`launch_work`/arbitrary-URL `open` in a turn that read MCP
  content), loopback Host allowlist on every route + bearer on job events + secret redaction in output
  events, honest `cancel` on `claude stop` failure, output cap no longer stalls completion, `open atlas`
  opens a paired command center, `desktopapps` trimmed to signed profiles. Accepted residuals: the
  result-frame nonce is visible to the session (no structured result channel exists for `claude --bg`);
  brain history records generated text, not what was actually heard before a barge-in; `worker.chat`
  waits for MCP connect before the turn (the voice worker does not).

## Exact next step (Daniel)

1. Cut the live checkout over and restart PM2 (single app now):
   ```powershell
   cd C:\Users\danie\Atlas
   git merge --ff-only claude/atlas-revamp      # or: git checkout claude/atlas-revamp
   pm2 delete atlas-subscription; pm2 start pm2.config.cjs --only atlas-worker; pm2 save
   ```
2. Spoken acceptance: wake → "how's it going" → "pull up gmail" → "what's on my calendar today" →
   "research X and write me a summary" (expect "launching…", a Workers tab with live output, spoken
   "Done — …") → "go to sleep".
3. If a voice turn feels slow, `python -m worker.chat "<same words>"` prints chunks with timings.

Text console without audio: `python -m worker.chat "pull up gmail"`. Command center only:
`python -m worker.ui_server`. Teach new apps in `config/apps.yaml`; add MCP servers in `config/mcp.yaml`.
