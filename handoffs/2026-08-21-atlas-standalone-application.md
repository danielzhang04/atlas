# Atlas standalone application handoff — 2026-08-21

**Status:** minimal subscription smoke and command-center redesign passed; live integrations remain human-gated
**Independent application:** `C:\Users\danie\Atlas`  
**Authoritative source:** `C:\Users\danie\kb\_private\codex-worktrees\atlas-live-premerge-20260820`  
**Branch:** `codex/atlas-live-premerge-20260820`

## Context

Atlas is now a physically independent, local-first application with no kb runtime import. The
standalone tree has its own `.venv`, installed dependencies, wake-word feature assets, UI, durable
job store, capability broker, and subscription-only heavy worker. Nothing was committed, pushed,
merged, remotely deployed, paired to a signed-in browser, bound to OAuth/desktop/file authority, or
launched as a paid Claude background task.

## What worked

- Production heavy execution now uses one Fable-led doctrine rather than separate task engines.
  Standard work may use fixed Haiku/Sonnet subagents and complete in one generation. Knowledge and
  build profiles add only the stronger evidence/review stages they require.
- Knowledge and combined workflows use a short-lived authenticated loopback MCP broker. Author and
  fresh Opus reviewer must cite separate exact read receipts; mutation capabilities are absent.
- Build workflows produce complete encrypted private drafts and receive a fresh tool-less Opus
  review. `code.change` fails before launch because external workspace writes are not activated.
- Rework is bounded to three generations. Repeated candidate digests, missing evidence, unknown
  session state, deadline exhaustion, cancellation, and restart all terminate or reconcile without
  an unbounded loop.
- Passed results are atomically encrypted separately from the public job/event projection. The
  paired loopback UI can open and download them using a bearer held only in page memory.
- The UI-only host now attaches to the same durable job store as the voice worker and supports
  `--no-browser` for headless local hosting checks.
- The standalone app was freshly installed, including openwakeword feature models, and a real
  headless local-host smoke returned root 200, `ASLEEP` state, and an empty jobs list.

## Adverse findings fixed

- Standard-heavy production work still forbade delegation; it now uses the explicit Fable agent
  envelope while retaining one-turn completion when sufficient.
- Early broker errors intermittently became Windows socket aborts. Bounded request bodies are now
  consumed before authenticated terminal rejection; the broker suite passed 20 consecutive runs.
- The first standalone sync copied the kb pytest bootstrap and one kb-only integration test. The
  standalone bootstrap was restored and the two new copied artifacts removed. Their originals
  remain in the authoritative worktree.
- A fresh `.venv` lacked openwakeword's shared feature models. They were downloaded into the
  independent environment and the own-interpreter suite then passed.

## Verification evidence

- Authoritative Atlas tests: **474 passed**, one dependency warning.
- Independent Atlas tests using `C:\Users\danie\Atlas\.venv`: **398 passed**, one dependency warning.
- Broker stress: **80/80 passed** across 20 repeated complete broker-suite runs.
- `pip check`: passed in both environments.
- `node --check atlas/ui/app.js`: passed.
- `git diff --check`: passed.
- Claude Code 2.1.238 exposes the exact required background, agent, MCP, settings, denial, model, and
  permission flags. No live session was launched.
- Review record: `docs/audits/2026-08-21-atlas-heavy-work-completion-review.md`.

## Use now

From `C:\Users\danie\Atlas`:

```powershell
.venv\Scripts\python -m worker.ui_server
```

This opens the locally hosted command center at `http://127.0.0.1:4360/` with one-time fragment
pairing. To serve without opening a browser, add `--no-browser`.

The human-gated subscription worker is:

```powershell
.venv\Scripts\python -m worker.subscription_cli --confirm-subscription-auth
```

Run it only after confirming the local Claude CLI is subscription-authenticated and the process
environment contains no metered-provider credential. The attestation admits new jobs for five
minutes; already running work retains its own bounded deadline.

## Human gates / not attempted

- First paid subscription background smoke.
- Voice microphone/speaker/provider smoke.
- Browser bridge installation and signed-in pairing.
- Google OAuth and external credential broker.
- Desktop aliases and external repository/file editing.
- Visual browser QA and optional Internet-hosted presentation.
- Commit, push, merge, or deployment.

