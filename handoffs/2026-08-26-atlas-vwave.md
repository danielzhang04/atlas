# Atlas handoff - 2026-08-26 (V-wave: conversation, reach, window, engine)

**Status:** DONE and DEPLOYED. `C:\Users\danie\Atlas` fast-forwarded to `claude/atlas-streamline` HEAD
(`git log -1`). **471 tests.** Plan: `docs/plans/2026-08-26-atlas-vwave-plan.md` (Daniel's six live-use asks +
his four decisions). Previous: `2026-08-25-atlas-streamline.md`. Every unit: codex sol build -> sol
adversarial review -> fix round(s) -> boss verification (suite + live probes) -> merge.

## What changed

1. **Conversation (W1).** While engaged, anything said within 90 s of the last exchange is addressed - no
   wake word needed (`addressed_window_s` in atlas.yaml); saying "Atlas" anywhere re-addresses until the
   120 s engagement timeout. Saying "I just said..." / "as I asked" / "do what I said" (nine phrases) pulls
   the last 3 minutes of ambient transcript into that turn as a marked, unverified, TAINTED block (<= 4,000
   chars, newest first); ambient speech still never triggers a response by itself. persona.md rewritten:
   <= 2 short sentences, no tool-step narration, one-line capability refusals ("No - I can't X. <hint>."),
   voice summaries 1-2 sentences unless a length is named.
2. **Reach (W2).** chrome-devtools MCP wired (27 tools live-connected): reads/snapshots instant;
   navigate/click/type/fill/evaluate and anything unlisted behind the voice confirm; `get_network_request` /
   `list_network_requests` hard-blocked (they can carry cookies/Authorization). Spotify desktop app profile
   (signed, web fallback). `open_folder`: Explorer at file_roots-allowlisted directories only (System32
   explorer.exe, refused on tainted turns). "Open my kb folder" now works.
3. **Window (W3 + W3b/c).** Frameless window has real edge/corner resize and Win+Arrow snap
   (WS_THICKFRAME/MAXIMIZEBOX restored with correct last-error semantics; NCCALCSIZE clamps maximized client
   to the work area; NCHITTEST 8 px borders; maximize via native WindowState on the UI thread). The live
   root cause of "styles/icon never applied": the start callback ran before pywebview created the Form
   (`window.native` None) - it now waits on `window.events.shown`. Live-verified: style 0x16070000, icon set,
   INFO "native window configured" in the desktop log.
4. **Engine (W4).** Jarvis holo: hex/graticule backdrop, 120-tick rotating ring, counter-rotating segmented
   arcs, the 24-band feed as a mirrored 96-segment glowing circular spectrum, 42 orbiting glow particles,
   breathing/pulsing bloom core. States: slate ASLEEP, violet/cyan LISTENING, amber THINKING scanner, warm
   SPEAKING, dim red OFFLINE - 420 ms interpolated transitions incl. layer fades. Measured 0.73 ms/frame avg
   (1.5 max), budget <= 2 ms; pauses when hidden/off-Live. `window.__atlasEnginePreview = 'THINKING'` (console)
   previews states - boss-authorized visuals-only hook.

## Numbers / evidence

Tests 410 -> 471; wake listener 6-10 s from spawn with `wake_model: hey_atlas` in `/state`; chrome-devtools +
google MCP both connect (27 + 38 tools); frame cost 0.73 ms; window hang-free; doctor 31/31 OK. Engine
screenshots (all four states) in the boss session scratchpad.

## Notes / residue

- During diagnosis the boss accidentally killed the worker of Daniel's 15:41 running instance (it showed
  "Atlas stopped" until cleanup) - disclosed in-session; the stale instance also held the single-instance
  mutex, which briefly made probes measure an "already running" dialog.
- WM_GETICON is a weak icon check (returns the WinForms default): trust the "window icon set" INFO log line.
- `Atlas-worktrees\{v1..v5,u1..u8,...}` dirs are ACL-locked sandbox residue (elevated delete); branches gone.
- Daniel's live checks: resize edges/corners, Win+Arrow snap, taskbar icon (re-pin once if a pinned button
  still shows the old identity), "hey atlas" -> converse without repeating the wake word, "I just said..."
  recall, "open Spotify" (app), "open my kb folder", and a confirmed chrome-devtools action ("click ...").
