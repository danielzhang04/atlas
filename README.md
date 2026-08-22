# Atlas

Atlas is a standalone, local-first voice application with a floating local command center. Claude
owns natural conversation and may optionally propose a hidden work route. The host validates and
admits that proposal to an encrypted SQLite outbox; substantial work runs only through an explicitly
started local Claude subscription worker.

Atlas does not depend on kb. The optional kb bridge is dormant and is not included in this
repository. Google, browser, desktop, OAuth, and hosted activation are unconfigured by default.

## Current safety state

- LiveKit has no autonomous LLM or tools; every finalized turn stops at the host front desk.
- Ordinary dialogue is unconstrained model text with bounded in-memory conversational context.
  Backend structure appears only when Claude proposes work; host routing and authority remain
  deterministic and invisible to the transcript.
- FAST is a narrow, host-parsed calendar grammar. Mutations become hash-bound proposals requiring
  a distinct trusted confirmation.
- SLOW payloads use Windows CurrentUser DPAPI and lease-token fencing.
- The Claude worker uses subscription-authenticated `claude --bg`, no API/SDK fallback, and a
  scrubbed environment. Production heavy work is Fable-led: standard jobs may delegate bounded
  reasoning to fixed Haiku/Sonnet roles, while knowledge/build profiles add evidence and/or a
  separately launched read-only Opus review.
- Completed heavy answers and drafts are encrypted separately from public job history. A paired
  loopback UI can open or download them; the public `/jobs` projection exposes only availability.
- Google credentials remain in a separate future local broker. Atlas contains no bearer-token API.
- Local files are unavailable; desktop/browser/Google adapters ship unpaired and inactive.

## Set up

```powershell
py -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
.venv\Scripts\python -m pytest -q
```

Put voice-provider values in `%USERPROFILE%\.atlas\env`; never commit them. Then run:

```powershell
.venv\Scripts\python -m worker.app console
```

To use the local command center without starting microphone or voice services, run:

```powershell
.venv\Scripts\python -m worker.ui_server
```

Atlas hosts this UI only on `127.0.0.1` (port 4360 by default) and opens a one-time fragment-paired
browser window. The UI never stores its bearer in cookies, local storage, or session storage.
The live Atlas Engine receives only a bounded ephemeral loudness scalar from `/signal`; raw
microphone samples and frequency bins never enter the page. Paired `Guide me` controls can admit
only host-reviewed contextual setup jobs into the normal encrypted subscription queue. A completed
guide does not clear its notification until the corresponding health/capability check reports ready.

If the voice worker already owns port 4360, a separately paired command center can mirror its live
state without opening another microphone or voice pipeline:

```powershell
.venv\Scripts\python -m worker.ui_server --port 4361 --mirror-port 4360
```

The mirror accepts only the fixed, bounded Atlas state schema from loopback. If the voice surface
is unavailable, it reports unavailable instead of presenting its own idle publisher as live state.

The separate subscription process is deliberately human-gated and is not started by PM2:

```powershell
.venv\Scripts\python -m worker.subscription_cli --confirm-subscription-auth
```

That flag is an attestation that the local `claude` CLI is using a subscription. The worker refuses
metered API credentials and provider-backend selectors, scrubs them from Claude child processes,
and expires the attestation after five minutes for new launches. Do not run it until the
subscription activation gate is intentionally approved.

For a disposable validation that must not share state with another Atlas process, override all
three mutable subscription paths together:

```powershell
.venv\Scripts\python -m worker.subscription_cli --confirm-subscription-auth `
  --job-store C:\path\to\smoke\jobs.sqlite3 `
  --health-file C:\path\to\smoke\health.json `
  --agentic-workspace C:\path\to\smoke\agent-jobs
```

External repository/file edits remain unavailable: build workflows currently return a reviewed,
encrypted private draft. `code.change` fails with `external_workspace_not_activated` rather than
claiming an edit. Enabling workspace writes requires a separate confinement and live activation
review.

## Verification

The unit suite is account-free and must not launch a browser, desktop application, paid Claude job,
or external mutation. Live voice/audio, OAuth, signed-in browser pairing, desktop activation, and
deployment are separate human gates documented in `handoffs/`.