These are deliberate activation gates, not hidden fallbacks. Missing sources fail
`knowledge_sources_unavailable`; external code edits fail `external_workspace_not_activated`.

## Load list

- `CLAUDE.md`
- `governance/agent-rules.md`
- `orgs/atlas-prep/contract.md`
- `orgs/atlas-prep/STATE.md`
- `handoffs/2026-08-21-atlas-standalone-application.md`
- `docs/audits/2026-08-21-atlas-heavy-work-completion-review.md`
- `docs/plans/2026-08-21-atlas-heavy-work-loop-plan.md`
- `atlas/README.md`
- `atlas/worker/subscription_supervisor.py`
- `atlas/worker/agent_logic.py`
- `atlas/worker/broker_ipc.py`
- `atlas/worker/knowledge_mcp.py`
- `atlas/worker/knowledge_workflow.py`
- `atlas/worker/stateserver.py`
- `atlas/worker/ui_server.py`

## Original exact next step — completed

Daniel chooses whether to run the first live subscription smoke. Use a disposable standard-heavy
task first, then a source-backed knowledge task only after a reviewed browser or Google read adapter
is deliberately paired. Do not activate external file writes as part of that smoke.

## Continuation update — provider-environment hardening

The standalone takeover re-ran the account-free baseline and found one activation-boundary gap:
Claude Code's documented Foundry and Bedrock Mantle selectors were not named by the child-environment
scrubber, and the admission check did not reject any provider selector. The worker now centralizes
the documented metered-provider variables, rejects nonblank selectors case-insensitively before
startup, and independently strips the complete set from Claude child environments.

- Focused activation/supervisor tests: **31 passed**.
- Full standalone suite: **398 passed**, one unchanged dependency warning.
- `pip check`, `node --check ui/app.js`, and whitespace checks: passed.
- Review: `docs/audits/2026-08-21-atlas-provider-environment-hardening-review.md`.

No Claude background task, auth-state inspection, browser, voice, OAuth, external mutation, commit,
push, merge, or deployment occurred. The exact next step above is unchanged and remains human-gated.

## Live subscription continuation

Daniel authorized a light subscription test. The final isolated standard-heavy job
`e8b7d227-4762-4c46-868c-ab22bc40f38e` completed `queued -> running -> succeeded` and returned the
DPAPI-protected answer `ATLAS_SUBSCRIPTION_SMOKE_OK`. Its public row contained only
`{"result_available":true,"summary":"Private result available."}`. The session finished `done`,
and the isolated worker was stopped with health `unavailable:worker_stopped`.

Live validation exposed and fixed cold-start timeout, CLI-assigned background IDs, restart cleanup,
UTF-8 log decoding, ANSI terminal redraws around typed frames, and mutable-state collision with an
already running Atlas checkout. Explicit `--job-store`, `--health-file`, and
`--agentic-workspace` overrides now support a truly isolated smoke. A recurring Windows 413 socket
abort was also fixed by draining authenticated oversized bodies through a separate 16 KiB rejection
cap.

- Final standalone suite: **405 passed**, one unchanged dependency warning.
- Broker stress after the final drain fix: **80/80 passed** across 20 complete runs.
- `pip check`, `node --check ui/app.js`, and whitespace checks: passed.
- Review: `docs/audits/2026-08-21-atlas-live-subscription-smoke-review.md`.

Five tiny fixed-answer Claude sessions were launched while correcting live-only compatibility gaps;
no source research, Opus review, connector, browser, voice, OAuth, external mutation, commit, push,
merge, or deployment occurred. One queued row accidentally created in the pre-existing shared store
was cancelled; the other shared-store attempts were already terminal. The disposable smoke database
and health file were removed. Two empty job-workspace directories remain locked by completed Claude
processes under `.live-smoke-20260821/agent-jobs/`; they contain no files and are ignored until the
process handles release.

## Exact next step

Do not cross another human gate while Daniel is away. After he returns, choose whether to pair a
reviewed browser or Google read adapter before attempting a source-backed knowledge smoke. External
file writes remain unavailable.

## Command-center continuation

