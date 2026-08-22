# Atlas agent constitution

Atlas is an independent local application. It must not import kb modules, assume a kb checkout, or
write to kb/ops. Any optional bridge requires a separately reviewed package and remains dormant by
default.

Non-negotiable rules:

1. Never read, print, copy, or commit credentials, `.env` files, browser profiles, cookies, OAuth
   material, or Claude authentication state.
2. Never add a heavy API, Agent SDK, `claude -p/--print`, alternate-model, or fallback execution
   path. Heavy work is subscription-only through the reviewed `claude --bg` launcher.
3. Models interpret or propose. Host code exposes and executes only tools in the typed registry.
4. Host tool policy is one of three paths: instant work runs inline, confirmed work returns an exact
   readback for a later `confirm` turn, and long work launches visibly in the background.
5. A mutating MCP tool cannot execute on a model assertion. The host holds one expiring, single-use
   pending action, and only a matching later `confirm` call may consume it.
6. MCP child environments come from `~/.claude.json`; they are never logged or served.
7. `open` accepts configured aliases and HTTPS URLs only. Executables come only from signed desktop
   profiles; the model never supplies an executable path.
8. Run focused tests, the full standalone suite, `git diff --check`, and adversarial code/security
   review before declaring a slice complete.

Do not merge, deploy, activate external connections, or launch paid/background work unless the user
explicitly authorizes that exact action.
