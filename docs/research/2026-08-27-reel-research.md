# Reel research 2026-08-27 - twelve saved reels -> Atlas + kb roadmap candidates

Method: 12 reels saved this week downloaded (yt-dlp, 33 MB), transcribed + frame-extracted (claude-video-vision),
each traced to its actual repo/product, then four research passes read those sources against our real code.
Full report (tables, evidence, sources): the "Twelve Reels, Two Systems" artifact; agent briefs in the boss
scratchpad `research/`.

## Ranked recommendations

| # | Change | System | Verdict | Effort | Risk |
|---|---|---|---|---|---|
| 1 | `ttl: "1h"` on both `cache_control` breakpoints (`worker/brain.py:236,266`); snapshot tools after MCP servers settle (capability text bakes tool names into the cached system block); assert Haiku 4.5's 4,096-token cache floor | Atlas | REWORK | S | Low |
| 2 | PostToolUse ruff/pyright/tsc hook ("red squigglies for agents"); kb has no Python linter, Atlas has ruff config but no ruff; emit `hookSpecificOutput.additionalContext`, cap 2,000 chars; verify Codex hooks fire on `apply_patch` (codex-cli 0.149.0 here) | kb + Atlas | ADAPT | S | Low-Med |
| 3 | Per-workflow `autonomy` (human-led / human-assisted / fully-autonomous) + `theHuman` beside `riskTier` | kb | ADAPT | S | Low |
| 4 | Connector status `connected / not_configured / error` (FounderOS `lib/connectors/types.ts`, MIT) in Mission Control + Atlas Settings//health | kb + Atlas | LIFT | S | Low |
| 5 | Port herdr `src/detect/manifests/{claude,codex}.toml` regex rules into kb's sentinel -> live working/blocked/done + inbox card on block | kb | LIFT | S | Low |
| 6 | `scripts/dream.py --apply --branch claude/dream-YYYY-MM-DD` -> PR into ops; absolute dates, "(Updated ..., previously ...)", never delete without replacement, <200-line index | kb | REWORK | M | Med |
| 7 | FTS5 search over dispatch logs + threads (Hermes `session_search` shape) | kb | ADAPT | S | Low |
| 8 | Atlas UI: time-of-day greeting, tool-bound radial quick-actions on the outer ring, TOOL palette, eadmin2/jarvis_ai MIT holo CSS; no WebGL, no gestures | Atlas | LIFT/ADAPT | S | Low |
| 9 | `plan` role -> opus in `governance/model-routing.yaml`; plan card + cheap execution cards for T2 work | kb | ADAPT | S-M | Med |
| 10 | Skill-learned loop (background review on a cheaper model -> staged `skills/learned/<name>/SKILL.md` behind pending/diff/approve) | kb | ADAPT | M | Med |
| 11 | BM25 + RRF leg in `brain_query.py` (vector-only today) with evals | kb | ADAPT | M | Med |
| 12 | kb workers inside herdr panes (`agent.wait`, `events.subscribe`, `pane.report_agent`) | kb | REWORK | L | High |

## The reels, resolved

- Huw Prosser gesture Jarvis: UI is an unreleased build; `jarvis-mlx` has no frontend + no license; gestures = printed ArUco markers; his verdict "awkward". ADAPT the UX (greeting, ring menu), NO gestures (Atlas CSP blocks MediaPipe anyway).
- "Holo" (justinbuilds.mov) = JustinGamer191/Holo: acoustic desk tap zones via mic, Swift/macOS, MIT, 705 stars. Not a hologram; not portable.
- Bennett Spooner "27 agents" OS = FounderOS-DEMO (MIT, Next.js, 697 stars, "larp-first, real-ready") + OptimalEngine (Elixir fork, "mostly schema-only" per its own audit; use docs as spec). Hermes harness = Nous Research Hermes Agent (MIT).
- Daisy Hollman: PostToolUse hooks; hooks cost no context until they fire.
- backboard R-CLI: MIT harness, memory layer closed; benchmark claims self-reported. Transferable idea = plan/execute split, which kb's routing table already scaffolds.
- /dream: kb already has `scripts/dream.py` (dry-run gated). Close the loop, don't rebuild.
- OX Alpha (x2): anonymous model; terms say prompts are training data. Skip.
- kzzy47 dashboard = Pulse by Vaylo Studios, closed, $500-1,000/mo. Aesthetic reproducible with d3-force (ISC).
- Chase AI: herdr (Rust, Apache-2.0, 32.7k stars, Windows GA via ConPTY, no live handoff on Windows, socket has no auth); concise output style already set.
- @technicallyhash prompt caching: kb measures 98.7% cache hits over 5,015 calls (already banked); Atlas needs the 1 h TTL.

## Skip

WebGL port (0.73 ms/frame today; WebView2 144+ drops SwiftShader), gesture control, OptimalEngine code, herdr marketplace plugins (unsandboxed), herdr live handoff, dream-skill's `nohup claude -p --allowedTools Bash` executor, FounderOS `lib/creds.ts` (T4), cosmos graph lib (CC-BY-NC), unlicensed Jarvis repos.

## Proposed waves

A quick wins (Atlas cache TTL + UI touches; PostToolUse hook; connector status). B Mission Control (autonomy field, herdr rules -> live state, fleet radar/org tree, FTS5). C gated (dream --apply via PR, Claims/Facts, plan role, skill loop, BM25+RRF). Parked: herdr runtime, constellation view.
