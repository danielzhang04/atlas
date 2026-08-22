# Atlas run surface plan

**Date:** 2026-08-21  
**Status:** design contract for incremental implementation  
**Scope:** local subscription work, deliberate human gates, and future VM kb work

## Product decision

Atlas is the conversational control plane. Executors do work; they do not establish separate,
uncoordinated relationships with Daniel.

1. Atlas clarifies the request before dispatch whenever a missing choice would materially change
   the work.
2. Atlas shows the proposed run shape when the work contains planned human gates, consequential
   actions, or governance requirements.
3. Drafting, evidence gathering, ordinary review, and bounded rework remain inside the worker unless
   the run was explicitly designed to pause for Daniel.
4. A worker that unexpectedly needs input emits a typed gate request. Atlas presents that request in
   the originating run tab and may also speak it when Atlas is engaged.
5. Daniel's answer is correlated to the exact run and gate. The worker does not accept unrelated
   terminal text as authority.

The raw CLI is an observability and expert-recovery surface. Normal clarification and approvals use
an Atlas-owned gate card. A separately reviewed one-use control grant may later allow direct terminal
input for exceptional recovery.

## One run model, multiple executors

Every unit of work has a stable Atlas run ID and an executor kind:

- `local_subscription`: a local Claude subscription session with an attached PTY stream.
- `kb_workflow`: a named workflow launched on VM kb; its stream is normalized workflow events.
- `kb_agent`: an individual kb agent; it may expose agent events and, when available, an attached PTY.
- `host_action`: a short host-owned capability action with proposal and receipt rather than a shell.

The Workers pane is adapter-driven. It does not pretend every run is a terminal. A local subscription
run selects the terminal renderer; a kb workflow selects a stages/agents renderer; a host action
selects a proposal/receipt renderer. All renderers share tab, state, gate, notification, and history
semantics.

## Lifecycle

The target public lifecycle is:

`drafting -> awaiting_dispatch -> queued -> running -> awaiting_input | awaiting_approval |
awaiting_review -> running -> succeeded | failed | cancelled | unavailable`

The existing standalone store supports only queued/running/terminal job states. The first UI slice
therefore displays only those truthful states. Adding resumable waiting states requires a reviewed
JobStore migration and supervisor continuation contract; a failed job code must not be relabeled as
a resumable gate.

### Active-tab behavior

- Queued, running, and any future waiting states appear as tabs.
- Each separate task has one tab even when its executor creates subordinate agents.
- Subordinate agents appear inside the run renderer unless explicitly promoted to Atlas runs.
- Planned and unexpected gates show a numeric badge on the run tab, the Workers heading, and the
  global header.
- Resolving the final open gate removes all three badges automatically.
- Successful, failed, cancelled, orphaned, and unavailable runs leave Workers automatically and
  remain available in History with their final result or failure code.
- The UI may animate a successful tab closed after a short grace period, but completion never
  depends on the browser being open.

## Stream contract

Each executor adapter emits monotonically sequenced, bounded frames:

```text
run_id, sequence, timestamp, executor_kind, frame_kind, stage, summary, attention_count
```

Allowed frame kinds are `state`, `output`, `progress`, `agent`, `gate`, `artifact`, and `receipt`.
Terminal bytes use a separate ephemeral stream and are never copied into public job rows. The host
must bound frame size and rate, remove unsafe control sequences, apply secret redaction before
broadcast, and provide a reconnect cursor. Browser clients receive no subscription credential,
Claude auth state, VM credential, or unrestricted shell authority.

## Adapter behavior

### Local subscription

The supervisor owns the Claude PTY. It publishes sanitized terminal frames and typed stage changes
over a loopback WebSocket. xterm.js renders the stream with the fit addon. The initial surface is
read-only. Resumable input and direct terminal control are later, separately reviewed capabilities.

### VM kb workflow

The dormant kb bridge targets a reviewed Atlas adapter rather than importing kb modules. It launches
a named workflow, maps the remote workflow ID to the Atlas run ID, and subscribes to normalized
workflow/agent events. The renderer shows stages, active agents, progress, logs, artifacts, and gates.
If kb exposes a real PTY for one agent, that agent can open a nested terminal view; Atlas does not
manufacture terminal output when none exists.

## Implementation slices

- [x] Correct active-tab semantics in the existing UI: only active jobs stay in Workers; terminal
  jobs move to History.
- [x] Surface existing Atlas action proposals as clearing attention counts in Workers and the global
  header.
- [x] Admit host-fixed contextual setup guides from paired UI controls into the same durable
  subscription queue. Their completion closes the run, but only the external readiness projection
  clears the originating notification.
- [ ] Add a versioned `RunProjection` and `RunFrame` contract without changing executor authority.
- [ ] Add resumable, typed gate records and one-use correlated responses.
- [ ] Add local supervisor PTY streaming and an xterm.js renderer.
- [ ] Add the separately reviewed, dormant kb bridge package and workflow renderer.
- [ ] Add completion grace animation, explicit history retention, and acknowledgement policy for
  terminal failures.

## Non-goals for the visual slice

The Atlas Engine is state- and loudness-reactive. The local wake listener reduces each 80 ms frame
to one bounded ephemeral scalar; raw samples, frequency bins, and signal history do not enter the
page or state history. This is a loudness visualization, not a frequency analyzer.
