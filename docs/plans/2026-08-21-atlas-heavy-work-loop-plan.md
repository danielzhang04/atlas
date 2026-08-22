# Atlas heavy-work loop build plan

**Status:** implemented and verified with a minimal live subscription smoke; integrations remain gated
**Date:** 2026-08-21  
**Scope:** independent local Atlas application; no kb runtime dependency

## Architecture correction

Claude's existing Fable-led agent loop is the inner work engine. Atlas does not recreate planning,
iteration, or ordinary subagent scheduling turn by turn. It supplies a concise kb-derived operating
doctrine and a host-owned roster: Haiku research scout, Sonnet worker, and a separately launched Opus
reviewer. Fable may dispatch the working roles; the host, not Fable, triggers any required final
review.

The controller below remains the outer envelope: it selects the workflow floor, binds models and
tools, meters time/calls, accepts broker receipts, correlates artifact/evidence generations, and
decides whether a final result may be accepted. Its directive format is available for host/broker
boundaries, not intended to reproduce Claude's internal reasoning loop.

## Decision

Atlas will launch one Fable-led heavy-work loop under a host-controlled execution envelope. It will
not have separate research, artifact, editing, or review loop engines. Task differences are expressed
through an immutable execution profile selected by trusted host policy before work starts.

`smallest satisfactory` governs the size of each coherent change and each delegated brief. It does
not authorize a model to downgrade its tier, skip declared workflow stages, remove acceptance
conditions, avoid independent review, or call a knowledge-heavy workflow complete without its
required evidence.

## Two layers

### Mechanical loop

One controller accepts four typed directives: `complete`, `call`, `ask_user`, and `fail`.
Delegation, artifact work, reading, editing, and verification are closed capabilities requested
through `call`. Each accepted directive is bound to the job, actor role, step, state hash, nonce,
and remaining host budget.

### Execution doctrine

The host locks an execution profile containing:

- coordinator role and model;
- allowed subordinate roles and their verified model bindings;
- allowed capabilities per role;
- file-read, file-edit, and review surfaces;
- required evidence and review gates;
- model-turn, capability-call, delegation, retry, no-progress, byte, and wall-time bounds;
- human-only decisions and terminal authority.

The model may request a role such as `research-scout`, `worker`, or `reviewer`. It never supplies a
model name, runtime, permission mode, tool list, or authority. Atlas resolves those from the locked
profile.

## Initial profiles

1. `standard-heavy`: Fable 5 coordinator. It may finish in one turn when no stronger gate applies,
   or delegate bounded work to verified lower-tier roles.
2. `knowledge-heavy`: Fable 5 coordinator, bounded research-scout fan-out, evidence minimums, and a
   fresh-context synthesis/review gate. A first-turn unsupported completion is rejected.
3. `build-review`: Fable 5 coordinator, scoped worker role, file-edit discipline, deterministic
   checks, and an independent read-only reviewer. The producing role cannot review its own output.
4. `knowledge-build-review`: the combined evidence, artifact, deterministic verification, and
   fresh-review floor. Mixed research-plus-build work must not lose either half of its doctrine.

These are data policies over the same controller, not controller subclasses.

## Model policy

- Coordinator/orchestrator: `claude-fable-5`.
- Mechanical scout: `claude-haiku-4-5`.
- Standard subordinate worker/drafter: `claude-sonnet-5`.
- Independent sensitive reviewer: `claude-opus-5`.

Only host policy may change these bindings. Atlas has no automatic fallback and no model-selected
downgrade. All heavy roles remain subscription-authenticated Claude background sessions; the
bounded voice interpreter remains a separate API call.

## File-edit and review policy

- Read the target, relevant neighbors, and tests before editing.
- Edit only the subsystem and paths named by the locked work plan.
- Use one serial owner for shared integration files.
- Preserve and strengthen acceptance tests; never delete, weaken, skip, or bypass them.
- Make the smallest coherent change that completes the declared stage, not the smallest change that
  permits early termination.
- The author cannot issue the independent review verdict.
- Deterministic verification wins over model assertion. Missing evidence parks the job.
- Subjective completion becomes `ready_for_review`; consequential effects remain human-confirmed.

## Build sequence

- [x] Extract concise kb loop, delegation, file-editing, and review doctrine without kb machinery.
- [x] Define host-owned Haiku scout and Sonnet worker declarations plus a host-only Opus reviewer.
- [x] Add an inactive controlled explicit-agent launch specification: project-only settings,
      strict MCP, disabled slash commands, explicit tools plus denials, isolated-workspace checks,
      Fable coordinator, and host-fixed Haiku/Sonnet roster.
- [x] Add a separate host-triggered Opus launch specification with no Agent or mutation tools.
- [x] Prove the controlled launch against a real subscription session. A minimal fixed-answer
      standard-heavy job completed through Claude Code 2.1.238, with the encrypted answer exposed
      publicly only as `result_available`. No research, reviewer, connector, or external mutation
      was activated.
- [x] Let production Fable run the inner plan/delegate/iterate/check cycle through the Agent tool;
      standard work can finish after one candidate, while stronger profiles retain their floors.

- [x] Add immutable role and execution-profile contracts.
- [x] Add the four typed directives and bounded loop state.
- [x] Implement finish gates, turn/call/delegation/wall-time/byte budgets,
      repeated-call/no-progress damping, and pause outcomes.
- [x] Represent delegation as the closed `agent.dispatch` capability with host model resolution.
- [x] Bind explicit and deterministically recognized heavy launches to a host policy floor; mixed
      knowledge-plus-build requests select the combined profile.
- [x] Adapt the subscription supervisor to host the Fable session lifecycle and its fixed roster.
- [x] Add a bounded private observation contract for reviewed reads; filter credential-shaped
      fields, digest the exact model input, truncate oversized data, and forbid mutation results.
- [x] Put the read-observation broker behind authenticated, expiring, request-bounded loopback IPC
      for Claude's strict single-tool MCP child.
- [x] Add a safe private-artifact slice: complete drafts are encrypted and paired-viewable. External
      filesystem/code edits stay blocked until their confinement boundary is proven.
- [x] Add fresh Opus reviewer turns, exact candidate-digest binding, host evidence checks, and
      deterministic schema/correlation/storage verification.
- [x] Persist only fixed public status/availability metadata plus encrypted private payloads;
      broker observation content and model logs are not written to public job or receipt surfaces.
- [x] Test shortcut refusal, model spoofing, self-review, scope widening, missing evidence,
      no-progress, exhaustion, cancellation, stale frames, and restart behavior.
- [x] Run the final code, security, and loop-design review record before live subscription
      activation. Live activation remains a human gate.

## Acceptance

The slice is acceptable only when tests prove that a knowledge-heavy profile cannot complete before
its evidence/review gates, a model cannot choose or downgrade a model tier, a worker cannot review
its own artifact, repeated identical work parks, and simple standard-heavy work can still complete
in one turn.
