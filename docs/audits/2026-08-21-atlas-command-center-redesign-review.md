# Atlas command-center redesign review — 2026-08-21

**Status:** implementation and account-free verification passed; user visual review is open

## Outcome

The Atlas home surface is now a personal command center rather than a landing page:

- The title copy, product slogans, dashboard summary cards, polling copy, and footer were removed.
- Home is fixed to the viewport. Atlas occupies the upper field; Conversation and Active work are
  equal lower panes with independent scrolling.
- Sources, History, and Settings moved into a compact top navigation bar.
- The Atlas mark is a small code-native orbit/A glyph. The central Atlas core has separate
  sleeping, listening, thinking, acting, speaking, offline, and observer behavior.
- Listening and speaking animate the signal bars; thinking and acting animate the scan system.
  Motion follows the operating-system reduced-motion preference.
- Durable jobs appear as task tabs. The selected task renders a terminal-style, bounded public
  lifecycle stream. Raw Claude terminal output remains private and is not copied into the UI.
- Pending exact action proposals render inside Active work. Receipts moved to History.
- Sources and runtime settings show numeric attention badges only while the runtime reports an
  unresolved condition. The aggregate and per-source numbers clear automatically when status
  becomes healthy.
- Settings explains action pairing in plain language and includes click-in local guides naming the
  files and keys governing voice, subscription work, transcript retention, display, pairing, and
  each source family.

## Live-state correction

The UI-only process previously created its own StatePublisher, so it could say ASLEEP while the
separate voice process was listening or speaking. That was the cause of the observed mismatch.

State responses now carry an x-atlas-surface identity:

- voice — the real voice worker
- observer — a UI-only process with no live voice state
- mirror — a paired UI-only command center reading the real voice worker's loopback state

The command worker.ui_server --port 4361 --mirror-port 4360 now provides a paired command center
while the voice worker owns 4360. If 4360 becomes unavailable, the mirrored state route fails
unavailable rather than showing the observer's idle state.

## Public telemetry additions

- GET /health returns only fixed subscription-worker health fields.
- GET /jobs/{uuid}/events returns at most 100 fixed, sanitized public lifecycle events.
- The UI treats an otherwise-available subscription heartbeat older than 30 seconds as stale.
- Private results remain separately encrypted and bearer-protected.
- Action lists, receipts, confirmations, and private results remain pairing-protected.

## Adversarial review

- The state mirror is restricted to 127.0.0.1, rejects its own serving port, uses a 0.8-second
  deadline, rejects responses larger than 64 KiB, and projects only the bounded Atlas state schema.
- Unknown keys from the mirrored process are discarded. Invalid states and malformed schemas fail
  with 503.
- Job events reject malformed/non-finite timestamps and expose no arbitrary nested payload.
- Subscription health accepts only the three declared states and bounded safe fields.
- The page uses text nodes rather than HTML insertion for transcript, proposal, event, receipt, and
  guide content.
- Pairing material remains fragment-bootstrapped, removed from the address bar, and memory-only.
- No connector, OAuth account, browser bridge, external file authority, deployment, paid task, or
  new Claude subscription session was activated by this slice.

## Verification

- Focused command-center/state suite: **36 passed**.
- Full standalone suite: **409 passed**, one unchanged dependency warning.
- JavaScript syntax check: passed.
- Python dependency check: passed.
- Changed-file trailing-whitespace scan: passed.
- Live loopback check: paired command center returned root 200, surface=mirror, a real 4360 voice
  state, and the existing transcript.

## Known limits / next design choices

1. The animated signal is state-reactive, not a raw microphone-energy meter. A truthful
   voice-amplitude animation requires the voice worker to publish a new bounded ephemeral scalar.
2. Active-work terminals show safe lifecycle telemetry, not raw Claude terminal output. A richer
   stream needs a reviewed, redacted supervisor-event contract.
3. Source buttons open the exact configuration guide and key, but the UI does not yet mutate
   atlas.yaml. Safe add/edit/delete needs an atomic, validated configuration-write boundary and
   explicit restart behavior.
4. Automated visual browser control was unavailable because the browser plugin rejected its own
   cached service path. Functional HTTP and DOM-source verification passed; Daniel's visual review
   of the opened local page remains the acceptance check.
