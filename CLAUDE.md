# Atlas agent constitution

Atlas is an independent local application. Open the app to turn Atlas on; close its window to turn
Atlas off. It must not import kb modules, assume a kb checkout, or write to kb/ops. Any optional
bridge requires a separately reviewed package and remains dormant by default.
The desktop app is the only command-center host; do not add a standalone browser-host process.

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
6. MCP child environments are never logged and never the host's full environment.
   `from_claude_config` servers are spawned with mcp's default child environment plus the env of
   their `~/.claude.json` entry; `command:` servers are spawned with an exact environment: only the
   flags named in `env_from` plus PATH and SystemRoot. Secrets never travel in env for command
   servers; the kb session token travels only over the private notification channel.
7. `open` accepts configured aliases and HTTPS URLs only. Executables come only from signed desktop
   profiles; the model never supplies an executable path.
8. Run focused tests, the full standalone suite, `git diff --check`, and adversarial code/security
   review before declaring a slice complete.
9. Objects passed to pywebview as `js_api` expose only explicitly reviewed public methods; native
   objects, handles, locks, windows, callbacks and mutable state stay private (underscore); every
   `js_api` class has a reflection-walk regression test.
10. Persistent logs are bounded and host-shaped: never pairing or shutdown tokens, credentials,
    private environment values, MCP child environments, prompts, or raw child stdout.
11. No new eager third-party import on the desktop or worker startup path without an importtime
    comparison and idle-RSS check recorded in the change.
12. Desktop control acts on windows by host-resolved title/pid; the model never supplies handles,
    executables, or delete chords. `delete`, `shift+delete`, `ctrl+d`, and `ctrl+x` are confirm-only.
    A single `backspace` stays instant as the narrow editing-correction exception.
13. The kb bridge enforces T3 classification and refuses T3 responses from `kb_human_respond`
    with typed `t3_requires_dashboard`; the Atlas brain and MCP tool descriptions only surface
    bridge results and do not enforce that boundary.

Do not merge, deploy, activate external connections, or launch paid/background work unless the user
explicitly authorizes that exact action.
