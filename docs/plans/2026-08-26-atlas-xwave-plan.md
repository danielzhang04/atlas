# Atlas X-wave plan (2026-08-26) - truthfulness, desktop control, kb bridge, GitHub

Source: Daniel's 2026-08-26 transcript (Spotify "is open" with no tool call; refused local folder access;
61-email mismatch) + asks: full local desktop control ("everything instant except delete"), Atlas <-> VM kb
bridge against the dashboard-v3 end state (build now, rebase later), Atlas as a GitHub repo (yes).

Pipeline per unit: codex sol build in its own worktree -> sol adversarial review (read-only) -> fix
round(s) -> boss verification in the real environment -> commit -> merge into claude/atlas-streamline ->
live probes -> deploy.

## Tasklist

- [x] X1 host truthfulness guard: per-claim tool association (opened -> open/open_folder/focus_window,
      sent -> *send*, ...), clause-local negation, attribution-aware detection, progressive streaming keeps
      streaming safe sentences and holds only claim sentences; capability refusals consult the registry;
      count_mail reports inbox + Primary. Review REWORK -> fixed.
- [x] X2 desktop control (worker/desktopcontrol.py, pure ctypes): list/focus/minimize/maximize/restore/
      move(zones)/resize/close windows, media keys, window-relative click, type, key chords. Everything
      instant; deletion confirm-only, bound to the exact HWND with a foreground re-check before SendInput;
      ctrl+x confirm, single backspace instant (documented). Existing-app detection verifies the signed exe
      path. CLAUDE.md rule 12. Review REWORK -> fixed.
- [x] X3 analysis: docs/audits/2026-08-26-kb-dashboard-api-analysis.md (v1 contracts, auth model, risk tiers,
      negotiation, tool table).
- [x] X3a kb bridge package (kb repo, claude/atlas-bridge, dashboard/atlas-bridge/): standalone MCP stdio
      server, fail-closed negotiation (legacy adapters are the LIVE path), session via private notification,
      closed mutation DTOs, T3 refused server-side, pagination + projections, streaming /api/index extractor.
      Review REWORK -> 4 fix rounds (2 driven by live smoke against 127.0.0.1:5317).
- [x] X3b Atlas wiring: `command:` MCP kind (fixed argv, named env only), session channel after initialize,
      "Atlas, unlock kb" WebAuthn window, health `session: held|none|expired`, kb READ instant / MUTATION
      confirm. CLAUDE.md rule 6 amended, rule 13 added. Review REWORK -> fixed.
- [x] X4 GitHub: private repo danielzhang04/atlas, secrets audit, main protected (PR required).
- [x] X5 condense: ClaimGuard -> worker/claims.py, shared desktop schema/validation, ctypes dedup (-150 net).
- [x] Deploy + live probes + handoff (handoffs/2026-08-27-atlas-xwave.md); X6 hotfix for the MCP env regression.
- [ ] After the dashboard-v3 workover lands: rebase claude/atlas-bridge, re-run negotiation against v1,
      point kb_bridge.path back at C:/Users/danie/kb/dashboard/atlas-bridge, exercise the unlock flow on a
      win32-desktop daemon.
