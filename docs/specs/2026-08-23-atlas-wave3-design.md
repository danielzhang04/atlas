# Atlas wave 3 — design (2026-08-23)

**Amends:** wave 1 (`2026-08-22-atlas-revamp-design.md`) and wave 2 (`2026-08-22-atlas-wave2-design.md`).
Evidence gathered live on 2026-08-23 with the app running: screenshots of the Home view, a click on
"History" that changed nothing, and per-process memory (`worker.app` 600 MB idle, WebView2 ~400 MB).

## 0. Daniel's asks

1. The top bar is too tall; **Live / History / Settings do nothing**.
2. The Atlas Engine visual is weak: remove the tilted "A"/diamond; make it *cool* — a circle with many
   rectangles that respond to sound (dB / bands), Jarvis-lineage, better than the earlier Codex orb.
3. Running Atlas should not be bulky — analyse and cut the overhead.
4. Bluetooth: if a BT speaker/mic is connected, I/O must follow whatever the system is currently on.
5. Test real workflows: they must spawn the right agents with the right infrastructure and produce the
   right results — spawn and monitor them.

## 1. UI/UX (vanilla JS + Canvas, no build step, offline inside WebView2)

**Shell.** Top bar shrinks to 40 px: mark + "Atlas" left, three text tabs centred, connection dot right.
View switching must actually work: `.view[hidden] { display: none !important }` (the Home grid rule
currently wins over the `hidden` attribute), tab state mirrored in `location.hash` (`#live`, `#history`,
`#settings`) so reloads keep the view.

**Atlas Engine.** A single Canvas, full panel, dark field (drop the grid background). Geometry:
- core disc r = 0.18·S (S = min(w,h)), soft inner glow; a thin ring at 0.30·S;
- **96 radial bars** between 0.34·S and 0.34·S + length, length = 0.02·S … 0.16·S, mirrored left/right
  (48 unique bands mapped symmetrically), rounded caps, 2–3 px wide at S = 320;
- an outer faint arc trio that rotates slowly while THINKING;
- no glyph, no letter, no diamond in the core.
Motion: per-bar attack 0.35 / decay 0.08 smoothing; idle "breathing" (±3 % of bar length at 0.2 Hz) when
ASLEEP; LISTENING bars follow the live bands; SPEAKING pulses the core radius with energy; THINKING spins
the arcs and runs a soft sweep across the bars. Colour per state: ASLEEP neutral grey, LISTENING Atlas
purple `#7C5CFF`, THINKING violet→white sweep, SPEAKING warm white with purple edge, OFFLINE dim red ring.
Drive: `GET /signal` polled at 20 Hz returns `{"energy": 0..1, "bands": [24 floats 0..1]}`; if `bands` is
absent the page synthesises 24 bands from `energy` (pink-noise shaped) so the visual never goes flat.
Performance: `devicePixelRatio`-aware canvas, pre-computed bar geometry, `requestAnimationFrame` paused
when `document.hidden`, ≤ 2 ms/frame budget.

**History view.** List of terminal jobs (title, state, when, summary) from `/jobs`; click → fetches
`/jobs/{id}/result` with the paired bearer and shows the result text; cancel button on active ones.
**Settings view.** Read-only cards: voice/wake settings from `atlas.yaml`, MCP servers + tool counts
(`/mcp`), Claude launcher availability, **current audio devices** (input/output names + "following
system default"), config file paths. No marketing copy.

## 2. Audio I/O follows the system (input AND output)

Today: TTS output already hot-follows the Windows default output (`devicewatch.OutputFollower`); the mic
is hard-pinned (`wake_input_device: Intel`) because Bluetooth hands-free (HFP) mics were unusable.
Daniel wants the system's current device, so:

- `devicewatch.current_default_input()` (pycaw `eCapture`/`eConsole`) beside `current_default_output()`;
  one `DeviceWatcher` polls both every 1.5 s.
- **Wake-word input follows the default input**: `wakeword.listen` opens the *current default* device,
  and on change closes/reopens the `InputStream` on the new device. Open at the device's native sample
  rate and resample to 16 kHz for the model (numpy linear resampling is sufficient for wake detection);
  HFP devices (8/16 kHz mono) therefore work instead of being refused.
- **LiveKit mic follows too**: the console's input stream is opened once at startup; mirror the
  `OutputFollower` trick for input via the installed `livekit.agents.cli._legacy.AgentsConsole` if it
  exposes an input reopen; if it does not, the worker requests a restart (exit code 21) and
  **`desktop.py` restarts the child in place** (window stays, shows "reconnecting audio…" for the
  seconds it takes) instead of showing "Atlas stopped". Restart is rate-limited to once per 30 s.
- `atlas.yaml`: `wake_input_device: follow` and `tts_output_device: follow` are the defaults; a name
  substring still pins a device for people who want that.
- `/state.output_device` becomes `/state.audio = {input: {name, following}, output: {name, following}}`,
  shown in Settings and as a tiny line under the engine.

## 3. Overhead

Measured: `worker.app` 600 MB working set idle, ~13 s to `/state`; 0 % CPU idle. Targets: ≤ 350 MB and
≤ 8 s. Known suspects to verify with `python -X importtime` and RSS deltas: openwakeword loading every
bundled model (construct with `wakeword_models=[path]` only, `inference_framework="onnx"`), silero VAD
allocation, `livekit-plugins-elevenlabs` imported even when the active voice is Deepgram, the anthropic
client's import cost (2.6 s), MCP connect blocking startup (it must stay off the critical path). Apply
what measures; report before/after numbers. WebView2 is Chromium and stays; disable GPU/accelerated
compositing only if it measurably cuts RAM without hurting the 60 fps canvas.

## 4. Workflow tests (spawned and monitored by the boss)

Through `python -m worker.chat` against the real environment, each job monitored to a terminal state:
1. **Research**: "research the three best budget mechanical keyboards under $100 and write me a
   summary" → `launch_work` → background Claude Code session uses web search, result summary spoken.
2. **File analysis**: a CSV is placed under `~/Documents` → "analyse sales.csv in my Documents and tell
   me the total revenue and the best month" → `launch_work` (too big for `read_file`) → the session
   reads the absolute path, computes, result correct versus the known totals.
3. **Chrome**: "open YouTube in Chrome and tell me the title of the first trending video" →
   `launch_work` with `--chrome` → result names a real title.
4. **Confirm flow**: "draft an email to myself with subject Atlas test saying hello" → MCP
   `draft_gmail_message` → `needs_confirmation` readback → "yes, send it"/"confirm" on the next turn →
   draft exists in Gmail (verified via `search_gmail_messages in:drafts`).
5. **Quick reads** stay in-lane: "how many unread emails", "what's on my calendar tomorrow".
Each run records: tool calls, job state timeline, wall time, result text, and whether the right lane was
chosen. Failures become fix tasks.

## 5. Acceptance

- Tabs switch views; top bar 40 px; no glyph in the core; bars visibly react to speech in the live app.
- `worker.app` ≤ 350 MB idle and ≤ 8 s to `/state` (or the report explains exactly what remains).
- Switching the Windows default input/output to a Bluetooth headset while Atlas runs moves both the mic
  and TTS within ~5 s with no manual restart; Settings shows the new device names.
- Workflow tests 1–5 pass with correct results; review finds no open high/medium.
