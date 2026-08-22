# Atlas Engine and run-surface review

**Date:** 2026-08-21  
**Scope:** home visual system, live-run lifecycle, notifications, and run architecture  
**Verdict:** first aesthetic/lifecycle slice complete; real streams and resumable gates remain future slices

## Outcome

The lettered status orb and teal dashboard treatment were replaced by a custom Canvas-based Atlas
Engine. The engine has concentric segmented frequency bars, independently rotating telemetry arcs,
a deforming inner signal, drifting particles, and a folded-map core. Atlas state selects both motion
character and a restrained accent while the surrounding shell remains black, white, and neutral
grey. The top-left mark now uses the same folded-map geometry rather than a literal A/orbit logo.

The lower panes are now named Transcript and Workers. Workers only retains queued, running, and
cancel-requested jobs. Terminal jobs automatically leave the active surface and appear in History,
where paired users can still open protected results. Existing action proposals contribute a clearing
badge on Workers and the global alert control. The global alert returns to the home surface when
worker attention is present.

The future interaction and adapter contract is recorded in
`docs/plans/2026-08-21-atlas-run-surface-plan.md`. Atlas owns clarification and typed gate responses.
Local subscription jobs use a genuine PTY renderer when implemented; VM kb workflows use normalized
workflow/agent events and do not masquerade as terminals.

## Review findings

- The visual uses no third-party runtime, remote asset, CDN, microphone, or audio capture. It changes
  only from the already public Atlas state and a procedural clock.
- Raw microphone frequency is not represented. Settings and the run-surface plan state this
  explicitly; a future real response requires only an ephemeral bounded energy scalar.
- The Canvas is decorative and excluded from accessibility semantics. The live state remains exposed
  through the engine's role, label, state text, and caption.
- Reduced-motion disables recurring Canvas frames and collapses CSS animation durations.
- All text from state, jobs, events, actions, receipts, and protected results is still inserted using
  text nodes. No innerHTML, eval, browser storage, WebSocket, microphone, or new external authority was
  added.
- Completed-run removal is derived from authoritative public job state. The UI does not claim current
  failed `needs_input` codes are resumable gates; that requires the planned state/store migration.
- History now preserves access to successful protected results after their active tab closes.

No unresolved high- or medium-severity correctness or security finding was found in this slice.

## Verification

- Focused UI/state suite: **31 passed**.
- Full standalone suite: **410 passed**, one unchanged dependency warning.
- First full-suite attempt exposed sandbox denial of the default Windows pytest temp root; the clean
  rerun used an Atlas-contained temporary root. That test-only directory was removed afterward.
- `node --check ui/app.js`: passed.
- `pip check`: passed.
- `git diff --check`: passed.
- Running loopback UI on port 4361 returned 200 and served the new Atlas Engine, Transcript, and
  Workers markup.
- Automated visual browser QA remains unavailable because the bundled browser runtime rejects its
  cached service path as untrusted. This is the same tool-side limitation recorded in the prior
  handoff; it is not an Atlas response failure.

## Boundaries retained

No subscription task, connector, OAuth flow, VM kb bridge, external file authority, deployment,
commit, merge, or push was launched. No real terminal streaming, resumable human gate, or remote kb
workflow execution was represented as complete.

## Next implementation slice

Daniel visually reviews and iterates on the refreshed 4361 home surface. After the visual direction
is accepted, add the versioned run/frame projection and resumable gate records before implementing
either xterm/PTY streaming or the dormant VM kb adapter.
