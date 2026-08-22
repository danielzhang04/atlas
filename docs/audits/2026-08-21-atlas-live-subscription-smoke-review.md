# Atlas live subscription smoke review

**Date:** 2026-08-21  
**Scope:** minimal standard-heavy subscription execution  
**Verdict:** live fixed-answer smoke passed; integrations and external effects remain gated

## Billing and authority boundary

Daniel explicitly authorized a light subscription test. Atlas accepted the human subscription
attestation only after its metered-credential and provider-selector guard passed. No Claude auth
state or credential file was inspected. The test used `claude --bg` through Claude Code 2.1.238 and
may have consumed included subscription usage; no API, SDK, fallback model provider, browser, voice,
OAuth, external file write, or deployment path was activated.

## Live findings fixed

1. The 20-second background-launch handshake was too short for a Windows cold start. Launches now
   have a bounded 60-second handshake; job wall-time limits are unchanged.
2. Claude Code assigns an independent background task ID rather than echoing the requested
   conversation UUID. Atlas now accepts one strict ID from launch output or resolves exactly one
   host-named session through `agents --json --cwd` inside the isolated job workspace.
3. Restart cleanup now discovers CLI-assigned active sessions by exact host name and workspace,
   stops them, skips already terminal sessions, and retains legacy deterministic fallback only when
   metadata is unavailable.
4. Windows subprocess output now decodes explicitly as UTF-8 with replacement and normalizes empty
   streams, preventing cp1252 reader-thread failure on Claude terminal output.
5. Typed candidate/review parsing now strips ANSI controls, accepts only whitespace-prefixed marker
   lines, and deduplicates identical terminal redraws. Plain duplicates and distinct redraw frames
   remain ambiguous and fail closed.
6. The default `%LOCALAPPDATA%/Atlas` state was already in use by another Atlas process. The CLI now
   supports explicit job-store, health-file, and agent-workspace overrides so validation can be
   physically isolated without stopping or modifying that process.
7. Stress testing reproduced the Windows oversized-request connection abort. Authenticated rejected
   bodies are now drained only through a separate 16 KiB cap before the 413 response; accepted
   request size remains 8 KiB.

## Final evidence

- Isolated job: `e8b7d227-4762-4c46-868c-ab22bc40f38e`.
- Lifecycle: `queued -> running -> succeeded`.
- Public payload: `{"result_available":true,"summary":"Private result available."}`.
- Protected answer: `ATLAS_SUBSCRIPTION_SMOKE_OK`.
- Evidence receipts: zero; artifact: none.
- Claude background session: `done`.
- Worker after test: `unavailable:worker_stopped`.
- Focused live-path tests: **41 passed** before the final broker correction.
- Broker stress: **20 complete runs / 80 tests passed** after correction.
- Full standalone suite: **405 passed**, one existing MCP dependency warning.
- `pip check` and `node --check ui/app.js`: passed.

Five minimal fixed-answer sessions were launched across the live-debug cycle. Earlier attempts failed
closed at DPAPI context, launch timeout, session-ID binding, UTF-8 decoding, or terminal-frame parsing.
No earlier attempt produced an accepted public result. A queued shared-store smoke row was cancelled;
all other shared-store attempts were terminal. No session or subscription worker remains active. The
disposable database and health file were deleted; two empty job-workspace directories remain locked
by completed Claude process handles and are ignored pending handle release.

## Remaining gates

Source-backed knowledge validation still requires a separately reviewed browser or Google read
adapter. Voice/audio, OAuth, desktop aliases, external repositories/files, hosted access, commit,
push, merge, and deployment remain unactivated.
