# Atlas continuous-conversation review

**Date:** 2026-08-21  
**Scope:** live voice failure diagnosis, wake/conversation/sleep lifecycle, provider deadline  
**Verdict:** fixed, verified, and reloaded into the existing standalone worker

## Incident

The 20:22 live transcript exposed two independent host-side failures. The first conversational turn
reached Anthropic but was canceled by Atlas after four seconds. The next three utterances never
reached Claude: a legacy addressed-speech window expired 30 seconds after wake and silently gated
them. At 20:24 a later explicitly addressed turn completed successfully, then a legacy 120-second
silence watcher raced the response and immediately put Atlas to sleep.

None of Daniel's language was rejected for safety or content. The host discarded it before the
model call. The backend routing, authorization, durable jobs, worker supervision, and receipts were
not the cause and were preserved.

## Product contract now implemented

- The local wake-word model is the only transition from asleep to awake.
- Once awake, every finalized utterance is part of one continuous Claude conversation.
- Atlas does not require its name again and does not expire the session on a timer.
- Only the exact configured dismiss phrases (`that's all`, `go to sleep`, including bounded filler
  variants such as `okay, go to sleep`) return Atlas to sleep.
- Gratitude, cancel/repeat phrasing, questions, profanity, frustration, and all other language go to
  Claude. They are not fixed host intents.
- The model may propose work through `atlas_route_work`; the existing host still validates the
  complete Request, chooses the lane, authorizes/admit/executes it, and records receipts.
- The proposal schema is non-strict because provider schema compilation is not a security boundary.
  Malformed proposals still fail host validation and create no job.
- The conversational deadline is ten seconds. A real timeout/provider/invalid-response failure now
  names that boundary and explicitly says Atlas remains awake.

The retired addressed-speech module and its tests were removed. No replacement router, session
manager, executor, or work infrastructure was added.

## Adverse review

- LiveKit remains `llm=None` and `tools=[]`; all finalized turns terminate at the existing
  `VoiceFrontDesk`.
- No normal utterance is inspected by a fixed intent table before Claude. The only exception is the
  explicit local sleep control.
- Wake and sleep continue to control whether microphone audio reaches Deepgram. Removing automatic
  sleep means the user-visible awake state remains active until explicit dismissal; it does not
  enable audio while the app says asleep.
- The hidden model proposal remains non-authoritative. Invalid, partial, compound, or forged FAST
  metadata cannot bypass Request validation or host routing policy.
- Provider diagnostics still log only exception class and numeric status, not request/error text.
- No paid/background Claude job, heavy workflow, connector, OAuth flow, VM bridge, external file
  mutation, deployment, commit, push, or merge was performed.

No unresolved high- or medium-severity finding remains in this slice.

## Verification

- Continuous-conversation focused regressions: **80 passed**.
- Full standalone suite: **401 passed**, one unchanged dependency warning.
- `pip check`, JavaScript syntax, Python compilation, and changed-file whitespace checks: passed.
- Production scans find no addressed-speech import, inactivity watcher, timer configuration, gated
  transcript log, or unsafe-understanding dialogue in the live voice path.
- The existing `atlas-worker` PM2 process was restarted from `C:\Users\danie\Atlas`; it is online.
- Both the voice state on 4360 and command-center mirror on 4361 report `ASLEEP` after reload.

Automated tests used injected clients and local state. No live model prompt or subscription job was
launched during this correction.

## Acceptance check

Wake Atlas once, speak an ordinary sentence, pause longer than 30 seconds, then speak a contextual
follow-up without saying Atlas again. Both turns should receive natural replies. Atlas should remain
awake until `go to sleep` or `that's all` is spoken.

## Prompt-contamination follow-up

The 21:06 live acceptance test proved the continuous session worked, but social turns such as
`Shit. Finally.` and `Hey, Atlas.` produced unsolicited inventories of local files, browser,
Spotify, and Google tools. These were successful model turns, not wake hooks. The interpreter was
serializing the current transcript and complete capability catalog together into every user message,
so Claude reasonably treated Daniel's short utterance as a reaction to newly supplied inventory.

The user message is now exactly the transcript. Sanitized capability inventory exists only in the
private `atlas_route_work` tool description, where it can inform a genuine work proposal without
entering dialogue or history. System/persona instructions make the distinction explicit: social
turns default to one brief sentence, and Atlas does not reintroduce itself, announce readiness, or
inventory tools unless Daniel asks.

Regression coverage proves `Shit. Finally.` and `Hey, Atlas.` remain raw user history while the
catalog remains tool-only. Focused prompt/front-desk/cutover tests passed **43/43**; the full suite
passed **401** tests with the unchanged dependency warning. No live model or heavy job was launched
for this correction.
