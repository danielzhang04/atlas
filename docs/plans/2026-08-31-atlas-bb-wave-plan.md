# BB-wave plan — "do anything", seamless rendering (2026-08-31)

Daniel's goals: (1) Atlas does anything a CLI-with-connectors can, automatically, with
guardrails (confirm before destructive); (2) rendering is seamless — video, not flipbook;
(3) tool rows out of the chat. Sources: B1 (google outage diagnosis), B2 (capability
architecture, external-server survey), B3 (frame-pacing analysis) — all file:line-cited.

## Established facts

- **Google outage:** workspace-mcp hung at startup 08-31 15:24:38 (two processes raced
  port 8000, 16ms apart, both stalled silently; token file last written 08-26, consistent
  with the known 7-day Testing-mode expiry). Atlas has no connect retry — one-shot
  `mcp.connect()` at app.py:637. Needs Daniel's manual reauth once + resilience work.
- **The taint wall:** any MCP call taints the turn; 12 host action tools then refuse.
  "Search Drive for X and open it" is structurally impossible regardless of connectors.
- **116 tools in the prompt** (~2-3x the 30-50 accuracy threshold; 32K prefix). Curation
  before ANY new server.
- **Choppiness:** top suspect GC micro-stalls from per-frame Path2D/gradient allocations
  (app.js:504,560-561,615); second, poller/rAF main-thread contention. DPI/CSS/bridge
  ruled out. Occlusion flag live but insufficient.

## Track C — capability ("all the things")

| Unit | Scope | Guardrails | Size | Daniel |
|---|---|---|---|---|
| C0 policy + surface budget | `expose:`/`describe:`/`never_instant:`/`confirm_when:`/`domain:` config keys; `escalate` hook (instant→confirm only); cut 116→~65 tools | never_instant fail-closed over instant; get_drive_shareable_link → confirm; manage_event arg-conditional | S | — |
| C1 taint handles | Host-minted per-turn handle table; open_file/open_folder accept handles; handle-only when tainted | Handles minted only from host-produced results; model can never synthesize a target from content | M | — |
| C2 filesystem MCP | Official @modelcontextprotocol/server-filesystem, roots = file_roots | 9 reads instant; write/edit/move/mkdir confirm; NO delete tool exists (structural); roots-capability decision explicit | S | — |
| C3 google trim + resilience | expose: 38→~14; connect retry w/ backoff; `reauth_needed` status detail + voice guidance ("Google needs a reauth") | tier the mutation set; never edit the shared ~/.claude.json entry | S | one-time reauth (steps below) |
| C4 Spotify | Vendored fork of marcelmarais/spotify-mcp-server (merge Feb-2026 API migration PRs, silence stdout, PKCE, tokens outside repo) | search/reads/playback instant (playback is the point of voice); playlist mutations confirm; clean Premium-missing refusal | L | Spotify dev app + Premium + OAuth once |
| C5 browser tiering | confirm-tier navigation/click/fill with readback | network tools stay hard-blocked; real browsing stays launch_work | S | — |
| (kb) | nothing — blocked on kb dashboard-v3 | — | — | merge decision on kb board |

Sequencing: C0 → C1 → C2 gives most of "feels like anything" with zero new OAuth.
Don't-build list (from B2): bespoke per-app clients, model-driven tool selection (rule 2),
Desktop Commander as a boundary, varunneal/NovaLux12 Spotify servers as dependencies,
dispatcher meta-tools, raising MAX_TOOL_ROUNDS before measuring (30s ceiling binds first,
not rounds), weakening taint (replace with C1, never delete).

## Track R — rendering seamlessness

| Unit | Scope | Size |
|---|---|---|
| R0 instrument | frame-interval histogram + drop counter (preallocated, ~10 lines) in engine metrics | S |
| R1 allocation + sprite fixes | reuse waveform paths (no per-frame Path2D), sprite-cache tick/segment rings (backdrop pattern exists), gradient rebuild only during 420ms color transitions | S |
| R2 gated trace | one flag: webview.settings REMOTE_DEBUGGING_PORT (composes with occlusion patch, verified); relaunch; chrome-devtools MCP performance trace of a spoken turn; check GPU raster status, GC events, long tasks | needs Daniel's relaunch OK |
| R3 OffscreenCanvas + Worker | engine off the main thread (WebView2 151 supports; CSP already allows same-origin workers) | M — only if R1+R2 show contention remains |
| R4 WebGL (vendored, non-WASM, no CSP change) | GPU-rasterized engine | L — last resort |
| R-quick tool rows | stop publishing `tool:` lines to the transcript ring (worker/app.py:556; traces DB keeps full telemetry) | S |

Seamless = measurable: p95 frame interval < 16.7ms awake; drops (>25ms) < 1% during a
spoken turn; zero rAF-attributable >50ms tasks; GC events ~0 from engine allocations.

## Daniel's reauth (do anytime, ~3 min)
1. Close Atlas + any Claude Code session using google-workspace (frees the OAuth port).
2. Rename (don't delete) `~\.google_workspace_mcp\credentials\daniel.zhang.t1@gmail.com.json`.
3. Foreground terminal with the env from the ~/.claude.json entry:
   `uvx workspace-mcp --tools gmail drive calendar`
4. Trigger `start_google_auth` (your email) → browser consent → credentials file rewritten.
5. Restart Atlas; google should show connected (38→~14 tools after C3).

## Process
Per unit: build → adversarial review → boss verify (mutations re-run) → merge →
full suite + live scenario → single PR → Daniel merges → sync + relaunch.
