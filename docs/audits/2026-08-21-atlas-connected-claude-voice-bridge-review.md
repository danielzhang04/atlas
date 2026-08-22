# Atlas connected-Claude voice bridge review

**Date:** 2026-08-21  
**Application:** `C:\Users\danie\Atlas`  
**Verdict:** the production action path now matches the intended product: Atlas is voice I/O over a
normal connected Claude Code subscription session, rather than a second hardcoded automation stack

## Goal reviewed

Daniel's target is the capability already available when typing into Claude Code:

- speak an ordinary sentence and receive an ordinary conversational answer;
- speak an action request and let Claude choose and use its connected Chrome, user MCP servers,
  plugins, skills, and normal tools;
- speak a larger task and let the same environment perform it as durable background work;
- preserve Atlas-owned process lifecycle, cancellation, public status, receipts, and honest errors;
- never hardcode every website, application, or workflow into the voice layer.

The prior direct-capability slice did not meet that target. It taught Atlas one `desktop.open` route
and one YouTube alias while the subscription launcher explicitly disabled Chrome, user settings,
and MCP configuration. Scaling that approach would have recreated Claude's reasoning and tool
selection in Python. The earlier direct-capability audit is retained as history but is superseded by
this review for the normal voice execution path.

## Implemented architecture

The turn interpreter now has only two semantic outcomes:

1. return plain conversational text; or
2. call a fieldless hidden `atlas_delegate_to_claude` tool when Daniel is asking Atlas to do work.

The model does not supply an application, URL, tool, permission, route, or execution plan. The host
converts that signal into one fixed `claude.connected` request and stores Daniel's exact transcript
as the encrypted slow-task payload.

The standalone subscription worker launches an isolated background Claude Code session with normal
user integrations enabled: `--chrome`, `--setting-sources user`, `--permission-mode auto`, and
`--tools default`. It does not use `--safe-mode`, `--no-chrome`, or `--strict-mcp-config`. Metered
provider environment variables and generic credential-shaped environment values are removed before
launch. Atlas does not read, print, copy, or persist Claude, Chrome, Google, MCP, or plugin secrets.

Each connected job receives an isolated directory under `%LOCALAPPDATA%\Atlas\connected-jobs`.
Claude receives the exact voiced request plus a host-owned result-frame contract. Atlas alone owns
the durable claim and terminal transition. Completed results are projected into History and are
reported back into the live transcript; active work remains in Workers only while it is active.

## Failure handling corrected

The first real smoke exposed two boundary defects:

- A deliberately mismatched Windows DPAPI test payload failed safely but restarted the subscription
  worker. The process loop now recovers from a supervisor error without terminating or exposing the
  protected payload.
- `claude logs` is a terminal redraw stream. It repeated the successful frame many times and echoed
  the prompt's schema example, so the strict parser reported `subscription_result_invalid`. Result
  parsing now removes ANSI rendering, collapses byte-identical redraws, ignores only the exact
  non-result schema template, and still rejects distinct/conflicting frames, wrong job IDs, unsafe
  artifact paths, malformed JSON, oversized data, and invalid nonces.

These are transport-boundary corrections; no action-specific route was added.

## Adversarial findings

- The voice process still uses its configured conversational model for short dialogue. Connected
  execution—not every conversational token—runs through the subscription Claude Code environment.
- User-scoped Claude integrations are inherited. Project-local settings from arbitrary other
  directories are intentionally not guessed or copied into job workspaces.
- A connection unavailable to Claude must produce an honest failed result and useful next step;
  Atlas must never substitute a success sentence.
- Current `waiting/needs_input` background sessions fail with `subscription_needs_input`. A true
  resumable human gate and embedded live terminal remain future run-surface work.
- The smoke deliberately used no tools or external services. Chrome navigation, Google Drive
  mutation, and a heavy research workflow were not exercised merely to test plumbing.

## Verification

- Connected supervisor and worker-health regression suite: **38 passed**.
- Final conversation/delegation regression suite: **45 passed**.
- Real subscription smoke: queued -> running -> **succeeded**, summary
  `ATLAS_CONNECTED_SMOKE_OK`; no tool or external service used.
- Full standalone suite: **406 passed**, with one unchanged pinned MCP/Pydantic warning.
- Python compilation, dependency consistency, JavaScript syntax, and whitespace checks: passed.
- The live `atlas-subscription` and `atlas-worker` PM2 processes are online; local health reports
  `available`, the smoke is terminal, and there are no active jobs.
