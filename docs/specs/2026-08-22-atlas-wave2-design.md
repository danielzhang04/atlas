# Atlas wave 2 — design (2026-08-22, evening)

**Amends:** `2026-08-22-atlas-revamp-design.md` (the three-lane revamp, merged on `claude/atlas-revamp`).
Everything there stands; this adds four things Daniel asked for after reviewing the first wave.

## 0. Daniel's asks (verbatim intent)

1. **Desk reach.** Pull up files, open and close apps or files, do data analysis on a file.
2. **Correct, fast answers from connected data.** "How many emails do I have in my inbox?" must be
   right and fast.
3. **Stop answering the room.** Once awake, Atlas must not respond to speech that is not addressed
   to it — reinstate the July addressed-speech gate. If Atlas auto-sleeps, it says nothing.
4. **Be an app, not a browser tab.** Atlas is on when its window is open and off when it is closed;
   opening it loads everything it needs. (Like the phone bridge — a separate app.)

## 1. Listening discipline (reinstated from `2026-07-21-atlas-conversation-rules-design.md` §1)

`worker/engagement.py` regains the silence clock (the kb version): `Engagement(timeout_s, clock)`
with `wake()`, `interacted()`, `dismiss()`, `tick() -> state`. The clock is re-stamped only by
*directed* interaction (wake, an addressed utterance, an Atlas reply, a completion callback) — never by
ambient speech — so a noisy room still sleeps `timeout_s` after Daniel last engaged.

`worker/addressing.py` is the July module, verbatim in behaviour: `Addressing(window_s, vocab,
clock)`, `mark_activity()`, `is_addressed(normalized_utterance) -> bool`:
- within `window_s` of the last Atlas-side activity (wake, Atlas reply) → everything is addressed;
- otherwise addressed iff the normalized utterance contains `atlas` or hits the vocabulary
  (single-word tokens or word-boundary phrases). Vocabulary = every `words` entry of
  `config/apps.yaml` plus `address_vocab` in `atlas.yaml` (email, inbox, calendar, file, files, open,
  close, launch, cancel, status, workers, job, haiku-free plain nouns Daniel actually uses).
- `is_addressed` never mutates state; only `mark_activity()` (called by `app.py` on wake and after
  every spoken Atlas line) re-arms the window.

Turn order in `app.py`: reflex → `engagement.tick()` → addressed? → brain. Not addressed → transcript
line with role `ambient`, no model call, no TTS. Addressed → `engagement.interacted()`.

**Silent auto-sleep.** A 5 s timer task calls `engagement.tick()`; on ENGAGED → ASLEEP it mutes the
mic, sets state ASLEEP, writes a transcript line `auto-sleep` and speaks **nothing**. The explicit
dismiss phrases keep their spoken line. Config: `engagement_timeout_s: 120`, `address_window_s: 30`.

## 2. Desk reach — `worker/localfiles.py` + new built-ins

Roots are configuration, not model input: `atlas.yaml` `file_roots: [~/Desktop, ~/Documents,
~/Downloads, C:/Users/danie/kb]`. Every path argument is resolved, must be inside a root (after
`resolve()`), and must exist.

| tool | input | policy | behaviour |
|---|---|---|---|
| `find_file` | `{query: str}` | instant | Case-insensitive substring match on file/dir names under the roots, depth ≤ 6, skipping `.git`, `node_modules`, `.venv`, `__pycache__`; newest-first; ≤ 20 results `{path, size, modified}`; whole search bounded to 2 s |
| `open_file` | `{path: str}` | instant | `os.startfile(path)` (default app) for a path inside a root → `{"opened": path}` |
| `read_file` | `{path: str}` | instant | Text files only (≤ 16 KiB read, utf-8 with replacement, control chars stripped); `.csv/.tsv/.txt/.md/.json/.yaml/.py/.log` and other text; returns `{path, bytes, text}` so Claude can answer quick questions ("what's the total in column B") itself |
| `close` | `{app: str}` | instant | Graceful close of a signed-profile app (`desktopapps.close_profile(app_id)` → `taskkill /IM <exe>` **without** `/F` so the app can prompt to save); error for url-only aliases ("I can close apps, not browser tabs") |

Data analysis beyond a quick read (plots, multi-file, anything that needs code) stays in the work
lane: `launch_work` with the absolute path in the brief; the job's Claude Code session reads it.
`BASE_SYSTEM` gains one rule: *use `find_file`/`read_file` for quick questions about a file;
`launch_work` for analysis that needs code or produces artifacts.*

## 3. Correct counts — `count_mail`

Evidence: `google__search_gmail_messages` returns `Found N messages` for one page only (page cap), so
the model cannot count a real inbox. New built-in `count_mail` `{query: str}` (instant): calls the
registry's `google__search_gmail_messages` with `page_size=500`, parses `Found N messages`, follows
`page_token` (the tool returns `Next page token: …` when more exist) up to 4 pages, and returns
`{"query", "count", "exact": bool}` (`exact=false` means ≥ 2000). The tool is registered only when the
`google` server is connected (registered from `McpServers.connect` through a hook, not hard-coded in
`tools.builtin`). `BASE_SYSTEM`: *for "how many emails/messages", use `count_mail` with a Gmail query
(`in:inbox is:unread` for unread, `in:inbox` for all); never count from a search page.*

## 4. The Atlas app — `worker/desktop.py`

pywebview 6.2 over the installed WebView2 runtime (151.x). `python -m worker.desktop`:
1. spawns the voice worker as a child: `<venv>\python.exe -m worker.app console` with the same
   device-pinning argv `app.main()` computes, stdout piped;
2. the child prints one line `ATLAS_UI <fragment-url>` once the state server is up and paired
   (app.py emits it right after `stateserver.start`; nothing else changes in app.py);
3. opens one native window (title "Atlas", 1100×760, min 800×600) on that URL;
4. when the window closes, terminates the child process tree (`taskkill /T /PID`, then `/F` after 10 s)
   and exits. Child death closes the window with a visible "Atlas stopped" page.
PM2 is retired: `pm2.config.cjs`, `run-worker.js`, `tests/test_pm2_config.py` are deleted. A Start-menu
shortcut is created by `scripts/install_shortcut.ps1` (`pythonw.exe -m worker.desktop`, icon from
`ui/favicon.svg` rendered to `.ico` is out of scope — default icon). README documents: open the app =
Atlas on; close it = Atlas off (running background jobs are cancelled on close, with one confirm
dialog inside the window if any are active).

## 5. Acceptance

1. Say something to the room 40 s after the last exchange, without "Atlas" → transcript shows it as
   `ambient`, no reply. "Atlas, what time is it" → reply.
2. Stay silent 2 min while awake → state ASLEEP, no speech, transcript `auto-sleep`.
3. "how many unread emails do I have" → `count_mail` → exact number spoken in < 3 s after MCP is up.
4. "find the haiku file" → `find_file` hit; "open it" → default app opens it; "close notepad" → closes.
5. `python -m worker.desktop` opens a native window showing the command center; closing it stops the
   worker (no `worker.app` process left).
6. Full suite green, `git diff --check`, `node --check`, adversarial review with no open high/medium.
