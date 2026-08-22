# Atlas direct-capability routing review

**Date:** 2026-08-21  
**Application:** `C:\Users\danie\Atlas`  
**Verdict:** the first connected lightweight action is production-wired; browser interaction and
Google remain honestly unconfigured

## Outcome

Atlas now has three explicit turn outcomes instead of forcing every non-conversational request
through one heavy-work object:

1. ordinary conversation returns natural text;
2. a connected lightweight capability produces one typed direct tool call and an actual host result;
3. durable, multi-step, research, drafting, or verification work produces the existing hidden
   `atlas_route_work` proposal.

`Open YouTube` is the first complete direct slice. The runtime advertises only the configured
`youtube` alias, Claude can select `desktop.open` with `chrome` and that alias, and the host validates
the fixed application/target pair. The shared ActionBroker records an exact parameter hash, binds
the proposal to a process-owned voice session/device context, confirms it through the trusted
service channel, and consumes it once. The signed executable resolver then launches Chrome without
a shell. Atlas receives the terminal host status and speaks a short natural result. No JobStore row
or subscription worker is involved.

## Root cause corrected

The prior interpreter supplied Claude only one generic 14-field `atlas_route_work` tool. An action
request such as `Open YouTube` therefore either produced an action-shaped tool result the parser
rejected or a malformed heavy Request. The provider call itself returned HTTP 200; the host then
collapsed the contract mismatch into `My conversation model returned an invalid response`.

The runtime catalog also dropped `status` and `detail` during sanitization, so the model could not
distinguish a connected adapter from one requiring configuration. Browser and Google were listed in
the product surface although neither had an active transport. Subscription Claude authentication
does not confer the CLI's browser sessions, plugin connections, Google OAuth, or desktop authority
on this separate voice process.

## Host boundary

- Direct tools are generated only from runtime catalog items whose status is exactly `connected`.
- The current voice-direct allowlist is only `desktop.open` and `desktop.focus`.
- Applications are fixed profiles; the model cannot name an executable.
- URLs are fixed configuration aliases; the model cannot supply an arbitrary URL.
- The model cannot provide a confirmation boolean, session, device, proposal ID, or parameter hash.
- A process-owned service boundary confirms only the exact broker proposal and consumes it once.
- Executor failures become bounded capability facts and a natural explanation, not false success or
  a generic conversation-schema error.
- Browser control, Google data/actions, local files, shell commands, and heavy work are not smuggled
  into this immediate path.

## Adversarial review

The review attempted arbitrary URL injection, extra capability parameters, an unregistered shell
capability, a connected-looking but unsupported Spotify projection, a non-immediate Google mutation,
proposal replay, changed binding, and launcher failure. These cases fail before execution or return
an honest failed/unavailable result. Repeating the same idempotent delivery does not launch twice.

A real-machine readiness check found two Windows-only resolver defects that mocks had hidden:

1. one failed known-folder lookup aborted every candidate location;
2. the PowerShell signature command appended an unquoted executable path to `-Command`, so Chrome's
   valid Google signature was never accepted.

Candidate roots now resolve independently with an OS-owned legacy Shell fallback. The candidate
path crosses into fixed Windows PowerShell through a dedicated scrubbed child environment value,
not command interpolation; inherited credentials and `PATH` do not cross. The installed Chrome now
resolves successfully and validates to the exact `Google LLC` signer.

## Verification

- Direct interpreter/front-desk/broker/runtime/desktop/cutover suite: **76 passed**.
- Full standalone suite: **409 passed**, one unchanged dependency warning in the pinned MCP stack.
- `python -m compileall -q worker`: passed.
- `pip check`: passed.
- `node --check ui/app.js`: passed.
- `git diff --check`: passed (the independent tree remains intentionally untracked).
- Live 4360 `/capabilities`: `desktop.open` is `connected` with only `youtube (url)` projected;
  browser and Google remain `configuration-needed`.
- PM2 `atlas-worker` restarted from `C:\Users\danie\Atlas` and is online.
- Real Chrome launch was deliberately not triggered by the test suite.

## Remaining scope

This does not make Atlas inherit Claude CLI connections. Future direct browser and Google tools must
be exposed only after their separate transport is paired and must receive operation-specific typed
schemas. Browser tab identity/origin and Google account-generation bindings remain host data, not
model guesses. Heavy work remains subscription-only and unchanged.

## Exact next step

Daniel wakes Atlas and says `Open YouTube`. Expected behavior: Chrome opens the fixed
`https://www.youtube.com/` alias once and Atlas briefly confirms the actual success. If local launch
fails, Atlas should say that it could not open YouTube and remain awake; it must not say
`conversation model returned an invalid response` and must not create a Worker job.
