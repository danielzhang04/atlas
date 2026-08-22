# Atlas conversational boundary review

**Date:** 2026-08-21  
**Scope:** voice failure diagnosis, conversational model boundary, hidden work routing, natural work narration  
**Verdict:** implementation, automated review, and live worker reload complete; one user-spoken
acceptance turn remains

## Incident

At 19:35 the standalone voice worker transcribed `Hello?`, `What do you mean?`, and
`What does that mean?` correctly. Each turn then received HTTP 400 from the Anthropic Messages API.
The sleep phrase succeeded because the local reflex lane handled it before the model request.

The old interpreter forced every utterance through one strict `reply | clarify | request` tool.
Its raw schema contained unsupported structured-output constraints (`minimum`, `maximum`,
`maxLength`, and `maxItems`) and sat at the documented limit of 24 optional parameters. Unit tests
used an injected fake client, so they validated local parsing without exercising provider-side schema
compilation. The catch-all failure was then spoken as `I couldn't safely understand that`, incorrectly
describing a backend request failure as a problem with Daniel's speech.

## Product correction

Claude now owns ordinary conversation. A normal response is plain bounded text, not a classification
object. The interpreter retains six recent user/Atlas exchanges in memory so short follow-ups are
ordinary conversational context.

When Daniel asks Atlas to do work, Claude may emit one hidden `atlas_route_work` proposal. The tool
schema is small, non-strict, uses no unsupported constraints, and has no optional fields. It is a
model-output shape, not an authority boundary. The host turns
that proposal into the existing bounded `Request`, independently selects FAST or SLOW, enforces
authority, and performs durable admission. Conversational text is never execution authority, and raw
phrase matching no longer converts a model clarification into a hidden job.

After admission, the host returns only bounded route facts to Claude: public status, lane, sanitized
error code, replay state, and whether a job is visible. Claude explains those facts naturally. The
transcript does not expose schemas, job identifiers, or routing machinery. Deterministic spoken text
remains only as the emergency fallback when the conversational model itself is unavailable.

The standalone persona was reduced to voice, conversational continuity, truthfulness, and the
model/host authority boundary. Legacy kb card/workflow dialogue and scripted response trees were
removed.

## Security and adverse review

- LiveKit still has `llm=None` and `tools=[]`; finalized speech terminates at `VoiceFrontDesk`.
- The route tool proposes metadata only. It cannot execute, confirm, bind capability parameters, or
  choose its own lane.
- Detailed bounds remain in the host `Request` contract; invalid tool values create no job.
- The host still overrides forged FAST metadata through raw-work routing policy after a proposal.
- Provider exceptions log only exception class and numeric HTTP status, never error text or request
  material.
- Model narration sees bounded backend facts, not job payloads, credentials, results, or receipts.
- No phrase resembling an action can create a job unless Claude emitted the explicit route tool.
- No heavy API fallback, subscription session, connector, OAuth flow, external mutation, or VM kb
  bridge was added or activated.

No unresolved high- or medium-severity correctness or security finding remains in this slice.

## Verification

- Conversational interpreter/front-desk/cutover regressions: **42 passed**.
- Routing/front-desk/state regression set: **126 passed**.
- Full standalone suite: **421 passed**, one unchanged dependency warning.
- `pip check`: passed.
- `node --check ui/app.js`: passed.
- `git diff --check`: passed.
- Production voice code contains no `couldn't safely understand/queue` response.
- PM2 restarted the standalone worker successfully; voice state and 4361 mirror both report healthy
  `ASLEEP`, with the expected zero idle energy and no current error-log entry.

No paid live model request was made during implementation or automated verification.

## Next step

Daniel says `Hello?` and one short contextual follow-up to the restarted worker. The expected path
is natural text with no job. A later explicitly authorized work request should produce a hidden
route, host admission, a Workers tab when applicable, and a natural Atlas status response.
