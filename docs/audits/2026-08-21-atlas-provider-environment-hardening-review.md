# Atlas provider-environment hardening review

**Date:** 2026-08-21  
**Scope:** subscription-worker admission and Claude child environment  
**Verdict:** account-free hardening complete; first live subscription smoke remains human-gated

## Finding

Atlas rejected common API credentials before subscription-worker startup, and its Claude transport
removed broadly named provider secrets. The two controls had separate definitions. Current Claude
Code documentation also exposes provider selectors for Bedrock, Bedrock Mantle, Vertex, Microsoft
Foundry, and Claude Platform on AWS. `FOUNDRY` and `MANTLE` were absent from the transport pattern,
while provider selectors as a class were absent from the startup rejection set. An inherited
selector could therefore conflict with the human claim that the worker would use only a Claude
subscription, including when the provider used ambient cloud identity rather than an API-key
environment variable.

Reference: [Claude Code environment variables](https://code.claude.com/docs/en/env-vars).

## Resolution

- Added one host-owned `METERED_PROVIDER_ENV` set covering documented provider selectors, endpoints,
  and credential variables relevant to the subscription-only boundary.
- Made startup detection case-insensitive and fail closed for every nonblank member of that set.
- Added `FOUNDRY` and `MANTLE` to the independent child-environment scrubber.
- Added tests for every documented provider selector, case normalization, and the invariant that
  every centralized metered-provider variable is removed before a Claude child receives its
  environment.

## Adversarial review

- Rejection occurs before configuration-dependent workspace creation, health publication, job-store
  opening, or Claude process launch.
- Empty variables do not create a false activation; nonblank values are rejected.
- Ambient AWS, Google, or Azure identity cannot select a metered backend after its selector is
  rejected and stripped.
- `CLAUDE_CONFIG_DIR` remains available for subscription authentication; no auth files or values are
  inspected, copied, logged, or persisted.
- Agentic jobs retain isolated workspaces, project-only settings sources, strict MCP configuration,
  and the existing host-fixed model/tool policy.

No unresolved high- or medium-severity issue was found in this slice.

## Verification

- Focused tests: **31 passed**.
- Full standalone suite: **398 passed**, with the existing MCP dependency warning only.
- `pip check`: passed.
- `node --check ui/app.js`: passed.
- Whitespace comparison against the authoritative source: passed.

No paid/background Claude session or live integration was launched.
