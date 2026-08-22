# Atlas audio-reactive and guided-setup review

**Date:** 2026-08-21  
**Scope:** live voice regression, loudness signal, visual cleanup, guided setup admission, live cutover  
**Verdict:** implementation and standalone cutover complete; interactive PTY/gate continuation remains future work

## Live incident diagnosis

At 19:08:30 the previous 4360 worker received "How's it going?" and invoked its legacy
`queue_summary` tool. That tool did not settle until 19:08:57. A second wake fired during the stall,
and TTS subsequently timed out. Atlas did not intentionally sleep immediately: the public transcript
shows the actual timeout transition at 19:11:01. The perceived shutdown was a blocked turn with no
reply.

The process serving 4360 was confirmed to run from the older
`C:\Users\danie\kb-worktrees\atlas\atlas` checkout. It was removed from PM2 and replaced by the
tested independent `C:\Users\danie\Atlas` worker. The saved PM2 target now uses the standalone
`run-worker.js`. The standalone voice agent has `llm=None`, `tools=[]`, and exactly one bounded
1.5-second interpretation call per finalized turn; it cannot enter the old autonomous LiveKit tool
loop.

## Audio-reactive Atlas Engine

The always-local wake listener now reduces each existing 80 ms int16 microphone frame to a
perceptual 0–1 loudness scalar. The conversion maps approximately -55 dBFS to silence and -10 dBFS
to full visual energy. Only the latest number is retained, it is forced to zero while Atlas is
asleep, and no raw sample, frequency bin, or history crosses the wake-listener boundary.

The voice surface exposes the scalar through a bounded `/signal` projection. The 4361 mirror proxies
that fixed projection, and the UI samples it with an overlap guard at the source frame cadence. The
Canvas engine smooths attack and decay and applies the live energy to radial bar height, waveform
deformation, glow, and particle drift while LISTENING or SPEAKING.

This is intentionally real loudness response, not a fabricated frequency spectrum.

## Visual cleanup

- Active Atlas states now share one purple imprint. State is communicated primarily through motion
  and text rather than unrelated hues.
- Black, white, and neutral grey remain the shell. Amber is reserved for attention and red for
  failure. Connected/success styling no longer adds green.
- User transcript labels are neutral; Atlas receives the single purple imprint.
- The home brand no longer inherits the selected-navigation underline.
- The brand and browser-tab favicon now use the same compact diamond/core mark.

## Guided configuration work

Unready source cards and the Voice/Subscription settings expose `Guide me`. A paired same-origin
POST can select only one host-reviewed guide ID. The browser cannot submit a prompt, command, path,
tool, model, or permission. Atlas composes the bounded context, governing files, strategy, secret
warning, and readiness condition, encrypts it as a normal slow payload, and admits it into the same
durable subscription queue as other work.

When the subscription worker is available, the task appears in Workers and its Claude background
session closes through the normal terminal lifecycle. The run then moves to History. Completion does
not clear the originating notification: capability and health polling clear it only after the real
readiness condition reports success.

This slice does not claim interactive PTY control or resumable setup questions. The current reviewed
worker returns a contextual private walkthrough. True interactive gates remain dependent on the
versioned run/frame and correlated gate work in the run-surface plan. Automatically starting the
subscription supervisor from a browser click was rejected because it could claim unrelated queued
work and cross the subscription activation boundary.

## Security and adverse review

- `/signal` is loopback-only, read-only, finite, clamped, and zero while asleep.
- Energy observers cannot terminate the wake listener; an observer exception disables only the
  visual callback.
- Guided setup requires the in-memory paired bearer, same-origin JSON, a fixed ID grammar, and an
  injected host provider.
- Guided instructions explicitly forbid credentials and claims of machine/account actions.
- Slow guide payloads use the existing encrypted, fingerprint-bound, lease-fenced store.
- No connector, OAuth flow, external file authority, VM kb bridge, subscription job, deployment,
  commit, merge, or push was performed.

No unresolved high- or medium-severity correctness or security finding remains in the implemented
slice.

## Verification

- Focused voice/state/server/UI/guided-setup suite: **71 passed**.
- Full standalone suite: **417 passed**, one unchanged dependency warning.
- `node --check ui/app.js`: passed.
- `pip check`: passed.
- `git diff --check`: passed.
- Standalone 4360 voice surface: state 200, voice mode, bounded `audio_energy`, signal 200.
- Mirrored 4361 surface: state 200, mirror mode, signal 200, favicon 200, guided controls served.
- PM2 standalone worker: online with zero restarts after cutover; current error log empty.

Automated visual browser control is still unavailable because the bundled browser runtime rejects
its cached service path as untrusted. Daniel's refreshed browser remains the visual acceptance
surface.

## Next step

Daniel tests one wake, conversational reply, and loudness response in the open 4361 page. After
visual tuning, implement the versioned run/frame stream and correlated resumable gates, then replace
the terminal-style event renderer with actual xterm/PTY output.