Daniel returned and reviewed the local UI. The home surface was rebuilt around the live Atlas core,
Conversation, and tabbed Active work panes. Marketing/title copy, polling language, summary cards,
and the footer were removed. Sources, History, and Settings now live in compact top navigation.
Runtime issues use aggregate and per-item numeric badges that disappear when the reported condition
clears. Settings contains click-in guides naming the local files and keys governing each area and
now explains action pairing as a one-runtime UI trust proof rather than a connector.

The reported wake-state mismatch was real: worker.ui_server owned an independent idle publisher
while the voice worker on 4360 handled speech. State surfaces now identify themselves as voice,
observer, or mirror. The paired command center currently running on 4361 mirrors the bounded live
state from 4360, so it shows the actual transcript and wake cycle without starting another
microphone/STT path. Mirror loss fails unavailable rather than presenting a false sleeping state.

Active subscription jobs render as selectable terminal-style tabs backed by a new fixed public job
event projection. Raw Claude terminal output remains private. A new fixed health projection drives
the subscription-worker badge and treats stale heartbeats as unresolved.

- Focused command-center/state tests: **36 passed**.
- Full standalone suite: **409 passed**, one unchanged dependency warning.
- JavaScript syntax, dependency, and changed-file whitespace checks: passed.
- Review: docs/audits/2026-08-21-atlas-command-center-redesign-review.md.

No connector, OAuth account, browser bridge, external file authority, deployment, paid task, or new
Claude subscription session was activated. The browser-control plugin rejected its own cached
service path, so visual acceptance remains Daniel's check in the opened local browser.

## Exact next step — command center

Daniel visually reviews the opened 4361 page and tests one wake/speak cycle. Then choose the next
bounded UI slice: validated configuration add/edit/delete, richer redacted subscription telemetry,
or a true ephemeral voice-energy signal. Browser/Google pairing and external file writes remain
separate activation gates.

## Atlas Engine and run-surface continuation

Daniel rejected the lettered status orb, teal-heavy palette, literal A/orbit logo, and ambiguous
Conversation/Active work terminology. The home surface now uses a custom Canvas Atlas Engine with
segmented radial frequency bars, rotating telemetry arcs, a deforming signal ring, particles, and a
folded-map core. State changes select a restrained blue, violet, green, warm-orange, or neutral
imprint while the product shell stays black, white, and grey. The compact brand mark uses matching
folded-map geometry. The lower panes are named Transcript and Workers.

Workers now contains only current queued/running/cancel-requested jobs. Terminal jobs automatically
leave it and appear in History, including paired access to protected results. Existing action
proposals contribute clearing Workers/global attention badges. The global alert routes to home when
worker attention exists.

The clarification, human-gate, local terminal, and future VM kb behavior is specified in
`docs/plans/2026-08-21-atlas-run-surface-plan.md`. Atlas is the conversational control plane:
clarification happens before dispatch; an executor can request input only through a typed,
run-correlated gate. Local subscription sessions will use real PTY streaming. VM kb workflows will
use normalized workflow/agent streams and show a terminal only when a real remote PTY exists.

- Focused UI/state tests: **31 passed**.
- Full standalone suite: **410 passed**, one unchanged dependency warning.
- JavaScript syntax, dependency consistency, whitespace, and live loopback asset checks: passed.
- Review: `docs/audits/2026-08-21-atlas-engine-and-run-surface-review.md`.

No paid/background task, connector, OAuth flow, VM bridge, external mutation, deployment, commit,
push, or merge occurred. The visual is state-reactive but does not claim raw microphone frequency;
real voice energy remains a future bounded ephemeral signal. Automated browser QA is still blocked
by the bundled browser plugin's trusted-cache-path rejection.

## Exact next step — Atlas Engine

Refresh the running 4361 page and visually tune the Atlas Engine with Daniel. After the design is
accepted, implement versioned run/frame projections and resumable gate records before xterm/PTY or
VM kb adapter work.

## Audio-reactive and guided-setup continuation

