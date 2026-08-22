# Atlas agent constitution

Atlas is an independent local application. It must not import kb modules, assume a kb checkout, or
write to kb/ops. An optional kb bridge requires a separately reviewed package and remains dormant by
default.

Non-negotiable rules:

1. Never read, print, copy, or commit credentials, `.env` files, browser profiles, cookies, OAuth
   material, or Claude auth state.
2. Never add a heavy API, Agent SDK, `claude -p/--print`, alternate-model, or fallback execution
   path. Heavy work is subscription-only through the reviewed `claude --bg` supervisor.
3. Models interpret or propose; host code routes, authorizes, executes, and records receipts.
4. FAST remains a small positive allowlist with complete host-bound arguments. Ambiguity,
   compounds, iteration, research, artifacts, or verification route SLOW.
5. Both lanes use the same typed host capability broker. Mutations require exact preview and a
   one-use trusted confirmation; model booleans are never authority.
6. Google credentials belong to an external local broker. Atlas receives typed results only.
7. Keep local files, browser profiles, desktop aliases, OAuth, hosted access, and live external
   actions unavailable until their named human activation gates pass.
8. Run focused tests, the full standalone suite, `git diff --check`, and adversarial code/security
   review before declaring a slice complete.

Do not merge, deploy, activate connectors, or launch a paid/background task unless the user
explicitly authorizes that exact action.
