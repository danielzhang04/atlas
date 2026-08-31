# AA-wave plan — voice truthing, smartness, UI polish (2026-08-31)

Source: Daniel's first real conversation session on the Z-wave build (17:27–17:29) + three
read-only analyses (A1 voice pipeline, A2 capability, A3 UI), each with file:line evidence.
Guiding constraints: no bloat (net LOC down where possible), CLAUDE.md rules binding,
adversarial review + full suite per unit, live scenario validation before deploy.

## Root causes established (evidence in the analyses)

1. **Voice garble is the ClaimGuard, not the model.** Every "shall I?"/"Want me to?" was
   host-authored: `_refused_capability` (worker/claims.py:89-93) offers capabilities by
   keyword-matching DANIEL's words with no applicability check and no once-only guard;
   delayed-sentence design (brain.py:598-603 → 665-672) reorders speech (caveat after remedy),
   strands boundary spaces (jammed sentences), and drops sentences (silent `return None`,
   claims.py:85). `open` missing from `_ASSOCIATIONS` made the guard call a successful open a
   lie. History stores the rewritten text, teaching ask-first behavior. All four transcript
   lines reproduced char-for-char headlessly.
2. **"Not smart" is mostly host defects.** `open` silently demotes a failed desktop launch to
   the web URL and reports success identically (tools.py:415-420); URL path has no dedupe
   (hence Spotify twice); the `open` schema hides the alias vocabulary; the prompt says "call
   instant tools directly" but never says which tools are instant (brain.py:89 vs :699-730), so
   the model hedges. A/B probe: fixed prompt turns a hallucinated "Music is playing" into a
   real `open('spotify')`. Model lane (haiku-4-5) is the residual factor.
3. **UI:** mic line clipped by a fixed 42px `.engine-status` track that overflows when the tool
   strip mounts (styles.css:278-279, pre-existing, surfaced by TOOL state now working);
   up/down bounce = tool-strip mount/unmount reflow under `justify-content:center` + `holoIdle`
   breathing transform; "dots go back and forth" = temporal aliasing from frame jank + a
   non-delta-time-scaled easing (app.js:548,551); wasted work: `renderAudio` unconditional 1s
   writes, `refreshJobs` full DOM rebuild every 2s; suspected WebView2 occlusion throttling
   under the frameless window (no browser-args hook in this pywebview — needs monkeypatch).

## Units

### AA1 — ClaimGuard surgery (S, net −37 LOC) [build: sonnet; review: opus — removes safety logic]
Worker/claims.py + brain.py:
1. Delete `_refused_capability` substitution + `_REFUSAL_ROUTES` + refusal half of `delayed()`;
   keep the WARNING log.
2. Preserve order: once anything is held, hold everything after it (both stream blocks + flushes).
3. Restrict guard to perfective claims (drop bare "open"/"done" from ACTION_CLAIM_VERBS; delete
   dead open_state machinery).
4. Add "open" to `started`/`played` associations.
5. Trailing space on UNBACKED_ACTION_REPLY; add a host-constants-end-in-whitespace test.
REWRITE tests/test_brain.py:1352 (it encodes the reordering bug). Live scenario: "open Spotify"
then "open it in my other Chrome profile" → caveat first, remedy second, single spaces, zero
host-fabricated offers.

### AA2 — Honest open + prompt tiering (S) [build: sonnet; review: sonnet]
1. `open` fallback returns `{"opened": name, "via": "web"}` (model can tell the truth); URL path
   dedupes (no double-launch in a turn).
2. Alias vocabulary into the `open` schema description (host already builds it, tools.py:403).
3. System prompt: "Every tool is instant except press_delete and mutating MCP actions; never ask
   permission before calling a tool" + alias-vs-URL guidance; persona.md:7,10 adjusted.
4. Promote the A/B probe harness into tests/ as a prompt-regression harness (utterance →
   expected tool-args, recorders behind real schemas).

### AA3 — Sonnet 5 lane (S) [build: sonnet; review: none needed beyond AA-wave gate; measure!]
`config/atlas.yaml` fast_model → claude-sonnet-5 + pricing block; measure real turn latency
against turn_timeout_s 12 / turn_ceiling_s 30 before raising (expect ~20/45 if needed).
Fix traces.py:92 1h cache-write pricing (×2 base input = $2.00/MTok, not ×2 write rate).
Cost: ~2× on pennies (~$0.40/day @100 turns). Two-tier router REJECTED (per-model cache
cold-starts make it ~7× dearer). Rule 2 untouched (same spend-capped key path).

### AA4 — UI layout + jank (S-M) [build: sonnet; review: sonnet]
Ranked: (1) `.engine-status` overflow → `auto` track (mic line never clipped, incl. TOOL state);
(2) reserve tool-strip space (`visibility` not `display`) — no reflow bounce; (3) gate
renderAudio/renderVoice by last-value; (4) signature-gate refreshJobs renders; (5) delta-time-
scale waveform easing; (6) SIGNAL_INTERVAL_MS 100→150; (7) stop rAF + holoIdle when
ASLEEP/OFFLINE; (8) drop topbar backdrop-filter. Verify via canvas data-frame-cost-ms telemetry
+ Daniel's eyes (mic line visible through a tool call; no bounce across repeated tool calls).

### AA5 — WebView2 occlusion flag (S code, M risk) [build: opus; review: opus; SEPARATE gate]
Monkeypatch EdgeChrome.__init__ to append `--disable-features=CalculateNativeWinOcclusion`
(standard fix for frameless/custom-WndProc windows throttled by DWM occlusion tracking).
Defensive: try/except + feature-detect; rule 11 note (importtime/RSS); verify the flag lands in
the WebView2 process command line. Only unit touching third-party internals — ship last,
separately revertible.

## Deferred / rejected
- Chrome profile launching (M): config-enumerated profile map → host-built `--profile-directory`
  argv; satisfies rule 7; build AFTER AA2 proves `open` honest. Rule 1 forbids auto-discovery.
- Two-tier model router: rejected (cache economics).
- More prompt text to discourage asking: rejected (cause is host-side; bloat).

## Process
Parallel worktrees per unit off claude/atlas-streamline; build → adversarial review → boss
verify (incl. re-running any reviewer mutations) → merge → full suite (expect only the known
test_post_edit_check flake) → live scenario script → single PR → Daniel merges on GitHub →
boss syncs live checkout + relaunches → Daniel re-runs the voice scenario.