Daniel's "How's it going?" report was not an immediate intentional sleep. The worker log showed the
older 4360 process invoked `queue_summary` at 19:08:31, blocked until 19:08:57, and then hit a TTS
timeout. The real two-minute sleep occurred at 19:11:01. PM2 was still targeting
`C:\Users\danie\kb-worktrees\atlas\atlas`; that process was removed and replaced with the verified
independent `C:\Users\danie\Atlas` worker. The new standalone 4360 surface and the 4361 mirror are
healthy. PM2's saved target now points at the standalone shim.

The Atlas Engine now follows real microphone loudness. The local wake loop converts each existing
80 ms frame to a bounded 0–1 scalar, retains only the latest number, publishes zero while asleep,
and exposes it through loopback `/signal`. Raw audio and frequency data never enter the UI or state
history. Canvas attack/decay smoothing drives the radial bars and inner signal while Atlas listens
or speaks.

The palette was collapsed to neutral chrome plus one Atlas purple. Amber remains attention-only and
red failure-only; green/blue role noise was removed. The home-brand underline is gone. The compact
diamond/core mark is now shared by the header and `ui/favicon.svg`.

Unready Sources and the Voice/Subscription settings now expose `Guide me`. A paired, same-origin,
fixed-ID request admits a host-authored contextual setup task into the same encrypted durable slow
queue used by ordinary Atlas work. The browser cannot provide its prompt or permissions. The task
auto-leaves Workers at terminal state, but its originating badge clears only when the real bounded
health/capability projection reports ready. Current guides return a private walkthrough; interactive
PTY and resumable questions still depend on the planned run/frame and correlated-gate slice.

- Focused suite: **71 passed**.
- Full standalone suite: **417 passed**, one unchanged dependency warning.
- JavaScript syntax, dependency, whitespace, live state/signal/mirror/favicon, and PM2 checks:
  passed.
- Review: `docs/audits/2026-08-21-atlas-audio-reactive-guided-setup-review.md`.

No subscription job, connector, OAuth flow, VM bridge, external mutation, deployment, commit, push,
or merge occurred. The subscription supervisor was not auto-started from the UI because doing so
could claim unrelated queued work and would cross its explicit activation boundary.

## Exact next step — audio-reactive Atlas

Daniel tests one wake, one lightweight conversational reply, and live loudness response in the
refreshed 4361 page. Then visually tune the energy mapping if needed and implement the versioned
run/frame plus resumable gate contract before xterm/PTY work.

## Conversational-boundary correction

Daniel's 19:35 transcript showed correct STT followed by `I couldn't safely understand that` for
every ordinary turn. The worker logs proved four immediate Anthropic HTTP 400 responses. The
standalone interpreter had forced all dialogue through a large strict tool schema containing
unsupported structured-output constraints; its injected unit tests never exercised provider schema
compilation. The sleep reflex still worked because it bypassed the model call.

Atlas now uses Claude as an actual conversational interface. Ordinary replies are bounded plain
text with six recent exchanges of in-memory context. Only a real request to do work produces the
small hidden `atlas_route_work` proposal. Host code converts that proposal to the bounded Request,
chooses FAST/SLOW, admits it durably, and returns bounded route facts to Claude for natural spoken
narration. Raw phrase matching no longer turns a clarification or explanation into a hidden job.

The old `couldn't safely understand/queue` dialogue is gone. A deterministic spoken fallback exists
only when the conversational model itself cannot respond. Provider diagnostics retain only error
class and numeric HTTP status. The persona now describes Atlas as a standalone conversational layer
over an invisible authoritative backend rather than the voice of kb or a scripted card menu.

- Focused conversational regressions: **42 passed**.
- Routing/front-desk/state regressions: **126 passed**.
- Full standalone suite: **421 passed**, one unchanged dependency warning.
- Dependency, JavaScript syntax, and whitespace checks: passed.
- PM2 standalone worker restarted successfully; 4360 voice state and 4361 mirror are healthy.
- Review: `docs/audits/2026-08-21-atlas-conversational-boundary-review.md`.

No paid model request, subscription job, connector, OAuth flow, VM bridge, external mutation,
deployment, commit, push, or merge occurred.

## Exact next step — conversational Atlas

Daniel says `Hello?` and one contextual follow-up to the restarted worker. Then test one explicitly
requested lightweight work route when desired; the route should stay invisible while the resulting
job/status appears naturally in Atlas and Workers.

