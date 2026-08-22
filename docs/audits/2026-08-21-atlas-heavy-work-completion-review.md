# Atlas heavy-work completion review

**Date:** 2026-08-21  
**Authoritative branch:** `codex/atlas-live-premerge-20260820`  
**Independent application:** `C:\Users\danie\Atlas`  
**Verdict:** account-free implementation ready; live integrations remain explicitly gated

## Outcome

Atlas now runs every production heavy job through a Fable-led, host-bounded execution envelope.
Standard work may delegate bounded reasoning or drafting to fixed Haiku/Sonnet roles and finish in
one candidate generation. Knowledge work adds authenticated broker evidence and a fresh Opus review.
Build work returns a reviewed encrypted private draft. Combined work keeps both evidence and review
floors. The host owns model selection, capabilities, budgets, evidence correlation, candidate digest,
terminal state, and private storage.

This is intentionally not an external code editor. `code.change` fails with
`external_workspace_not_activated`; arbitrary Windows file writes remain unavailable until a
separate confinement proof and human activation review exist.

## Goal-state cases

| Case | Host behavior | Evidence |
| --- | --- | --- |
| Small standard task | One Fable candidate; optional fixed Haiku/Sonnet delegation; no forced Opus turn | `test_standard_profile_uses_fable_agent_envelope_and_completes_without_forced_opus` |
| Knowledge-heavy task | Fable author and fresh Opus reviewer each meet independent broker-evidence minimums | `test_knowledge_profile_runs_fable_then_fresh_opus_and_encrypts_passed_answer` |
| Private build draft | Fable plus fixed agents have no source/file/shell tools; fresh tool-less Opus reviews exact digest | `test_build_profile_produces_private_draft_then_fresh_opus_review_without_file_tools` |
| Knowledge plus artifact | Evidence and artifact/review floors are joined rather than downgraded | `test_combined_profile_keeps_both_evidence_and_artifact_review_floors` |
| Iterative rework | Review can launch a new Fable generation; maximum three generations | `test_knowledge_review_rework_launches_new_fable_generation_then_new_opus` |
| No progress | Repeated candidate digest parks/fails instead of spinning | `test_repeated_rework_candidate_parks_as_no_progress` |
| Missing/stale evidence | Unobserved, duplicate, insufficient, wrong-job, or wrong-generation evidence cannot pass | knowledge workflow, broker IPC, and supervisor adverse tests |
| Cancellation/deadline/restart | Child is stopped, broker closes, claim fencing remains authoritative, deterministic sessions reconcile | supervisor cancel/deadline/restart tests |
| External code edit | Rejected before any Claude launch | `test_code_change_stays_fail_closed_until_external_workspace_is_activated` |
| Private result use | Public jobs expose only `result_available`; paired UI opens/downloads encrypted result | protected-result and state-server tests |

## Adverse reviews

### Code review

Two material findings were fixed during review:

1. Production `standard-heavy` still used the legacy one-shot Fable path with delegation disabled.
   It now uses the same isolated explicit-agent envelope as other heavy work and retains its
   one-generation finish rule.
2. Windows could convert early broker 403/410/429 responses into `WSAECONNABORTED` because the
   server closed before consuming a bounded request body. The server now drains authenticated
   bounded requests before terminal rejection and gives invalid bearers only a 250 ms drain window.
   The complete broker suite then passed 20 consecutive runs (80 tests).

No unresolved high- or medium-severity correctness finding remains in the activated scope.

### Security review

- Claude receives no lease token, JobStore handle, account credential, or metered API fallback.
- Model names, tool surfaces, subordinate roster, permission mode, settings sources, MCP config,
  budgets, and reviewer launch are host-fixed.
- The knowledge bearer is short-lived, job-bound, capability-scoped, request-bounded, hidden from
  repr and argv/MCP JSON, and accepted only on an exact `127.0.0.1` endpoint.
- Observation content and model logs are not persisted to public jobs/events/receipts. Passed output
  is encrypted under result-specific job/request entropy; public history contains a fixed marker.
- Private results require the in-memory paired bearer and `Cache-Control: no-store`; the client uses
  text nodes, does not use cookies/localStorage/sessionStorage, and clears cached results on 401.
- External source strings, prior candidates, and reviewer subjects are explicitly treated as
  untrusted data. Mutations never enter the evidence MCP surface.

No unresolved high- or medium-severity security finding remains. Live OAuth, browser, desktop,
external files, voice/device use, paid Claude launch, and remote hosting remain human gates.

### Loop-design review

- **Spinning/token burn:** wall time, evidence-call budget, three-generation cap, cancellation, and
  repeated-digest no-progress stop are host-enforced.
- **Goodhart verifier:** author and reviewer use separate sessions and evidence ledgers; reviewer is
  bound to the exact candidate digest; host schema/digest/evidence checks cannot be self-awarded.
- **Wrong answer to completion:** strong profiles require fresh review and evidence. Subjective
  correctness remains model judgment and consequential external activation remains human judgment.
- **Self-review:** Fable cannot select or launch the Opus reviewer; the host launches it without
  Agent or mutation tools.
- **Wrong workflow:** deterministic request signals join knowledge and artifact floors and reject a
  weaker named profile.
- **Fallback:** missing capability, evidence, input, status, time, progress, or external authority
  parks/fails with a fixed public code instead of silently downgrading.

The loop is decidable at its host boundary: schema, nonce/job correlation, evidence receipts,
candidate digest, budgets, and terminal transition are machine-checkable. Human judgment is kept for
credentials, live services, external mutations, and deployment.

## Verification

- Authoritative Atlas suite: **474 passed**, excluding only kb dirty-worktree `test_preflight.py`.
- Independent `C:\Users\danie\Atlas` suite using its own new `.venv`: **398 passed**.
- Broker IPC stress: **20 complete suite runs / 80 tests passed** after the Windows drain fix.
- Dependency consistency: `pip check` passed in both environments.
- Static checks: `node --check atlas/ui/app.js` and `git diff --check` passed.
- Claude Code 2.1.238 help confirms the required `--bg`, `--agents`, `--strict-mcp-config`,
  `--setting-sources`, `--disallowedTools`, `--permission-mode`, and `claude-fable-5` surfaces.
- Real standalone local-host smoke: headless `worker.ui_server` bound at
  `http://127.0.0.1:44360/`, root returned 200, `/state` returned `ASLEEP`, `/jobs` returned an empty
  list, and the smoke process was stopped cleanly.
- The recurring MCP test warning is Pydantic's `IncompleteFieldDefinitionWarning` inside the pinned
  MCP dependency; it does not fail the suite.

## Human gates

No paid/background Claude task, voice/audio session, OAuth consent, Google credential broker,
signed-in browser pairing, desktop alias, external file mutation, Internet deployment, commit,
merge, or push was performed. The optional remote hosted presentation remains disabled by design;
the application hosts locally on loopback.
