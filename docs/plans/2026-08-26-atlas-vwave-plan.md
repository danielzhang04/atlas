# Atlas V-wave plan - 2026-08-26 (Daniel's live-use feedback)

Inputs: Daniel's transcript + six asks (2026-08-26). Decisions (Daniel, via question round):
ambient recall on request - yes; "Atlas," prefix + longer addressed window; chrome-devtools MCP + local app
profiles; Jarvis holo engine + frameless with real resize/snap; radical spoken-verbosity cut.

Evidence from the transcript: at 3:46:57 Atlas answered; at 3:47:31 (34 s later) Daniel gave a long
instruction that landed as `ambient` and was lost - `engagement_timeout_s` is 120 but `Addressing.window_s`
is shorter, creating a dead zone where Atlas is engaged yet not listening-to-you. Also: step-narration
("Let me search for that.", "Now let me read the full content.") and capability lectures are too long for
voice; `open` failed on a local folder; no browser interaction; frameless window cannot resize/snap; engine
visual too basic.

| Unit | Owns (exclusive) | Deliverable |
|---|---|---|
| W1 conversation | `worker/router.py`, `worker/engagement.py`, `worker/app.py`, `worker/brain.py`, `config/persona.md`, `config/atlas.yaml`, their tests | While ENGAGED, speech within 90 s of the last directed interaction is addressed (no vocab needed); beyond that, "atlas" anywhere in the utterance re-addresses; ambient lines carry timestamps and the brain gets the last ~3 min of ambient transcript as clearly-marked overheard context ONLY when the user references it ("I just said", "as I asked", "do what I said"); voice style: <= 2 sentences by default, no tool-step narration for instant tools, capability refusals in one line + one enablement hint, voice summaries 1-2 sentences unless a length is asked. |
| W2 reach | `config/mcp.yaml`, `config/apps.yaml`, `worker/tools.py`, `worker/desktopapps.py`, `worker/mcp_client.py` (only if the config shape needs it), their tests | chrome-devtools MCP server wired as an Atlas MCP server (navigate/click/type/fill via the existing MCP tool path; mutating tools stay behind the host confirm; instant prefixes tuned); Spotify desktop app profile (signed exe) with web fallback; `open <folder>` opens Explorer at a `file_roots`-allowlisted directory. |
| W3 window | `worker/desktop.py`, `tests/test_desktop.py` | Frameless window gains native edge-resize and Win+Arrow snap (WS_THICKFRAME/WS_MAXIMIZEBOX + hit-test), keeping the custom bar, close-confirm, icon, and the js_api method-only rule. |
| W4 engine | `ui/app.js`, `ui/index.html`, `ui/styles.css` | Jarvis-style holo engine: layered reactive rings/ticks/arcs, particle field, glow, state-driven motion and colour, volume/band-reactive; bespoke canvas only (no external libs); draw budget <= 2 ms/frame; boss screenshots and iterates. |

Process per unit: codex sol build (own worktree v1-v4 off `claude/atlas-streamline`) -> sol adversarial
review -> fix -> boss verification (full suite, probes) -> merge. Close: wake/hang/icon probes, headless UI
screenshots, live-checkout deploy, summary. Benchmarks: tests >= 418 and green; wake listener <= 12 s;
window responsive; engine <= 2 ms/frame measured; no new eager startup import.