## Continuous-conversation correction

Daniel's 20:22 live transcript isolated the remaining failure. `Just wanna chat. How are you doing?`
reached Anthropic but Atlas canceled the request at its four-second deadline. The immediate replies
afterward never reached Claude: the inherited 30-second addressed-speech window silently discarded
them. A later explicitly addressed turn returned successfully, but the inherited two-minute silence
watcher raced the reply and spoke the sleep line in the same second.

The standalone voice lifecycle now matches the intended product. The wake word opens one continuous
Claude conversation; every subsequent finalized utterance reaches Claude without another address;
and only an explicit configured dismiss phrase closes the microphone. The addressing module,
inactivity watcher, timer configuration, gratitude dismissal, and other fixed conversational intents
were removed. `that's all` and `go to sleep` remain exact host-owned sleep controls.

The conversation deadline is now ten seconds. The hidden work proposal schema is non-strict because
the existing Request/front-desk boundary is authoritative; malformed model proposals still create no
job. Real timeout, provider, and malformed-response failures now name the failed boundary and say
Atlas remains awake. Existing routing, authorizations, durable jobs, subscription supervision,
Workers projection, and receipts were preserved.

- Focused continuous-conversation regressions: **80 passed**.
- Full standalone suite: **400 passed**, one unchanged dependency warning.
- Dependency, JavaScript syntax, Python compilation, production-path scan, and whitespace checks:
  passed.
- PM2 `atlas-worker` restarted successfully from `C:\Users\danie\Atlas`; 4360 voice state and 4361
  mirror both report `ASLEEP` after reload.
- Review: `docs/audits/2026-08-21-atlas-continuous-conversation-review.md`.

No paid/background Claude job, heavy workflow, connector, OAuth flow, VM bridge, external mutation,
deployment, commit, push, or merge occurred.

## Exact next step — continuous conversation

Wake Atlas once, speak an ordinary sentence, wait more than 30 seconds, and ask a contextual
follow-up without saying Atlas again. Atlas should reply to both and stay awake until `go to sleep`
or `that's all` is spoken. After that passes, the next product slice remains the versioned run/frame
and resumable human-gate contract for live local and VM work tabs.

## Conversational prompt-contamination correction

The 21:06 acceptance transcript confirmed continuous listening but exposed unsolicited tool
inventory in replies to `Shit. Finally.` and repeated `Hey, Atlas.` turns. These were not wake hooks:
the interpreter had encoded the transcript and full capability catalog together as every user
message. Claude was reacting to internal backend context as though Daniel had just supplied it.

User messages are now the exact spoken transcript. The sanitized backend catalog moved into the
private `atlas_route_work` tool description and never enters conversation history. Persona/system
instructions now make short social turns genuinely short and prohibit reintroductions, readiness
announcements, or capability lists unless Daniel explicitly asks.

- Focused prompt/front-desk/cutover regressions: **43 passed**.
- Full standalone suite: **401 passed**, one unchanged dependency warning.
- No live model prompt, paid/background job, connector, or external mutation was used in testing.

## Exact next step — short conversational acceptance

After the worker reload, wake Atlas and try `Shit. Finally.` followed later by `Hey, Atlas.` The
expected replies are brief social acknowledgments, with no mention of tools or capabilities. The
garbled first block in the prior transcript is a separate microphone-source issue: while explicitly
awake, continuous STT cannot inherently distinguish Daniel from a nearby video. Solve that later
with a reviewed speaker/turn boundary rather than restoring silent timers or name gates.

## Direct-capability routing correction

Daniel's 22:12 acceptance transcript isolated the next contract error: ordinary conversation was
working, but every action request returned `My conversation model returned an invalid response`.
Anthropic had returned HTTP 200. The host exposed only the generic heavy-work tool and then rejected
the action-shaped tool result because it was not the 14-field durable Request schema.

Atlas now distinguishes conversation, a connected typed capability, and durable heavy work. The
first complete direct path is `Open YouTube`: `desktop.open` is exposed to Claude only while the
runtime reports it connected; only the fixed `youtube` URL alias and fixed desktop profiles can be
selected. The host validates the call, records and service-confirms one exact hash-bound proposal,
executes it once through the shared ActionBroker, and returns the terminal status to Claude for a
brief natural reply. Direct actions create no Worker job. Failed or unavailable execution is
explained as the actual capability problem rather than mislabeled as a conversational-model error.

Catalog sanitization now preserves status/detail, unsupported Spotify is no longer falsely marked
connected by an unrelated URL alias, and browser/Google remain `configuration-needed`. This does
not make the standalone worker inherit Claude CLI plugins, browser sessions, or OAuth connections.

Adversarial live-readiness review also fixed the signed Windows launcher: candidate known folders
fail over independently, and Authenticode verification passes the candidate through a scrubbed
child environment rather than appending an unquoted path to a PowerShell command. The installed
Chrome validates to `Google LLC` without launching it.

- Focused direct/cutover suite: **76 passed**.
- Full standalone suite: **409 passed**, one unchanged dependency warning.
- Python compilation, dependency consistency, JavaScript syntax, and whitespace checks: passed.
- PM2 `atlas-worker` restarted and reports online; 4360 projects `desktop.open` as connected with
  only `youtube (url)`. Browser and Google project configuration-needed.
- Review: `docs/audits/2026-08-21-atlas-direct-capability-routing-review.md`.

No heavy/subscription job, browser bridge, OAuth flow, Google action, external file mutation,
deployment, commit, push, or merge occurred. The tests did not open a real Chrome window.

## Exact next step - direct capability acceptance

Wake Atlas and say `Open YouTube`. Chrome should open the fixed YouTube alias once and Atlas should
briefly report the actual outcome. A launcher failure should produce a useful failure sentence while
Atlas stays awake, with no invalid-response wording and no Worker job.

## Connected-Claude voice bridge correction

Daniel's later acceptance transcript showed that the direct-capability design was the wrong product
boundary. It could open one fixed YouTube alias but could not navigate arbitrary sites, use the same
Chrome profile and integrations as Claude Code, or generalize to Google/MCP-backed work. Meanwhile,
the subscription launcher explicitly disabled Chrome and user MCP/settings. That architecture was
recreating Claude's tool routing in Atlas instead of making Atlas voice I/O over Claude Code.

The normal production path is now generalized. Conversation remains natural model text. Any request
to do work produces one fieldless hidden delegation signal; the model cannot invent an application,
URL, permission, route, or execution schema. Atlas stores Daniel's exact transcript in the encrypted
slow queue. The subscription worker launches a background Claude Code session with `--chrome`, user
settings, automatic permission handling, default tools, and the user's normal MCP/plugin/skill
surface. The child environment is scrubbed of metered-provider and credential-shaped environment
values, and Atlas never reads or copies connection secrets.

The earlier alias-specific `VoiceCapabilityDispatcher` path was removed from production composition,
and `desktop_target_aliases` is empty again. The Windows signed launcher remains available as a
generic host capability, but it is no longer the logic behind ordinary voice requests. Active jobs
remain in Workers, terminal jobs move to History, and the awake transcript receives a short factual
completion or failure callback.

A real tiny subscription smoke found and corrected terminal redraw parsing: `claude logs` repeats the
screen and echoes the result-schema prompt. The parser now strips ANSI rendering, deduplicates exact
redraws, ignores only the exact non-result template, and rejects conflicting frames. A malformed
protected payload can fail its job without restarting the process. The repeated smoke completed
`queued -> running -> succeeded` with summary `ATLAS_CONNECTED_SMOKE_OK`; it used no tools or external
services.

- Connected supervisor/health regressions: **38 passed**.
- Final conversation/delegation regressions: **45 passed**.
- Full standalone suite: **406 passed**, one unchanged pinned MCP/Pydantic warning.
- Python compilation, dependency consistency, JavaScript syntax, and whitespace checks: passed.
- Real light subscription smoke: **passed**; no Chrome, Google, or heavy job was run.
- PM2 `atlas-worker` and `atlas-subscription` are online; health is `available` and no jobs remain
  active.
- Review: `docs/audits/2026-08-21-atlas-connected-claude-voice-bridge-review.md`.

This section supersedes the previous direct-capability acceptance target for normal voice work. The
prior audit remains as implementation history, not the current product contract.

## Exact next step - connected voice acceptance

Wake Atlas and issue one ordinary browser request, such as `Open Nasdaq in Chrome`. Atlas should
briefly acknowledge, show one active Worker while connected Claude runs, and report the observed
outcome. Then try one request that needs an unconfigured connection; it should name the real missing
connection and useful next step. Resumable human gates and a live embedded terminal/VM run surface
remain the next architectural slice rather than more hardcoded action aliases.

## Pause checkpoint for the next terminal

**Goal state:** Atlas is voice I/O over Daniel's normal Claude Code subscription environment. Atlas
handles waking, sleeping, transcription, conversation, durable job/process state, Workers/History,
receipts, and truthful result presentation. Claude Code handles reasoning, tool selection, Chrome,
user-scoped MCP servers, plugins, skills, and multi-step execution. Do not add website/application
aliases or rebuild Claude's tool router in Python.

**Current state:** implementation and local verification are complete for the generalized bridge.
`atlas-worker` and `atlas-subscription` are running under PM2. Local health is `available`; the active
queue is empty. Normal voiced work becomes a fieldless `atlas_delegate_to_claude` signal, and the
exact transcript is encrypted in a `claude.connected` slow job. The connected launcher uses
`--bg --chrome --brief --setting-sources user --permission-mode auto --tools default`. The other
governed heavy/knowledge launch methods remain intentionally restricted; do not mistake their
`--no-chrome` flags for the connected voice path.

**Verified evidence:** real smoke job `8df262e6-ffe3-4581-a59d-4988f5e33816` transitioned
`queued -> running -> succeeded` with public summary `ATLAS_CONNECTED_SMOKE_OK`. It deliberately used
no tools or external services. Final full suite: **406 passed**, one unchanged pinned MCP/Pydantic
warning. Focused conversation/delegation suite: **45 passed**. Compilation, dependency consistency,
JavaScript syntax, and whitespace checks passed. Terminal redraw result parsing and recovery from a
single supervisor launch error are covered. No job is active at handoff.

**Not yet proven:** a real voice-triggered Chrome navigation, Daniel's preferred Chrome profile,
Google Drive/MCP access, or an external multi-step workflow. Do not describe these as accepted until
observed. User-scoped integrations must already be connected in Claude Code. Missing connections
should produce a concrete failure and useful next step, never false success or a generic
`conversation model returned an invalid response` line.

**Next terminal starts here:** read `CLAUDE.md`, this handoff section, and
`docs/audits/2026-08-21-atlas-connected-claude-voice-bridge-review.md`; check local health and confirm
the queue is empty without inspecting credentials. Then have Daniel perform one live voice acceptance:
`Open Nasdaq in Chrome`. Observe transcript, one Workers entry while active, actual browser outcome,
terminal callback, and automatic removal from Workers. Diagnose the first failed boundary if any;
do not add a Nasdaq alias. After browser acceptance, test one already-connected Google/MCP action only
when Daniel requests it. The next product slice after integration acceptance is resumable human gates
plus the embedded local/VM run surface.

**Authority and spend:** the tiny subscription smoke was authorized and consumed subscription usage,
not a metered API key. No heavy job, browser action, Google action, deployment, commit, push, or merge
was performed. Do not launch additional paid/background tests merely on the strength of this handoff;
follow Daniel's current instruction and the project authorization boundary.

## Shutdown checkpoint - 2026-08-22

Daniel requested that every live Atlas background component be cut. The two remaining idle
Atlas-connected Claude sessions (`8c816889` and `6b8659b7`) were explicitly stopped. PM2 processes
`atlas-worker` and `atlas-subscription` were stopped and `pm2 save` persisted that stopped state.
Verification found zero Atlas Claude sessions with a live PID and both Atlas PM2 entries at
`stopped`, PID 0. Historical done/stopped Claude session records remain as inert history; no files or
job history were deleted. The unrelated `kb-dashboard` was intentionally left online.

The next terminal must not assume Atlas is live. Start Atlas components only when Daniel asks, and
recheck the queue before starting `atlas-subscription` so an old queued job cannot be claimed
unexpectedly.
