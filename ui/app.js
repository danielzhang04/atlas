(root => {
  "use strict";

  if (typeof document === "undefined") return;

  const ROUTES = new Set([...document.querySelectorAll("[data-view]")].map((view) => view.dataset.view));
  const VOICE_STATES = new Set(["ASLEEP", "LISTENING", "THINKING", "SPEAKING"]);
  const ENGINE_STATES = new Set([...VOICE_STATES, "TOOL"]);
  // No "tool" role: the host stopped mirroring tool calls into the
  // transcript (worker/app.py _record_tool), so an unknown role falls
  // through to "system" like any other.
  const TRANSCRIPT_ROLES = new Set(["user", "atlas", "ambient", "system"]);
  const ACTIVE_JOB_STATES = new Set(["queued", "launching", "running"]);
  const ACTION_HEADER = "x-atlas-action-token";
  const PAIRING_STORAGE_KEY = "atlas.pairing";
  /*
   * Polling request budget:
   * Live (visible): 600 signal + 60 state + 30 jobs + (30 x active jobs) events/min.
   * Hidden: 0 signal + 12 state + 12 jobs + 0 events = 24/min (88.2% below 204).
   * Settings adds 12 health requests/min only while visible.
   */
  const SIGNAL_INTERVAL_MS = 150;
  const STATE_INTERVAL_MS = 1000;
  const JOBS_INTERVAL_MS = 2000;
  const HIDDEN_INTERVAL_MS = 5000;
  const SETTINGS_INTERVAL_MS = 5000;

  const refs = {
    connection: document.querySelector("#connection"), topbar: document.querySelector(".topbar"),
    windowControls: document.querySelector("#window-controls"), windowMinimize: document.querySelector("#window-minimize"),
    windowMaximize: document.querySelector("#window-maximize"), windowClose: document.querySelector("#window-close"),
    canvas: document.querySelector("#engine-canvas"), engineCard: document.querySelector(".engine-card"),
    stateLabel: document.querySelector("#state-label"),
    greeting: document.querySelector("#greeting"), toolStrip: document.querySelector("#tool-strip"),
    toolAnnouncer: document.querySelector("#tool-announcer"),
    textForm: document.querySelector("#text-turn-form"), textInput: document.querySelector("#text-turn-input"),
    pendingCard: document.querySelector("#pending-confirmation"), pendingText: document.querySelector("#pending-confirmation-text"),
    audioLine: document.querySelector("#audio-line"), transcript: document.querySelector("#transcript"),
    workerSummary: document.querySelector("#worker-summary"), workerTabs: document.querySelector("#worker-tabs"),
    workerOutput: document.querySelector("#worker-output"), history: document.querySelector("#history-list"),
    historyCount: document.querySelector("#history-count"), resultTitle: document.querySelector("#result-title"),
    resultPlaceholder: document.querySelector("#result-placeholder"), historyResult: document.querySelector("#history-result"),
    voiceStatus: document.querySelector("#voice-status"), wakeStatus: document.querySelector("#wake-status"),
    audioInput: document.querySelector("#audio-input"), audioInputMode: document.querySelector("#audio-input-mode"),
    audioOutput: document.querySelector("#audio-output"), audioOutputMode: document.querySelector("#audio-output-mode"),
    claudeStatus: document.querySelector("#claude-status"), mcpList: document.querySelector("#mcp-list"),
    appsList: document.querySelector("#apps-list"),
    pairingStatus: document.querySelector("#pairing-status"), repairButton: document.querySelector("#repair-button"),
  };

  let actionToken = "", actionExpiresAt = 0, pairingExpiryTimer = 0;
  let currentView = "live", jobs = [], selectedJobId = "", selectedResultId = "";
  let transcriptSignature = "", jobsSignature = "", signalTimer = 0, stateTimer = 0, jobsTimer = 0, settingsTimer = 0;
  let greetingTimer = 0, userName = "", activeToolIdentity = "";
  let pendingDismissTimer = 0, pendingDismissEnd = null;
  const pendingRequests = new Set();
  const eventsByJob = new Map();
  const resultsByJob = new Map();

  function nativeWindowApi() { return window.pywebview?.api || null; }
  function syncNativeWindowControls() {
    if (nativeWindowApi() !== null) refs.windowControls.classList.remove("no-native");
  }
  function callNativeWindow(method) {
    const api = nativeWindowApi();
    if (api === null || typeof api[method] !== "function") return Promise.resolve(undefined);
    return Promise.resolve(api[method]()).catch(() => undefined);
  }
  document.querySelectorAll(".no-drag").forEach((element) => {
    element.addEventListener("mousedown", (event) => event.stopPropagation());
  });
  refs.windowMinimize.addEventListener("click", () => callNativeWindow("minimize"));
  refs.windowMaximize.addEventListener("click", () => callNativeWindow("toggle_maximize"));
  refs.windowClose.addEventListener("click", () => callNativeWindow("request_close"));
  refs.topbar.addEventListener("dblclick", (event) => {
    if (event.target.closest(".no-drag")) {
      return;
    }
    callNativeWindow("toggle_maximize");
  });
  window.addEventListener("pywebviewready", syncNativeWindowControls);
  syncNativeWindowControls();
  window.setTimeout(() => {
    if (nativeWindowApi() === null) refs.windowControls.classList.add("no-native");
  }, 1500);
  function node(tag, className, text) {
    const element = document.createElement(tag);
    if (className) element.className = className;
    if (text !== undefined) element.textContent = text;
    return element;
  }

  const kbUnlockButton = node("button", "button", "Unlock kb");
  kbUnlockButton.id = "kb-unlock-button";
  kbUnlockButton.type = "button";
  const kbUnlockStatus = node("p", "status-note", "");
  refs.mcpList.parentElement.append(kbUnlockButton, kbUnlockStatus);
  let kbVoiceSignature = "";
  async function requestKbUnlock() {
    kbUnlockButton.disabled = true;
    kbUnlockStatus.textContent = "Unlocking kb";
    const result = await callNativeWindow("unlock_kb");
    kbUnlockStatus.textContent = typeof result === "string" && result ? result : "unlock cancelled";
    kbUnlockButton.disabled = false;
    refreshSettings();
  }
  function maybeRequestVoiceKbUnlock(lines) {
    const latest = [...lines].reverse().find((line) => isRecord(line) && line.role === "user" && typeof line.text === "string");
    if (!latest) return;
    const phrase = latest.text.toLowerCase().replace(/[^a-z0-9]+/g, " ").trim();
    if (!["atlas unlock kb", "unlock the dashboard"].includes(phrase)) return;
    const signature = `${stringValue(latest.t)}:${latest.text}`;
    if (signature === kbVoiceSignature) return;
    kbVoiceSignature = signature;
    requestKbUnlock();
  }
  kbUnlockButton.addEventListener("click", requestKbUnlock);

  function isRecord(value) { return value !== null && typeof value === "object" && !Array.isArray(value); }
  async function requestJson(path, {
    authenticated = false, clearUnauthorized = false, ignoreHttpError = false, parse = true, ...options
  } = {}) {
    const headers = {...(options.headers || {})};
    if (authenticated) headers[ACTION_HEADER] = actionToken;
    const response = await fetch(path, {...options, headers});
    if (response.status === 401 && (authenticated || clearUnauthorized)) clearPairing();
    if (response.status === 401 && authenticated) return null;
    if (!response.ok) {
      if (ignoreHttpError) return null;
      throw new Error(`request failed: ${response.status}`);
    }
    return parse ? response.json() : null;
  }

  function publicJson(path, options = {}) { return requestJson(path, {...options, authenticated: false}); }
  function authenticatedJson(path, options = {}) { return requestJson(path, {...options, authenticated: true}); }
  async function runOnce(key, action) {
    if (pendingRequests.has(key)) return;
    pendingRequests.add(key);
    try { await action(); } finally { pendingRequests.delete(key); }
  }

  function stringValue(value) {
    return typeof value === "string" && value.trim() ? value.trim() : "—";
  }

  function displayString(value) { return typeof value === "string" && value.trim() ? value : "\u2014"; }

  function clamp(value, minimum = 0, maximum = 1) {
    return Math.max(minimum, Math.min(maximum, value));
  }

  function createEngine(canvas) {
    const BAR_COUNT = 96;
    const UNIQUE_BANDS = 48;
    const INPUT_BANDS = 24;
    // ---- CC6/DD-6 comet-trail tuning ----
    // CC6 baseline: Daniel's pick was "fewer, larger, glowing particles with
    // soft motion trails -- Siri-orb fluidity" (replacing CC4-era 42 small
    // pinpricks with no trail): PARTICLE_COUNT 42->16, bigger/brighter sprite.
    // DD-6: after seeing that on screen, Daniel's next call was "smaller,
    // more, and less bright and behind" -- i.e. the CC6 comets read as too
    // few, too big, and too foreground/bright. This pass turns the same
    // knobs the other way (count up, size/glow down, sprite + trail-layer
    // alpha softened) so the swarm reads as ambient background dust behind
    // the rings/waveform/core, not foreground comets. See TRAIL layer alpha
    // and particleGlow gradient-stop comments below for the brightness split.
    // Every knob for this redesign lives here so a Daniel-driven tweak is a
    // one-line edit; nothing else needs to move. The integrated-angle motion
    // itself (particleOrbitAngle/wrapAngle, see the CONVENTION note above
    // draw()) is untouched by this block.
    const PARTICLE_COUNT = 28; // DD-6: 16 -> 28 (CC6 was 16, pre-CC6 42)
    const PARTICLE_SIZE_BASE = 2.6; // px @ index%3===0, DD-6: 4.4 -> 2.6 (~-40%)
    const PARTICLE_SIZE_STEP = 1.6; // px added per size tier, DD-6: 2.6 -> 1.6 (~-40%)
    const PARTICLE_GLOW_RADIUS_PX = 32; // particleSprite backing size, DD-6: 48 -> 32 (proportional to size cut)
    const TRAIL_FADE_ALPHA = .16; // destination-out fade per 60fps frame, DD-6: .12 -> .16 (dimmer trails fade quicker so they don't linger brighter-looking than the live particles; dt-scaled at use)
    // DD-6: layer-level dim applied to the final trail blit below (see
    // drawParticles' context.globalAlpha before drawImage(trailCanvas, ...)),
    // on top of the per-particle sprite softening -- this is the "behind"
    // half of "less bright and behind": it recedes the whole layer under
    // drawBackdrop()'s output rather than only shrinking each particle.
    const TRAIL_LAYER_ALPHA = .6;
    // Trail canvas backing-store resolution multiplier against CSS-px
    // width/height. 1 = CSS-px (NOT pixelRatio-scaled like tickRingSprite/
    // segmentRingSprite -- see the resize()/drawParticles() notes on
    // trailCanvas for why that's a deliberate perf choice here, not an
    // oversight).
    const TRAIL_RESOLUTION_SCALE = 1;
    const TAU = Math.PI * 2;
    const palettes = {
      ASLEEP: {
        primary: [105, 119, 137], secondary: [139, 151, 166], core: [114, 130, 148],
        motion: [.08, .05, .03, .12, 0, 0],
      },
      LISTENING: {
        primary: [110, 72, 255], secondary: [66, 226, 255], core: [143, 104, 255],
        motion: [.46, .62, .52, .78, .08, 0],
      },
      THINKING: {
        primary: [255, 165, 55], secondary: [255, 222, 111], core: [255, 184, 71],
        motion: [.92, 1, .68, .64, 1, 0],
      },
      TOOL: {
        primary: [255, 196, 38], secondary: [255, 244, 184], core: [255, 211, 92],
        motion: [.78, .94, .74, .82, .72, 0],
      },
      SPEAKING: {
        primary: [255, 113, 72], secondary: [255, 236, 184], core: [255, 244, 220],
        motion: [.62, .82, .86, 1, .12, 1],
      },
      OFFLINE: {
        primary: [102, 57, 65], secondary: [131, 77, 84], core: [112, 62, 70],
        motion: [0, 0, 0, .05, 0, 0],
      },
    };
    const context = canvas.getContext("2d", {alpha: false, desynchronized: true});
    const inputBands = new Float32Array(INPUT_BANDS);
    const expandedBands = new Float32Array(UNIQUE_BANDS);
    const barValues = new Float32Array(BAR_COUNT);
    const cosine = new Float32Array(BAR_COUNT);
    const sine = new Float32Array(BAR_COUNT);
    const particleAngle = new Float32Array(PARTICLE_COUNT);
    const particleRadius = new Float32Array(PARTICLE_COUNT);
    const particleSpeed = new Float32Array(PARTICLE_COUNT);
    const particlePhase = new Float32Array(PARTICLE_COUNT);
    // Persistent orbit accumulator, one per particle: the *additional*
    // rotation beyond each particle's initial phase (particleAngle[i]),
    // integrated every frame as rate * dt instead of read back out of
    // absolute uptime. See the CONVENTION note above draw() for why this
    // must stay a fixed-size Float32Array rather than a per-frame value.
    const particleOrbitAngle = new Float32Array(PARTICLE_COUNT);
    // Waveform per-bar scratch geometry, reused every frame (no per-frame
    // allocation) so the bar-stroke pass and tip-fill pass can read the same
    // computed coordinates without rebuilding a Path2D for each.
    const waveInnerX = new Float32Array(BAR_COUNT);
    const waveInnerY = new Float32Array(BAR_COUNT);
    const waveOuterX = new Float32Array(BAR_COUNT);
    const waveOuterY = new Float32Array(BAR_COUNT);
    const primaryColor = new Float32Array(3);
    const secondaryColor = new Float32Array(3);
    const coreColor = new Float32Array(3);
    const fromPrimaryColor = new Float32Array(palettes.OFFLINE.primary);
    const fromSecondaryColor = new Float32Array(palettes.OFFLINE.secondary);
    const fromCoreColor = new Float32Array(palettes.OFFLINE.core);
    const toPrimaryColor = new Float32Array(palettes.OFFLINE.primary);
    const toSecondaryColor = new Float32Array(palettes.OFFLINE.secondary);
    const toCoreColor = new Float32Array(palettes.OFFLINE.core);
    const motion = new Float32Array(palettes.OFFLINE.motion);
    const fromMotion = new Float32Array(palettes.OFFLINE.motion);
    const toMotion = new Float32Array(palettes.OFFLINE.motion);
    const backdropSprite = document.createElement("canvas");
    const glowSprite = document.createElement("canvas");
    const particleSprite = document.createElement("canvas");
    // Tick ring + segment ring geometry is pre-rendered here (once per resize,
    // or while a color transition is active) instead of live-stroking up to
    // 120 segments every frame; the frame path only rotates + drawImages them.
    const tickRingSprite = document.createElement("canvas");
    const segmentRingSprite = document.createElement("canvas");
    // CC6: persistent comet-trail layer. Unlike the ring sprites above (bake
    // once, reused unchanged for many frames), this canvas is faded and
    // re-stamped every frame, so its context is cached once here instead of
    // re-fetched from a render function -- same reasoning as caching `context`
    // itself for the main canvas above. Left at the default alpha:true (unlike
    // the main canvas's alpha:false) since a transparent backing is what lets
    // destination-out fading and the final drawImage composite correctly over
    // whatever the main canvas already has painted -- see drawParticles().
    const trailCanvas = document.createElement("canvas");
    const trailContext = trailCanvas.getContext("2d");
    const FRAME_BUCKET_MS = [17, 25, 33, 50];
    const metrics = {
      samples: 0, lastMs: 0, averageMs: 0, maxMs: 0, running: false,
      frameHistogram: new Uint32Array(5), drops: 0, totalFrames: 0,
      maxAngleStepDeg: 0,
    };
    let realState = "OFFLINE";
    let visualState = "OFFLINE";
    let energy = 0;
    let frameEnergy = 0;
    // Integrated rotation accumulators for the tick ring, segment ring,
    // scanner sweep, and core dashed ring (the 4 non-particle rotating
    // elements). Each is advanced every frame by its own rate(t) * dt and
    // wrapped into [0, TAU) — see wrapAngle below and each drawX() site —
    // instead of being read straight out of absolute uptime.
    let tickRingAngle = 0;
    let segmentRingAngle = 0;
    let scannerAngle = 0;
    let coreDashAngle = 0;
    let width = 0;
    let height = 0;
    let scale = 1;
    let centerX = .5;
    let centerY = .5;
    let pixelRatio = 1;
    let transitionAt = performance.now();
    let layerPresence = 0;
    let fromLayerPresence = 0;
    let toLayerPresence = 0;
    let animationFrame = 0;
    let running = false;
    let wantsActive = false;
    let lastFrameNow = 0;
    let coreDashPattern = [];
    let majorTickPath = new Path2D();
    let minorTickPath = new Path2D();
    let innerSegmentPath = new Path2D();
    let tickRingSpriteSize = 0;
    let segmentRingSpriteSize = 0;
    // Every cache below is keyed on the COLOR STRINGS it was built with, not
    // on a time window. A `now - transitionAt <= 420` key is only correct
    // while the rAF loop is running: pausing mid-transition (ASLEEP/OFFLINE,
    // tab hidden) froze these at whatever half-blended color the last frame
    // baked, and the window had long expired by the time the loop resumed,
    // so the stale raster/gradient stayed on screen indefinitely. Comparing
    // the color actually wanted this frame against the color actually baked
    // in the cache cannot go stale: it rebuilds exactly when the pixels
    // would differ, and self-heals on the first frame after any pause with
    // no coupling to pauseLoop()/start() at all. Empty string = never built.
    let tickRingMinorColor = "", tickRingMajorColor = "";
    let segmentRingColor = "";
    // Cached gradient objects. Canvas gradient stop coordinates are evaluated
    // against the CTM active at fill()-time, not at creation-time (confirmed:
    // gradients follow the paint-time transform, same as patterns), so both
    // of these can be created once and reused across frames — drawScanner's
    // local-space gradient re-orients itself via the per-frame rotate(), and
    // drawCore's is built in a unit-radius local space and re-scaled per
    // frame via context.scale(coreRadius, coreRadius) so its continuously
    // "breathing" radius never forces a rebuild. Both are only rebuilt when
    // geometry that actually needs new coordinates changes (scannerGradient:
    // resize) or when one of their own color stops changes — see
    // drawScanner/drawCore and the cache-key note above.
    let scannerGradient = null, scannerGradientRadius = -1;
    let scannerStopInner = "", scannerStopMid = "", scannerStopOuter = "";
    let coreGradient = null;
    let coreStopCenter = "", coreStopInner = "", coreStopOuter = "", coreStopEdge = "";

    for (let index = 0; index < BAR_COUNT; index += 1) {
      const angle = -Math.PI / 2 + index * TAU / BAR_COUNT;
      cosine[index] = Math.cos(angle);
      sine[index] = Math.sin(angle);
    }
    for (let index = 0; index < PARTICLE_COUNT; index += 1) {
      const seed = (index * 47) % PARTICLE_COUNT;
      particleAngle[index] = seed / PARTICLE_COUNT * TAU;
      particleRadius[index] = .265 + ((index * 29) % 17) / 17 * .13;
      particleSpeed[index] = .18 + ((index * 13) % 11) / 11 * .52;
      particlePhase[index] = ((index * 31) % 23) / 23 * TAU;
    }

    glowSprite.width = 256;
    glowSprite.height = 256;
    const glowContext = glowSprite.getContext("2d");
    const glow = glowContext.createRadialGradient(128, 128, 0, 128, 128, 128);
    glow.addColorStop(0, "rgb(255 255 255 / 0.36)");
    glow.addColorStop(.22, "rgb(255 255 255 / 0.18)");
    glow.addColorStop(.56, "rgb(255 255 255 / 0.06)");
    glow.addColorStop(1, "rgb(255 255 255 / 0)");
    glowContext.fillStyle = glow;
    glowContext.fillRect(0, 0, 256, 256);

    // CC6 reused this exact radial-gradient-sprite technique (same as
    // glowSprite above), just bigger and hotter than pre-CC6
    // (0.95/0.5/0.1/0 @ 0/.16/.52/1), so fewer, larger particles read as
    // glowing comets rather than pinpricks.
    // DD-6: Daniel called those comets too bright/foreground. Stops softened
    // roughly in half (CC6 was 1/.62/.16/0 @ the same 0/.18/.55/1 positions)
    // -- this is the per-particle half of the brightness cut (softer core,
    // no more near-opaque center), paired with TRAIL_LAYER_ALPHA above for
    // a whole-layer dim. Splitting it this way keeps individual dust motes
    // soft-edged AND keeps the whole swarm receded behind the backdrop,
    // rather than one big alpha multiply that would just shrink a still-hot
    // pinpoint.
    particleSprite.width = PARTICLE_GLOW_RADIUS_PX;
    particleSprite.height = PARTICLE_GLOW_RADIUS_PX;
    const particleContext = particleSprite.getContext("2d");
    const particleCenter = PARTICLE_GLOW_RADIUS_PX / 2;
    const particleGlow = particleContext.createRadialGradient(
      particleCenter, particleCenter, 0, particleCenter, particleCenter, particleCenter,
    );
    particleGlow.addColorStop(0, "rgb(255 255 255 / 0.55)");
    particleGlow.addColorStop(.18, "rgb(255 255 255 / 0.34)");
    particleGlow.addColorStop(.55, "rgb(255 255 255 / 0.09)");
    particleGlow.addColorStop(1, "rgb(255 255 255 / 0)");
    particleContext.fillStyle = particleGlow;
    particleContext.fillRect(0, 0, PARTICLE_GLOW_RADIUS_PX, PARTICLE_GLOW_RADIUS_PX);

    function copyValues(target, source) {
      for (let index = 0; index < target.length; index += 1) target[index] = source[index];
    }

    // Keeps every integrated rotation accumulator (particleOrbitAngle[i],
    // tickRingAngle, segmentRingAngle, scannerAngle, coreDashAngle) inside
    // [0, TAU) forever instead of growing without bound over days of uptime
    // (float precision loss, not a visual bug at any single frame — but the
    // whole point of integrating is to stay exact for the life of the app).
    function wrapAngle(angle) {
      const wrapped = angle % TAU;
      return wrapped < 0 ? wrapped + TAU : wrapped;
    }

    function mixColors(now) {
      const linearProgress = clamp((now - transitionAt) / 420);
      const progress = linearProgress * linearProgress * (3 - 2 * linearProgress);
      for (let channel = 0; channel < 3; channel += 1) {
        primaryColor[channel] = fromPrimaryColor[channel] + (toPrimaryColor[channel] - fromPrimaryColor[channel]) * progress;
        secondaryColor[channel] = fromSecondaryColor[channel] + (toSecondaryColor[channel] - fromSecondaryColor[channel]) * progress;
        coreColor[channel] = fromCoreColor[channel] + (toCoreColor[channel] - fromCoreColor[channel]) * progress;
      }
      for (let index = 0; index < motion.length; index += 1) {
        motion[index] = fromMotion[index] + (toMotion[index] - fromMotion[index]) * progress;
      }
      layerPresence = fromLayerPresence + (toLayerPresence - fromLayerPresence) * progress;
    }

    function colorString(color, alpha = 1) {
      return `rgb(${Math.round(color[0])} ${Math.round(color[1])} ${Math.round(color[2])} / ${alpha})`;
    }

    function resize() {
      const bounds = canvas.getBoundingClientRect();
      const nextWidth = Math.max(1, Math.round(bounds.width));
      const nextHeight = Math.max(1, Math.round(bounds.height));
      const nextPixelRatio = Math.min(2, Math.max(1, window.devicePixelRatio || 1));
      if (
        nextWidth === width
        && nextHeight === height
        && canvas.width === Math.round(nextWidth * nextPixelRatio)
      ) return;
      width = nextWidth;
      height = nextHeight;
      scale = Math.min(width, height);
      centerX = width / 2;
      centerY = height / 2;
      pixelRatio = nextPixelRatio;
      coreDashPattern = [.025 * scale, .014 * scale, .006 * scale, .018 * scale];
      canvas.width = Math.round(width * pixelRatio);
      canvas.height = Math.round(height * pixelRatio);
      context.setTransform(pixelRatio, 0, 0, pixelRatio, 0, 0);
      context.imageSmoothingEnabled = true;

      // CC6: trail canvas is deliberately sized at TRAIL_RESOLUTION_SCALE
      // (1 = CSS-px), NOT `* pixelRatio` like the main canvas above or the
      // tickRingSprite/segmentRingSprite pattern below -- unlike those, this
      // canvas is fully re-painted (fade + re-stamp) every single frame, not
      // just on resize or a color change, so its pixel count is a real
      // per-frame cost: at pixelRatio 2 that's 4x the fill area of the
      // destination-out fade for no visible gain, since a comet trail is a
      // soft blur by design and drawParticles()'s final drawImage already
      // upscales it into the pixelRatio-scaled main canvas -- the softening
      // from that upscale is a feature here, not the blur regression the
      // ring sprites had to fix. Setting width/height also clears any
      // existing trail content, which is fine: a resize already interrupts
      // the visual continuity a trail exists to preserve.
      trailCanvas.width = Math.round(width * TRAIL_RESOLUTION_SCALE);
      trailCanvas.height = Math.round(height * TRAIL_RESOLUTION_SCALE);

      majorTickPath = new Path2D();
      minorTickPath = new Path2D();
      for (let index = 0; index < 120; index += 1) {
        const angle = index / 120 * TAU;
        const isMajor = index % 10 === 0;
        const outer = .475 * scale;
        const inner = (isMajor ? .448 : index % 5 === 0 ? .458 : .465) * scale;
        const path = isMajor ? majorTickPath : minorTickPath;
        path.moveTo(Math.cos(angle) * inner, Math.sin(angle) * inner);
        path.lineTo(Math.cos(angle) * outer, Math.sin(angle) * outer);
      }

      innerSegmentPath = new Path2D();
      for (let index = 0; index < 18; index += 1) {
        const start = index / 18 * TAU;
        const length = TAU / 18 * (.62 + (index % 3) * .08);
        innerSegmentPath.moveTo(Math.cos(start) * .295 * scale, Math.sin(start) * .295 * scale);
        innerSegmentPath.arc(0, 0, .295 * scale, start, start + length);
      }

      // Geometry changed size, so the ring sprites must be rebuilt now
      // regardless of transition state (drawTickRing/drawSegmentRings only
      // rebuild on their own during a color transition).
      renderTickRingSprite();
      renderSegmentRingSprite();

      // NOTE (pre-existing, not part of this pass): like the ring sprites
      // above before this fix, this backing store is CSS-px sized, not
      // pixelRatio-scaled, so it rasterizes at 1x and gets bilinearly
      // upscaled at any display scale above 100%. Left as-is deliberately:
      // this sprite's own geometry (middle/unit/hex coordinates below) is
      // defined directly in spriteSize units with no separate logical-size
      // variable, so a proper fix means introducing one and rewriting every
      // coordinate below, not a one-line change like the ring sprites got.
      // It stays imperceptible in practice — drawBackdrop() paints it at
      // .075-.36 alpha as a faint decorative texture.
      const spriteSize = Math.max(256, Math.ceil(scale * 1.08));
      backdropSprite.width = spriteSize;
      backdropSprite.height = spriteSize;
      const backdrop = backdropSprite.getContext("2d");
      const middle = spriteSize / 2;
      const unit = spriteSize / 12;
      backdrop.clearRect(0, 0, spriteSize, spriteSize);
      backdrop.strokeStyle = "rgb(80 134 155 / 0.075)";
      backdrop.lineWidth = 1;
      const hexPath = new Path2D();
      for (let row = -1; row < 15; row += 1) {
        for (let column = -1; column < 8; column += 1) {
          const x = column * unit * 1.5 + (row % 2 ? unit * .75 : 0);
          const y = row * unit * .866;
          for (let side = 0; side <= 6; side += 1) {
            const angle = side * TAU / 6;
            const pointX = x + Math.cos(angle) * unit;
            const pointY = y + Math.sin(angle) * unit;
            if (side === 0) hexPath.moveTo(pointX, pointY); else hexPath.lineTo(pointX, pointY);
          }
        }
      }
      backdrop.save();
      backdrop.translate(unit * .2, unit * .35);
      backdrop.stroke(hexPath);
      backdrop.restore();
      backdrop.strokeStyle = "rgb(106 190 216 / 0.085)";
      backdrop.beginPath();
      for (let index = 1; index <= 4; index += 1) {
        backdrop.moveTo(middle + index * spriteSize * .095, middle);
        backdrop.arc(middle, middle, index * spriteSize * .095, 0, TAU);
      }
      backdrop.moveTo(middle, spriteSize * .07);
      backdrop.lineTo(middle, spriteSize * .93);
      backdrop.moveTo(spriteSize * .07, middle);
      backdrop.lineTo(spriteSize * .93, middle);
      backdrop.stroke();
      // canvas.width/height above just cleared the bitmap; the rAF loop self-pauses
      // while idle (ASLEEP/OFFLINE), so nothing will repaint it on its own — force
      // one frame now without resuming the loop.
      if (!running) draw(performance.now());
    }

    function expandBands() {
      for (let index = 0; index < UNIQUE_BANDS; index += 1) {
        const position = index * (INPUT_BANDS - 1) / (UNIQUE_BANDS - 1);
        const lower = Math.floor(position);
        const upper = Math.min(INPUT_BANDS - 1, lower + 1);
        const mix = position - lower;
        expandedBands[index] = inputBands[lower] + (inputBands[upper] - inputBands[lower]) * mix;
      }
    }

    function synthesizeBands(now) {
      const seconds = now / 1000;
      for (let index = 0; index < INPUT_BANDS; index += 1) {
        const pinkShape = 1 / Math.sqrt(1 + index * .12);
        const movement = .82 + .18 * Math.sin(seconds * (2.1 + index * .017) + index * 1.73);
        inputBands[index] = clamp(energy * pinkShape * movement * 1.25);
      }
      expandBands();
    }

    function prepareFrameSignal(now, previewState, dt) {
      // Frame-rate-independent exponential smoothing, the exact idiom
      // drawWaveform uses (growFactor/shrinkFactor below): factor =
      // 1 - (1-k)^(dt/16.67). k = .3 => tau ~= 47ms, ~90% settle in
      // ~108ms, ~95% in ~140ms at 60fps -- inside the ~100-150ms target and
      // still visibly live. Ambient mic noise updates `energy` on every
      // audio tick with no smoothing of its own; reading it straight into
      // frameEnergy (the old direct assignment) let a single noisy tick
      // re-scramble every rotation rate and pulse this value drives
      // (drawParticles' drift/size/alpha, drawCore's speakingPulse/glow) —
      // this is the second half of the fix alongside integrating rotation
      // (see the CONVENTION note above draw()): the rate itself can still
      // legitimately move with audio, it just can no longer jump instantly.
      const smoothing = 1 - Math.pow(1 - .3, dt / 16.67);
      frameEnergy += (energy - frameEnergy) * smoothing;
      if (!previewState) {
        expandBands();
        return;
      }
      if (previewState !== "LISTENING" && previewState !== "SPEAKING") {
        frameEnergy = previewState === "THINKING" ? .08 : 0;
        return;
      }
      const seconds = now / 1000;
      const speaking = previewState === "SPEAKING";
      const pulse = .5 + .5 * Math.sin(seconds * (speaking ? 3.4 : 1.7));
      frameEnergy = speaking ? .28 + pulse * .18 : .12 + pulse * .08;
      for (let index = 0; index < UNIQUE_BANDS; index += 1) {
        const pinkShape = 1 / Math.sqrt(1 + index * .1);
        const ripple = .68
          + .2 * Math.sin(seconds * (speaking ? 5.2 : 2.2) + index * .43)
          + .12 * Math.sin(seconds * 1.3 + index * 1.71);
        expandedBands[index] = clamp(frameEnergy * pinkShape * ripple * (speaking ? 1.45 : 1.2));
      }
    }

    function setSignal(rawEnergy, bands) {
      energy = Number.isFinite(rawEnergy) ? clamp(rawEnergy) : 0;
      if (!Array.isArray(bands) || bands.length === 0) {
        synthesizeBands(performance.now());
        return;
      }
      const finalIndex = bands.length - 1;
      for (let index = 0; index < INPUT_BANDS; index += 1) {
        const position = index * finalIndex / (INPUT_BANDS - 1);
        const lower = Math.floor(position);
        const upper = Math.min(finalIndex, lower + 1);
        const mix = position - lower;
        const lowValue = Number.isFinite(bands[lower]) ? clamp(bands[lower]) : 0;
        const highValue = Number.isFinite(bands[upper]) ? clamp(bands[upper]) : 0;
        inputBands[index] = lowValue + (highValue - lowValue) * mix;
      }
      expandBands();
    }

    function setVisualState(nextState) {
      if (nextState === visualState) return;
      const now = performance.now();
      mixColors(now);
      copyValues(fromPrimaryColor, primaryColor);
      copyValues(fromSecondaryColor, secondaryColor);
      copyValues(fromCoreColor, coreColor);
      copyValues(fromMotion, motion);
      fromLayerPresence = layerPresence;
      copyValues(toPrimaryColor, palettes[nextState].primary);
      copyValues(toSecondaryColor, palettes[nextState].secondary);
      copyValues(toCoreColor, palettes[nextState].core);
      copyValues(toMotion, palettes[nextState].motion);
      toLayerPresence = nextState === "OFFLINE" ? 0 : 1;
      transitionAt = now;
      visualState = nextState;
    }

    function setState(nextState) {
      realState = ENGINE_STATES.has(nextState) ? nextState : "OFFLINE";
      canvas.setAttribute("aria-label", `Atlas engine ${realState.toLowerCase()}`);
      if (!ENGINE_STATES.has(window.__atlasEnginePreview)) setVisualState(realState);
      // Waking from ASLEEP/OFFLINE must resume the loop immediately, not wait for
      // the next start()/stop() call from view/visibility changes.
      if (wantsActive && !running && !isIdleNow()) start();
    }

    function drawBackdrop() {
      const size = scale * 1.08;
      context.globalAlpha = .36 + motion[1] * .22;
      context.drawImage(backdropSprite, centerX - size / 2, centerY - size / 2, size, size);
      context.globalAlpha = 1;
    }

    // Ring-sprite rebuild: bakes the tick-ring / segment-ring stroke geometry
    // (which never changes shape outside a resize) at the current theme
    // color/alpha into an offscreen canvas. Safe to skip most frames because
    // primaryColor/secondaryColor/motion only move during a color transition;
    // once settled they are exactly constant, so re-stroking every frame
    // would draw an identical raster. The per-frame draw path only rotates +
    // drawImages it. The rebuild trigger is the baked color itself, not a
    // time window — see the cache-key note above.
    function tickRingMinorColorNow() {
      return colorString(primaryColor, (.15 + motion[1] * .1) * (visualState === "OFFLINE" ? .18 : 1));
    }

    function tickRingMajorColorNow() {
      return colorString(secondaryColor, (.35 + motion[1] * .2) * (visualState === "OFFLINE" ? .18 : 1));
    }

    // Strokes the tick ring into `target`, which the caller has already
    // translated to the ring centre. One routine for both destinations: the
    // offscreen sprite context and — during a transition — the live frame
    // context, so the two paths can never drift in width, cap or order.
    function strokeTickRing(target, minorColor, majorColor) {
      target.lineCap = "butt";
      target.lineWidth = Math.max(.45, scale * .0012);
      target.strokeStyle = minorColor;
      target.stroke(minorTickPath);
      target.lineWidth = Math.max(.7, scale * .0021);
      target.strokeStyle = majorColor;
      target.stroke(majorTickPath);
    }

    function renderTickRingSprite(
      minorColor = tickRingMinorColorNow(),
      majorColor = tickRingMajorColorNow(),
    ) {
      const majorWidth = Math.max(.7, scale * .0021);
      const outerRadius = .475 * scale;
      const size = Math.max(2, Math.ceil(outerRadius * 2 + majorWidth * 4));
      // Backing store must be device-pixel sized (size * pixelRatio), same as
      // the main canvas in resize(), or this rasterizes at 1x and gets
      // bilinearly upscaled by the drawImage in drawTickRing (soft/blurry at
      // any display scale above 100%). The offscreen context is scaled by
      // pixelRatio so every draw call below keeps using CSS-px-equivalent
      // `size` units; the sprite is still drawImage'd back at CSS-px `size`
      // (tickRingSpriteSize, unchanged) since the destination context already
      // has pixelRatio baked into its own CTM (also set in resize()).
      const deviceSize = Math.max(2, Math.round(size * pixelRatio));
      if (tickRingSprite.width !== deviceSize || tickRingSprite.height !== deviceSize) {
        tickRingSprite.width = deviceSize;
        tickRingSprite.height = deviceSize;
      }
      const spriteContext = tickRingSprite.getContext("2d");
      spriteContext.setTransform(pixelRatio, 0, 0, pixelRatio, 0, 0);
      spriteContext.clearRect(0, 0, size, size);
      spriteContext.save();
      spriteContext.translate(size / 2, size / 2);
      strokeTickRing(spriteContext, minorColor, majorColor);
      spriteContext.restore();
      tickRingSpriteSize = size;
      tickRingMinorColor = minorColor;
      tickRingMajorColor = majorColor;
    }

    function segmentRingColorNow() {
      return colorString(secondaryColor, (.22 + motion[1] * .42) * (visualState === "OFFLINE" ? .16 : 1));
    }

    // Same contract as strokeTickRing: `target` is already centred.
    function strokeSegmentRing(target, color) {
      target.lineCap = "round";
      target.lineWidth = Math.max(.65, scale * .0022);
      target.strokeStyle = color;
      target.stroke(innerSegmentPath);
    }

    function renderSegmentRingSprite(color = segmentRingColorNow()) {
      const lineWidth = Math.max(.65, scale * .0022);
      const outerRadius = .295 * scale;
      const size = Math.max(2, Math.ceil(outerRadius * 2 + lineWidth * 4));
      // See the matching comment in renderTickRingSprite: backing store is
      // device-pixel sized (size * pixelRatio) and the offscreen context is
      // scaled by pixelRatio so drawing stays in CSS-px-equivalent `size`
      // units; drawImage back in drawSegmentRings still targets CSS-px
      // `size` (segmentRingSpriteSize, unchanged).
      const deviceSize = Math.max(2, Math.round(size * pixelRatio));
      if (segmentRingSprite.width !== deviceSize || segmentRingSprite.height !== deviceSize) {
        segmentRingSprite.width = deviceSize;
        segmentRingSprite.height = deviceSize;
      }
      const spriteContext = segmentRingSprite.getContext("2d");
      spriteContext.setTransform(pixelRatio, 0, 0, pixelRatio, 0, 0);
      spriteContext.clearRect(0, 0, size, size);
      spriteContext.save();
      spriteContext.translate(size / 2, size / 2);
      strokeSegmentRing(spriteContext, color);
      spriteContext.restore();
      segmentRingSpriteSize = size;
      segmentRingColor = color;
    }

    // A ring is in one of two regimes. SETTLED (the overwhelming majority of
    // frames): the color is constant, so the sprite is valid and the frame
    // costs one rotate + one blit. TRANSITIONING: the color changes every
    // frame, so a sprite would have to be re-stroked every frame AND blitted
    // — the stroke cost plus a blit the live path never pays. So a
    // transition live-strokes into the frame context exactly like the
    // pre-sprite code did, and the sprite is re-rendered ONCE, on the first
    // frame after the color settles (its cached color no longer matches).
    function drawTickRing(now, dt) {
      const minorColor = tickRingMinorColorNow();
      const majorColor = tickRingMajorColorNow();
      const transitioning = now - transitionAt <= 420;
      if (
        !transitioning
        && (tickRingSpriteSize === 0
          || minorColor !== tickRingMinorColor
          || majorColor !== tickRingMajorColor)
      ) renderTickRingSprite(minorColor, majorColor);
      // Integrated in place of the old `now * rate` read: rate * dt keeps
      // the rotation continuous even while `motion[0]` (and so the rate
      // itself) changes between frames, instead of re-deriving the whole
      // angle from absolute uptime every frame (see the CONVENTION note
      // above draw() and the matching accumulators for the other 3 rings).
      tickRingAngle = wrapAngle(tickRingAngle + .000018 * (1 + motion[0]) * dt);
      context.save();
      context.translate(centerX, centerY);
      context.rotate(tickRingAngle);
      if (transitioning) {
        strokeTickRing(context, minorColor, majorColor);
      } else {
        context.drawImage(
          tickRingSprite,
          -tickRingSpriteSize / 2, -tickRingSpriteSize / 2,
          tickRingSpriteSize, tickRingSpriteSize,
        );
      }
      context.restore();
    }

    function drawSegmentRings(now, dt) {
      const color = segmentRingColorNow();
      const transitioning = now - transitionAt <= 420;
      if (
        !transitioning
        && (segmentRingSpriteSize === 0 || color !== segmentRingColor)
      ) renderSegmentRingSprite(color);
      const speed = .000035 + motion[0] * .00016;
      segmentRingAngle = wrapAngle(segmentRingAngle + speed * 1.32 * dt);
      context.save();
      context.translate(centerX, centerY);
      context.rotate(-segmentRingAngle);
      if (transitioning) {
        strokeSegmentRing(context, color);
      } else {
        context.drawImage(
          segmentRingSprite,
          -segmentRingSpriteSize / 2, -segmentRingSpriteSize / 2,
          segmentRingSpriteSize, segmentRingSpriteSize,
        );
      }
      context.restore();
    }

    function drawScanner(now, dt) {
      // Advanced unconditionally, even on the frames the guard below skips
      // rendering (motion[4] < .04), so the sweep resumes from wherever it
      // actually stopped instead of jumping to re-derive its angle from
      // absolute uptime once it becomes visible again.
      scannerAngle = wrapAngle(scannerAngle + (.00018 + motion[4] * .00034) * dt);
      if (motion[4] < .04) return;
      const rotation = scannerAngle;
      const radius = .44 * scale;
      // Gradient stop coordinates are evaluated against the CTM active at
      // fill()-time, not at createLinearGradient()-time (confirmed: gradients
      // follow the paint-time transform the same way patterns do), so a
      // gradient created once in the LOCAL (0,0)-(radius,0) space established
      // by translate+rotate below re-orients itself correctly every frame as
      // `rotation` changes with zero need to recreate it. Only rebuilt when
      // `radius` changes (resize) or one of its own color stops changes.
      const stopInner = colorString(primaryColor, 0);
      const stopMid = colorString(primaryColor, .02 + motion[4] * .09);
      const stopOuter = colorString(secondaryColor, .08 + motion[4] * .38);
      if (
        scannerGradient === null
        || scannerGradientRadius !== radius
        || stopInner !== scannerStopInner
        || stopMid !== scannerStopMid
        || stopOuter !== scannerStopOuter
      ) {
        scannerGradient = context.createLinearGradient(0, 0, radius, 0);
        scannerGradient.addColorStop(0, stopInner);
        scannerGradient.addColorStop(.72, stopMid);
        scannerGradient.addColorStop(1, stopOuter);
        scannerGradientRadius = radius;
        scannerStopInner = stopInner;
        scannerStopMid = stopMid;
        scannerStopOuter = stopOuter;
      }
      context.save();
      context.translate(centerX, centerY);
      context.rotate(rotation);
      // Restores the cap the pre-sprite drawSegmentRings used to leave on
      // the frame context (it set lineCap = "round" outside its own
      // save/restore, so every stroke after it inherited round caps). Now
      // that the rings stroke into their own sprite contexts, the arc below
      // and drawCore's strokes set it explicitly instead of inheriting a
      // leak — same pixels, no action at a distance.
      context.lineCap = "round";
      context.fillStyle = scannerGradient;
      context.beginPath();
      context.moveTo(0, 0);
      context.arc(0, 0, radius, -.11, .11);
      context.closePath();
      context.fill();
      context.beginPath();
      context.arc(0, 0, radius, -.08, .08);
      context.lineWidth = Math.max(.7, scale * .002);
      context.strokeStyle = colorString(secondaryColor, .08 + motion[4] * .54);
      context.stroke();
      context.restore();
    }

    function drawParticles(now, dt) {
      const seconds = now / 1000;
      const drift = .025 + motion[2] * .14 + frameEnergy * .2;
      // Integrated in place of the old `particleAngle[i] + seconds *
      // particleSpeed[i] * drift` absolute-uptime read: `drift` changes
      // every frame with motion[2]/frameEnergy, so reading it back against
      // absolute `seconds` re-scrambled the whole orbit history any time
      // drift changed (the actual choppiness bug — see the CONVENTION note
      // above draw()). Advanced unconditionally (even on frames the
      // layerPresence guard below skips rendering) so re-entry resumes
      // in place instead of snapping to a wall-clock-derived angle.
      const deltaSeconds = dt / 1000;
      let maxStepThisFrame = 0;
      for (let index = 0; index < PARTICLE_COUNT; index += 1) {
        const step = particleSpeed[index] * drift * deltaSeconds;
        if (step > maxStepThisFrame) maxStepThisFrame = step;
        particleOrbitAngle[index] = wrapAngle(particleOrbitAngle[index] + step);
      }
      // Acceptance instrumentation (see window.__atlasEngineMetrics below):
      // rolling max per-frame particle angle delta in degrees, reset
      // alongside frameHistogram in start(). Makes the ~1.5 deg/frame bound
      // measurable live instead of only by offline simulation.
      const maxStepDeg = maxStepThisFrame * (180 / Math.PI);
      if (maxStepDeg > metrics.maxAngleStepDeg) metrics.maxAngleStepDeg = maxStepDeg;
      // Paused/idle frames never reach here (isIdleNow() stops the rAF loop
      // entirely), so the trail simply stops being written to and freezes on
      // its last frame -- see pauseLoop()/start() for why that (rather than
      // clearing here) is the right call, and why start() clears it instead.
      if (layerPresence <= 0) return;
      const particleEnergy = .25 + motion[2] * .45 + frameEnergy * .45;

      // CC6: comet trail. Fade the persistent trail canvas via
      // destination-out, which multiplies existing pixel ALPHA toward zero
      // (RGB untouched) regardless of what color those pixels are -- this is
      // what makes it composite correctly under any backdrop, unlike
      // painting a translucent rect over it (source-over), which would only
      // look seamless if that rect's color exactly matched the backdrop's
      // actual composited appearance (the #070a10 fill + backdropSprite
      // texture + the CSS radial-gradient behind the canvas element) and
      // would silently drift out of sync the moment any of those change.
      trailContext.globalCompositeOperation = "destination-out";
      // Frame-rate-independent, same idiom as drawWaveform/prepareFrameSignal:
      // a flat per-frame alpha made the trail length a function of frame rate
      // (a long comet at 30fps, a short one at 120), which is the one thing
      // this canvas is for.
      trailContext.globalAlpha = 1 - Math.pow(1 - TRAIL_FADE_ALPHA, dt / 16.67);
      trailContext.fillStyle = "#000";
      trailContext.fillRect(0, 0, trailCanvas.width, trailCanvas.height);

      // Stamp each particle onto the trail (not the main canvas): "screen"
      // still brightens overlapping stamps within the trail itself (same
      // glow-accumulation effect the pre-CC6 code got by screen-compositing
      // straight onto the main canvas), and the composited trail is blitted
      // onto the main canvas below with a normal (source-over) drawImage.
      trailContext.globalCompositeOperation = "screen";
      for (let index = 0; index < PARTICLE_COUNT; index += 1) {
        const orbit = particleAngle[index] + particleOrbitAngle[index];
        const tremor = Math.sin(seconds * (1 + particleSpeed[index]) + particlePhase[index]) * (.003 + frameEnergy * .006);
        const radius = (particleRadius[index] + tremor) * scale;
        const size = (PARTICLE_SIZE_BASE + index % 3 * PARTICLE_SIZE_STEP) * (1 + frameEnergy * .4);
        trailContext.globalAlpha = layerPresence * particleEnergy * (.18 + .22 * Math.sin(particlePhase[index] + seconds * .7) ** 2);
        trailContext.drawImage(
          particleSprite,
          centerX + Math.cos(orbit) * radius - size,
          centerY + Math.sin(orbit) * radius - size,
          size * 2,
          size * 2,
        );
      }

      // Blit the (deliberately low-res, see TRAIL_RESOLUTION_SCALE above)
      // trail canvas into the main, pixelRatio-scaled context at CSS-px
      // width/height -- context.imageSmoothingEnabled (set in resize())
      // makes this upscale a soft blur, which a comet trail wants anyway.
      // Normal source-over compositing: the trail's own pixels already
      // carry the right RGBA (including their screen-blended overlaps), so
      // this is a plain alpha-composite onto whatever drawBackdrop() just
      // painted, not another blend-mode layer.
      // DD-6: TRAIL_LAYER_ALPHA dims the whole composited layer one more
      // step below its per-particle sprite alpha (see particleGlow stops
      // above) -- the "behind" half of "less bright and behind": the swarm
      // recedes as one layer under drawBackdrop()'s output instead of only
      // each particle shrinking individually. Reset to 1 right after so it
      // never leaks into drawTickRing/drawSegmentRings/etc, which assume
      // globalAlpha is 1 on entry (same convention as drawBackdrop/
      // drawWaveform/drawCore's own local globalAlpha resets).
      context.globalAlpha = TRAIL_LAYER_ALPHA;
      context.drawImage(trailCanvas, 0, 0, width, height);
      context.globalAlpha = 1;
    }

    function drawWaveform(now, dt) {
      const baseRadius = .323 * scale;
      const inwardRange = .018 * scale;
      const outwardRange = .092 * scale;
      const breathing = .5 + .5 * Math.sin(now * .00115);
      // Frame-rate-independent exponential decay: factor = 1 - (1-k)^(dt/16.67).
      // At 60fps (dt=16.67, rate=1) this equals the old fixed k exactly; at lower
      // frame rates it decays proportionally with no overshoot (a linear
      // `k * rate` would overshoot ~19% at 30fps and hard-snap to target for any
      // dt >= ~52ms, inside the 100ms clamp above) — the exponential form is
      // naturally < 1 for any finite rate, so no Math.min cap is needed.
      const rate = dt / 16.67;
      const growFactor = 1 - Math.pow(1 - .32, rate);
      const shrinkFactor = 1 - Math.pow(1 - .1, rate);
      for (let index = 0; index < BAR_COUNT; index += 1) {
        const mirroredIndex = index < UNIQUE_BANDS ? index : BAR_COUNT - 1 - index;
        let target = .025 + breathing * .02;
        if (visualState === "LISTENING") target = clamp(expandedBands[mirroredIndex] * 1.35 + .045);
        if (visualState === "SPEAKING") target = clamp(expandedBands[mirroredIndex] * 1.08 + frameEnergy * .22 + .06);
        if (visualState === "THINKING") target = .17 + .11 * (.5 + .5 * Math.sin(index * .31 + now * .002));
        if (visualState === "OFFLINE") target = 0;
        const current = barValues[index];
        barValues[index] += (target - current) * (target > current ? growFactor : shrinkFactor);
        const value = barValues[index];
        const innerRadius = baseRadius - inwardRange * value;
        const outerRadius = baseRadius + .008 * scale + outwardRange * value;
        waveInnerX[index] = centerX + cosine[index] * innerRadius;
        waveInnerY[index] = centerY + sine[index] * innerRadius;
        waveOuterX[index] = centerX + cosine[index] * outerRadius;
        waveOuterY[index] = centerY + sine[index] * outerRadius;
      }
      if (layerPresence <= 0) return;
      context.save();
      context.globalAlpha = layerPresence;
      context.lineCap = "round";
      // Two passes over the SAME current path (beginPath()/moveTo/lineTo, no
      // Path2D object): stroke() re-strokes whatever path is current without
      // rebuilding it, exactly reproducing the former two-pass Path2D stroke.
      context.beginPath();
      for (let index = 0; index < BAR_COUNT; index += 1) {
        context.moveTo(waveInnerX[index], waveInnerY[index]);
        context.lineTo(waveOuterX[index], waveOuterY[index]);
      }
      context.lineWidth = Math.max(3, scale * .009);
      context.strokeStyle = colorString(primaryColor, .05 + motion[1] * .11);
      context.stroke();
      context.lineWidth = Math.max(1.15, scale * .0035);
      context.strokeStyle = colorString(secondaryColor, .34 + motion[3] * .56);
      context.stroke();
      const tipMoveOffset = Math.max(1, scale * .0026);
      const tipRadius = Math.max(.8, scale * .0026);
      context.beginPath();
      for (let index = 0; index < BAR_COUNT; index += 1) {
        context.moveTo(waveOuterX[index] + tipMoveOffset, waveOuterY[index]);
        context.arc(waveOuterX[index], waveOuterY[index], tipRadius, 0, TAU);
      }
      context.fillStyle = colorString(secondaryColor, .28 + motion[3] * .62);
      context.fill();
      context.restore();
    }

    function drawCore(now, dt) {
      const breathing = .5 + .5 * Math.sin(now * .00115);
      const speakingPulse = frameEnergy * motion[5] * (.055 + .035 * Math.sin(now * .019));
      const coreRadius = (.142 + breathing * .006 + speakingPulse) * scale;
      const glowRadius = coreRadius * (2.45 + motion[3] * .75);
      context.save();
      context.globalCompositeOperation = "screen";
      context.globalAlpha = .04 + motion[3] * .24 + frameEnergy * .12;
      context.drawImage(
        glowSprite,
        centerX - glowRadius,
        centerY - glowRadius,
        glowRadius * 2,
        glowRadius * 2,
      );
      context.restore();

      // coreRadius breathes every frame (idle animation) even outside a
      // transition, so the gradient's absolute position changes constantly —
      // but gradient stops are evaluated against the CTM at fill()-time (see
      // note above scannerGradient), so instead of rebuilding the gradient
      // every frame we build it ONCE in a unit-radius local space and scale
      // the context by coreRadius before filling; the cached gradient
      // reproduces the exact original absolute-space gradient every frame
      // with zero per-frame allocation. Only rebuilt when one of its own
      // color stops changes.
      const stopCenter = colorString(secondaryColor, .7 + motion[3] * .25);
      const stopInner = colorString(coreColor, .22 + motion[3] * .24);
      const stopOuter = colorString(primaryColor, .08 + motion[3] * .1);
      const stopEdge = colorString(primaryColor, .015);
      if (
        coreGradient === null
        || stopCenter !== coreStopCenter
        || stopInner !== coreStopInner
        || stopOuter !== coreStopOuter
        || stopEdge !== coreStopEdge
      ) {
        coreGradient = context.createRadialGradient(-.22, -.25, .04, 0, 0, 1);
        coreGradient.addColorStop(0, stopCenter);
        coreGradient.addColorStop(.28, stopInner);
        coreGradient.addColorStop(.72, stopOuter);
        coreGradient.addColorStop(1, stopEdge);
        coreStopCenter = stopCenter;
        coreStopInner = stopInner;
        coreStopOuter = stopOuter;
        coreStopEdge = stopEdge;
      }
      context.save();
      context.translate(centerX, centerY);
      context.scale(coreRadius, coreRadius);
      context.beginPath();
      context.arc(0, 0, 1, 0, TAU);
      context.fillStyle = coreGradient;
      context.fill();
      context.restore();
      // Same restored cap as drawScanner: the core outline, the inner ring
      // and (through the save() below, which inherits it) the dashed ring
      // all rendered with round caps while drawSegmentRings leaked that
      // setting into the frame context. The dashed ring is where it is
      // visible — every dash gets rounded ends.
      context.lineCap = "round";
      context.beginPath();
      context.arc(centerX, centerY, coreRadius, 0, TAU);
      context.lineWidth = Math.max(.8, scale * .0028);
      context.strokeStyle = colorString(secondaryColor, .24 + motion[3] * .58);
      context.stroke();

      context.beginPath();
      context.arc(centerX, centerY, coreRadius * .74, 0, TAU);
      context.lineWidth = Math.max(.55, scale * .0015);
      context.strokeStyle = colorString(coreColor, .18 + motion[3] * .24);
      context.stroke();

      // Same integration as the other 3 rings (see drawTickRing): this
      // dashed-ring rotation used the identical `now * rate` pattern, just
      // masked by the ring's own dash symmetry rather than being visibly
      // choppy.
      coreDashAngle = wrapAngle(coreDashAngle + (.000025 + motion[0] * .00014) * dt);
      context.save();
      context.translate(centerX, centerY);
      context.rotate(-coreDashAngle);
      context.setLineDash(coreDashPattern);
      context.beginPath();
      context.arc(0, 0, .19 * scale, 0, TAU);
      context.lineWidth = Math.max(.6, scale * .0018);
      context.strokeStyle = colorString(primaryColor, .16 + motion[1] * .24);
      context.stroke();
      context.restore();
    }

    // CONVENTION: nothing under draw()/frame() (or any function they call —
    // drawBackdrop, drawTickRing, drawSegmentRings, drawScanner, drawParticles,
    // drawWaveform, drawCore) may allocate. That means no `new Path2D()`,
    // `new Float32Array()`/other typed-array or array literal, no
    // `createRadialGradient`/`createLinearGradient`/`createPattern` whose
    // result isn't cached across frames, and no object/array literals. Each
    // per-frame allocation becomes GC churn and shows up as a stutter in
    // frameHistogram's >25ms buckets. Reuse a preallocated buffer or a
    // resize-time/color-keyed cache instead (see waveInnerX/etc.,
    // tickRingSprite/segmentRingSprite, scannerGradient/coreGradient). The
    // only allocation-allowed call sites are resize() (runs on resize, not
    // every frame) and the cache-miss branches inside drawScanner/drawCore/
    // renderTickRingSprite/renderSegmentRingSprite, which can only be
    // reached when the color they baked actually changed — i.e. during a
    // transition, not in steady state. The one steady-state allocation left
    // is the colorString() key each cache is compared against: short-lived
    // young-generation strings, deliberately paid to make staleness
    // impossible (see the cache-key note above tickRingMinorColor).
    //
    // Same convention applies to angle state: particleOrbitAngle[i],
    // tickRingAngle, segmentRingAngle, scannerAngle, and coreDashAngle are
    // persistent accumulators (Float32Array or a plain `let`, never a
    // per-frame local), each advanced in place every frame as
    // `angle = wrapAngle(angle + rate(t) * dt)` and wrapped into [0, TAU)
    // by wrapAngle() (defined above, near copyValues/mixColors) instead of
    // being re-derived from absolute `now` each frame. That is the fix for
    // this engine's rotation bug, not just an allocation detail: reading
    // `now * rate` retroactively rescaled the *entire* elapsed-time history
    // any time `rate` changed (drift, motion[], frameEnergy all vary frame
    // to frame), which is what made the rotation look choppy instead of
    // continuous. Integrating means only the current frame's small `dt` is
    // ever affected by a rate change.
    function draw(now) {
      // boss-authorized preview override for headless review; visuals only
      const previewState = ENGINE_STATES.has(window.__atlasEnginePreview)
        ? window.__atlasEnginePreview
        : null;
      setVisualState(previewState || realState);
      // Computed here (moved ahead of its former spot below mixColors) so it
      // can be threaded into prepareFrameSignal and every rotation-bearing
      // draw* call below instead of each one re-deriving its own angle from
      // absolute `now` (see the CONVENTION note above and the accumulators
      // it feeds: particleOrbitAngle, tickRingAngle, segmentRingAngle,
      // scannerAngle, coreDashAngle).
      const dt = lastFrameNow ? Math.min(100, Math.max(0, now - lastFrameNow)) : 16.67;
      lastFrameNow = now;
      prepareFrameSignal(now, previewState, dt);
      mixColors(now);
      context.fillStyle = "#070a10";
      context.fillRect(0, 0, width, height);
      drawBackdrop();
      // CC6: trail layer moved here (was after drawScanner) so the comet
      // trail sits above the backdrop but below every HUD element -- rings,
      // scanner, waveform, core -- instead of painting over them.
      drawParticles(now, dt);
      drawTickRing(now, dt);
      drawSegmentRings(now, dt);
      drawScanner(now, dt);
      drawWaveform(now, dt);
      drawCore(now, dt);
      return dt;
    }

    // Idle (ASLEEP/OFFLINE) pauses the rAF loop entirely; a live preview override
    // (headless review) keeps it running so any state can still be inspected.
    //
    // ...but not while the palette is still moving. A state change starts a
    // 420ms color transition (the same window drawTickRing/drawSegmentRings
    // key off), and going idle used to pause the loop on its first frame:
    // the ASLEEP palette never finished arriving, so what froze on screen was
    // a half-decayed smear of the previous state's colors, held until the
    // next wake. Letting those ~25 frames run costs nothing measurable and
    // renders the transition to completion; the first frame after the window
    // closes pauses exactly as before.
    function isIdleNow() {
      return (realState === "ASLEEP" || realState === "OFFLINE")
        && performance.now() - transitionAt > 420
        && !ENGINE_STATES.has(window.__atlasEnginePreview);
    }

    function pauseLoop() {
      running = false;
      metrics.running = false;
      // Forget the last frame's timestamp. dt is "time since the previous
      // frame", which is meaningless across a pause: waking after 12s idle
      // would otherwise hand the first frame a dt clamped to 100ms and
      // record a >=50ms "drop" that never happened, plus a full-strength
      // waveform decay step. 0 makes the next draw() use the 16.67ms
      // first-frame default, exactly as at startup.
      //
      // Behavior change from the pre-CC4 `now * rate` rotation (an
      // improvement, not a regression): every rotation accumulator now
      // simply stops advancing across a pause instead of being read back
      // out of absolute `now`, so waking no longer makes the rings/particles
      // snap forward to "catch up" for time spent invisible -- they resume
      // exactly where they visually stopped.
      //
      // CC6: the same freeze applies to the comet trail (drawParticles()
      // simply doesn't run while paused), so trailCanvas just holds its last
      // painted frame rather than being explicitly cleared here. Freeze, not
      // clear-on-pause, is the deliberate choice: going idle already changes
      // the card's look -- `.is-idle` REMOVES the holoIdle breathing
      // animation, which styles.css runs on `.engine-card:not(.is-idle)`
      // (it does not add an idle animation; the earlier note here had that
      // backwards) -- so an abrupt "trail vanishes the instant the engine
      // goes idle" would be a second, jarring change on top of the card
      // going still. Freezing lets it read as "paused", and
      // start() below clears it so a *resumed* session never shows a stale,
      // already-faded trail ghost left over from before the pause. (Neither
      // this freeze/clear nor anything else in the canvas engine currently
      // reads prefers-reduced-motion -- confirmed by checking: none of
      // drawTickRing/drawSegmentRings/drawScanner/drawParticles/drawWaveform/
      // drawCore ever did, before or after CC6. Only the DOM-level holoIdle
      // breathing animation in styles.css -- the one `.is-idle` turns OFF --
      // is reduced-motion-gated. The trail
      // inherits the same idle-based pause every other animated element in
      // this engine already has; it does not add or remove reduced-motion
      // support, which would be a broader, separate pass across the whole
      // engine, not something specific to this redesign.)
      lastFrameNow = 0;
      canvas.dataset.animation = "paused";
      if (animationFrame) cancelAnimationFrame(animationFrame);
      animationFrame = 0;
    }

    function frame(now) {
      if (!running) return;
      if (isIdleNow()) { pauseLoop(); return; }
      const started = performance.now();
      const dt = draw(now);
      const elapsed = performance.now() - started;
      metrics.samples += 1;
      metrics.lastMs = elapsed;
      metrics.averageMs += (elapsed - metrics.averageMs) * (metrics.samples < 60 ? 1 / metrics.samples : .035);
      metrics.maxMs = Math.max(metrics.maxMs, elapsed);
      // Frame-interval histogram (preallocated Uint32Array(5), no per-frame
      // allocation): buckets by dt (this frame's interval since the last
      // one), not by draw cost above. Bucket edges: <17 / 17-25 / 25-33 /
      // 33-50 / >=50 ms. A dt >= 25ms is a visible drop (same boundary as
      // the 25-33 bucket edge, so drops stays self-consistent with the
      // histogram — it's exactly the sum of the >=25ms buckets).
      let bucket = 0;
      while (bucket < FRAME_BUCKET_MS.length && dt >= FRAME_BUCKET_MS[bucket]) bucket += 1;
      metrics.frameHistogram[bucket] += 1;
      metrics.totalFrames += 1;
      if (dt >= 25) metrics.drops += 1;
      if (metrics.samples % 30 === 0) {
        canvas.setAttribute("data-frame-cost-ms", metrics.averageMs.toFixed(3));
        canvas.dataset.frameMaxMs = metrics.maxMs.toFixed(3);
      }
      animationFrame = requestAnimationFrame(frame);
    }

    function start() {
      wantsActive = true;
      if (running || isIdleNow() || document.visibilityState !== "visible") return;
      resize();
      running = true;
      metrics.running = true;
      // resize() above may have forced an out-of-band draw() while paused,
      // which sets lastFrameNow; that timestamp is not "the previous frame"
      // of the loop starting now either (see pauseLoop).
      lastFrameNow = 0;
      metrics.frameHistogram.fill(0);
      metrics.drops = 0;
      metrics.totalFrames = 0;
      metrics.maxAngleStepDeg = 0;
      // CC6: clear-on-start, pairing with the freeze-on-pause in pauseLoop()
      // above -- a fresh/resumed session should never show an already-faded
      // trail ghost left over from before the pause.
      trailContext.clearRect(0, 0, trailCanvas.width, trailCanvas.height);
      canvas.dataset.animation = "running";
      animationFrame = requestAnimationFrame(frame);
    }

    function stop() {
      wantsActive = false;
      pauseLoop();
    }

    if (typeof ResizeObserver === "function") {
      const observer = new ResizeObserver(resize);
      observer.observe(canvas);
    } else {
      window.addEventListener("resize", resize);
    }
    resize();
    draw(performance.now());
    canvas.dataset.frameCostMs = "0.000";
    canvas.dataset.frameMaxMs = "0.000";
    canvas.dataset.animation = "paused";
    window.__atlasEngineMetrics = metrics;
    return {setSignal, setState, start, stop};
  }

  const engine = createEngine(refs.canvas);

  function showPendingConfirmation(readback) {
    if (pendingDismissTimer) window.clearTimeout(pendingDismissTimer);
    pendingDismissTimer = 0;
    if (pendingDismissEnd) refs.pendingCard.removeEventListener("animationend", pendingDismissEnd);
    pendingDismissEnd = null;
    refs.pendingText.textContent = readback;
    refs.pendingCard.hidden = false;
    refs.pendingCard.classList.remove("is-holo-dismissing");
  }

  function hidePendingConfirmation() {
    if (refs.pendingCard.hidden || pendingDismissTimer) return;
    const finish = () => {
      if (pendingDismissTimer) window.clearTimeout(pendingDismissTimer);
      pendingDismissTimer = 0;
      if (pendingDismissEnd) refs.pendingCard.removeEventListener("animationend", pendingDismissEnd);
      pendingDismissEnd = null;
      refs.pendingCard.hidden = true;
      refs.pendingCard.classList.remove("is-holo-dismissing");
    };
    if (window.matchMedia?.("(prefers-reduced-motion: reduce)").matches === true) {
      finish();
      return;
    }
    refs.pendingCard.classList.add("is-holo-dismissing");
    pendingDismissEnd = (event) => {
      if (event.target === refs.pendingCard) finish();
    };
    refs.pendingCard.addEventListener("animationend", pendingDismissEnd);
    pendingDismissTimer = window.setTimeout(finish, 400);
  }

  function renderPendingConfirmation(pending) {
    if (isRecord(pending) && typeof pending.readback === "string") {
      showPendingConfirmation(pending.readback);
    } else {
      hidePendingConfirmation();
    }
  }

  refs.textForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    const text = refs.textInput.value.trim();
    if (!text) return;
    if (!actionToken) {
      showPendingConfirmation("Pair Atlas before sending a message.");
      return;
    }
    refs.textInput.disabled = true;
    try {
      const payload = await authenticatedJson("/turn", {
        method: "POST",
        headers: {"content-type": "application/json"},
        body: JSON.stringify({text}),
      });
      if (payload?.ok === true) {
        refs.textInput.value = "";
        renderPendingConfirmation(payload.pending);
      }
    } catch (_error) {
      showPendingConfirmation("Message could not be sent.");
    } finally {
      refs.textInput.disabled = false;
      refs.textInput.focus();
    }
  });
  refs.textInput.addEventListener("keydown", (event) => {
    if (event.key !== "Escape") return;
    event.preventDefault();
    refs.textInput.value = "";
  });

  function activateTab(button, active, handler = null, syncHash = false) {
    button.classList.toggle("is-active", active);
    if (button.getAttribute("role") === "tab") {
      button.setAttribute("aria-selected", String(active));
      if (button.dataset.viewTarget) button.tabIndex = active ? 0 : -1;
    }
    if (!handler) return;
    button.addEventListener("click", () => {
      const target = handler();
      if (!syncHash) return;
      window.location.hash === `#${target}` ? selectView(target) : window.location.hash = target;
    });
  }
  function selectView(name) {
    const resolved = ROUTES.has(name) ? name : "live";
    const changed = resolved !== currentView;
    currentView = resolved;
    document.querySelectorAll("[data-view]").forEach((view) => { view.hidden = view.dataset.view !== resolved; });
    document.querySelectorAll("[data-view-target]").forEach((button) => {
      const active = button.dataset.viewTarget === resolved;
      if (button.getAttribute("role") === "tab") activateTab(button, active);
      else button.classList.toggle("is-active", active);
    });
    updateLiveActivity();
    if (changed) updateSettingsPolling(true);
  }

  function routeFromHash() {
    const name = window.location.hash.slice(1).toLowerCase();
    const resolved = ROUTES.has(name) ? name : "live";
    if (name !== resolved) history.replaceState(null, "", `${window.location.pathname}${window.location.search}#${resolved}`);
    selectView(resolved);
  }
  document.querySelectorAll("[data-view-target]").forEach((button) => {
    activateTab(button, false, () => button.dataset.viewTarget, true);
  });
  const viewTabs = [...document.querySelectorAll('.nav-button[role="tab"]')];
  viewTabs.forEach((button, index) => {
    button.addEventListener("keydown", (event) => {
      let nextIndex = null;
      if (event.key === "ArrowLeft") nextIndex = (index - 1 + viewTabs.length) % viewTabs.length;
      if (event.key === "ArrowRight") nextIndex = (index + 1) % viewTabs.length;
      if (event.key === "Home") nextIndex = 0;
      if (event.key === "End") nextIndex = viewTabs.length - 1;
      if (nextIndex === null) return;
      event.preventDefault();
      const nextTab = viewTabs[nextIndex];
      nextTab.focus(); window.location.hash = nextTab.dataset.viewTarget;
    });
  });
  document.querySelector(".skip-link").addEventListener("click", (event) => {
    event.preventDefault();
    document.querySelector("#main").focus();
  });

  function setConnection(online) {
    refs.connection.classList.toggle("is-online", online);
    refs.connection.classList.toggle("is-offline", !online);
    refs.connection.querySelector(".connection-label").textContent = online ? "connected" : "offline";
  }

  function setEngineState(rawState, tool = null) {
    const state = rawState === "THINKING" && isRecord(tool) && typeof tool.name === "string" && tool.name.trim()
      ? "TOOL"
      : VOICE_STATES.has(rawState) ? rawState : "OFFLINE";
    if (refs.stateLabel.textContent !== state) refs.stateLabel.textContent = state;
    refs.engineCard.classList.toggle("is-idle", state === "ASLEEP" || state === "OFFLINE");
    engine.setState(state);
  }

  function renderTool(rawState, tool) {
    const name = rawState === "THINKING" && isRecord(tool) && typeof tool.name === "string"
      ? tool.name.trim()
      : "";
    const since = name && typeof tool.since === "string" ? tool.since.trim() : "";
    const identity = name && since ? JSON.stringify([name, since]) : "";
    if (identity === activeToolIdentity) return;
    activeToolIdentity = identity;
    if (!identity) {
      refs.toolStrip.classList.remove("is-active");
      refs.toolStrip.setAttribute("aria-hidden", "true");
      refs.toolStrip.textContent = "";
      return;
    }
    refs.toolStrip.classList.add("is-active");
    refs.toolStrip.removeAttribute("aria-hidden");
    refs.toolStrip.textContent = `TOOL - ${name}`;
    refs.toolAnnouncer.replaceChildren(document.createTextNode(`Tool started: ${name}.`));
  }

  function renderGreeting(user = null, now = new Date()) {
    if (isRecord(user)) userName = typeof user.name === "string" ? user.name.trim() : "";
    const hour = now.getHours();
    const period = hour < 12 ? "MORNING" : hour < 18 ? "AFTERNOON" : "EVENING";
    const greeting = `GOOD ${period}${userName ? `, ${userName.toLocaleUpperCase()}` : ""}`;
    if (refs.greeting.textContent !== greeting) refs.greeting.textContent = greeting;
  }

  function renderTranscript(lines) {
    const safeLines = Array.isArray(lines) ? lines : [];
    const signature = JSON.stringify(safeLines);
    if (signature === transcriptSignature) return;
    transcriptSignature = signature;
    maybeRequestVoiceKbUnlock(safeLines);
    refs.transcript.replaceChildren();
    if (safeLines.length === 0) {
      refs.transcript.append(node("p", "empty", "No transcript yet."));
      return;
    }
    safeLines.forEach((line) => {
      if (!isRecord(line) || typeof line.text !== "string" || typeof line.role !== "string") return;
      const role = TRANSCRIPT_ROLES.has(line.role) ? line.role : "system";
      const row = node("div", `transcript-line is-${role}`);
      row.append(node("span", "transcript-role", role));
      row.append(node("span", "transcript-text", line.text));
      if (typeof line.t === "string") {
        const stamp = new Date(line.t);
        const time = node("time", "transcript-time", Number.isNaN(stamp.valueOf()) ? "" : stamp.toLocaleTimeString());
        time.dateTime = line.t;
        row.append(time);
      }
      refs.transcript.append(row);
    });
    refs.transcript.scrollTop = refs.transcript.scrollHeight;
  }

  function audioDevice(audio, direction) {
    if (!isRecord(audio) || !isRecord(audio[direction])) return {name: "—", following: false, present: false};
    const device = audio[direction];
    return {
      name: stringValue(device.name),
      following: device.following === true,
      present: true,
    };
  }

  function renderAudio(audio) {
    const input = audioDevice(audio, "input");
    const output = audioDevice(audio, "output");
    const line = `mic: ${input.name} · speaker: ${output.name}`;
    const inputMode = input.present && input.following ? "following system default" : "";
    const outputMode = output.present && output.following ? "following system default" : "";
    if (refs.audioLine.textContent !== line) refs.audioLine.textContent = line;
    if (refs.audioInput.textContent !== input.name) refs.audioInput.textContent = input.name;
    if (refs.audioOutput.textContent !== output.name) refs.audioOutput.textContent = output.name;
    if (refs.audioInputMode.textContent !== inputMode) refs.audioInputMode.textContent = inputMode;
    if (refs.audioOutputMode.textContent !== outputMode) refs.audioOutputMode.textContent = outputMode;
  }

  function renderVoice(payload) {
    const voice = displayString(payload.voice);
    const wake = displayString(payload.wake_model);
    if (refs.voiceStatus.textContent !== voice) refs.voiceStatus.textContent = voice;
    if (refs.wakeStatus.textContent !== wake) refs.wakeStatus.textContent = wake;
  }

  function refreshState() {
    return runOnce("state", async () => {
      try {
        const payload = await publicJson("/state", {cache: "no-store"});
        setConnection(true);
        setEngineState(payload.state, payload.tool);
        renderTool(payload.state, payload.tool);
        renderGreeting(payload.user);
        renderTranscript(payload.transcript);
        renderVoice(payload);
        renderAudio(payload.audio);
      } catch (_error) {
        setConnection(false);
        setEngineState("OFFLINE");
        renderTool("OFFLINE", null);
        renderAudio(null);
      }
    });
  }

  function refreshSignal() {
    if (currentView !== "live" || document.visibilityState !== "visible") return;
    return runOnce("signal", async () => {
      try {
        const payload = await publicJson("/signal", {cache: "no-store", ignoreHttpError: true});
        if (payload) engine.setSignal(payload.energy, payload.bands);
      } catch (_error) {
        engine.setSignal(0);
      }
    });
  }

  function updateLiveActivity() {
    const active = currentView === "live" && document.visibilityState === "visible";
    if (active) {
      engine.start();
      if (!greetingTimer) {
        renderGreeting();
        greetingTimer = window.setInterval(renderGreeting, 60_000);
      }
      if (!signalTimer) {
        refreshSignal();
        signalTimer = window.setInterval(refreshSignal, SIGNAL_INTERVAL_MS);
      }
      return;
    }
    engine.stop();
    if (greetingTimer) window.clearInterval(greetingTimer);
    greetingTimer = 0;
    if (signalTimer) window.clearInterval(signalTimer);
    signalTimer = 0;
  }

  function eventStore(jobId) {
    if (!eventsByJob.has(jobId)) eventsByJob.set(jobId, []);
    return eventsByJob.get(jobId);
  }
  function removeStoredPairing() {
    try { sessionStorage.removeItem(PAIRING_STORAGE_KEY); } catch (_error) { return; }
  }
  function pairedUntilText() {
    const expiry = new Date(actionExpiresAt * 1000);
    const time = expiry.toLocaleTimeString([], {hour: "2-digit", minute: "2-digit"});
    return `paired until ${time}`;
  }
  function renderPairingStatus() {
    const paired = Boolean(actionToken) && actionExpiresAt * 1000 > Date.now();
    refs.pairingStatus.textContent = paired ? pairedUntilText() : "Not paired";
    refs.repairButton.disabled = !paired;
  }

  function clearPairing() {
    if (pairingExpiryTimer) window.clearTimeout(pairingExpiryTimer);
    pairingExpiryTimer = 0; actionToken = ""; actionExpiresAt = 0;
    removeStoredPairing();
    eventsByJob.clear(); resultsByJob.clear();
    renderPairingStatus();
    const selected = jobs.find((job) => job.id === selectedResultId);
    setResultPanel(
      selected ? selected.title : "No job selected",
      selected ? "pair to view" : "Select a completed job.",
    );
    renderWorkers();
    renderHistory();
  }

  function setPairing(token, expiresAt) {
    const expiration = Number(expiresAt);
    if (typeof token !== "string" || !token || !Number.isFinite(expiration) || expiration * 1000 <= Date.now()) {
      clearPairing();
      return false;
    }
    if (pairingExpiryTimer) window.clearTimeout(pairingExpiryTimer);
    actionToken = token; actionExpiresAt = expiration;
    try {
      sessionStorage.setItem(PAIRING_STORAGE_KEY, JSON.stringify({token, expires_at: expiration}));
    } catch (_error) {
      clearPairing();
      return false;
    }
    const delay = Math.min(expiration * 1000 - Date.now(), 2_147_000_000);
    pairingExpiryTimer = window.setTimeout(clearPairing, delay);
    renderPairingStatus();
    return true;
  }

  function restorePairing() {
    let stored = null;
    try { stored = JSON.parse(sessionStorage.getItem(PAIRING_STORAGE_KEY) || "null"); }
    catch (_error) { removeStoredPairing(); }
    if (!isRecord(stored) || !setPairing(stored.token, stored.expires_at)) clearPairing();
  }

  async function refreshEvents(job) {
    if (!actionToken) return;
    const existing = eventStore(job.id);
    const lastSequence = existing.length ? existing[existing.length - 1].sequence : 0;
    try {
      const payload = await authenticatedJson(
        `/jobs/${encodeURIComponent(job.id)}/events?after=${lastSequence}`,
        {cache: "no-store", ignoreHttpError: true},
      );
      if (!payload) return;
      if (Array.isArray(payload.events)) existing.push(...payload.events);
    } catch (_error) {
      return;
    }
  }

  function jobView(job, titleTag = "", titleClass = "", statusClass = "") {
    const active = ACTIVE_JOB_STATES.has(job.status);
    return {
      job, active, title: titleTag ? node(titleTag, titleClass, job.title) : null,
      status: titleTag ? node("span", statusClass, job.status) : null,
      summary: job.summary || job.error || (active ? "In progress" : "No summary available."),
    };
  }
  function cancelButton(view, className = "", showUnpaired = false) {
    if (!actionToken && !showUnpaired) return null;
    const classes = ["small-button", className].filter(Boolean).join(" ");
    const button = node("button", classes, actionToken ? "Cancel" : "Pair to cancel");
    button.type = "button"; button.disabled = !actionToken;
    button.addEventListener("click", () => cancelJob(view.job.id, button));
    return button;
  }
  function renderWorker(view) {
    refs.workerOutput.replaceChildren();
    if (!view) {
      refs.workerOutput.append(node("p", "empty", "No workers are active."));
      return;
    }
    const header = node("div", "worker-heading"), title = node("div", "worker-title");
    title.append(view.title, view.status);
    header.append(title);
    const cancel = cancelButton(view);
    if (cancel) header.append(cancel);
    refs.workerOutput.append(header);
    const terminal = node("div", "terminal");
    const events = eventStore(view.job.id);
    if (!actionToken) terminal.append(node("p", "quiet", "pair to view output"));
    else if (events.length === 0) terminal.append(node("p", "quiet", "Waiting for output."));
    if (actionToken) {
      events.forEach((event) => {
        const row = node("div", `terminal-line is-${event.kind}`);
        row.append(node("span", "terminal-sequence", String(event.sequence).padStart(4, "0")));
        row.append(node("span", "terminal-kind", event.kind));
        row.append(node("span", "terminal-text", event.text));
        terminal.append(row);
      });
    }
    refs.workerOutput.append(terminal);
  }
  function renderWorkers() {
    const active = jobs.map((job) => jobView(job, "strong", "", "quiet")).filter((view) => view.active);
    refs.workerSummary.textContent = active.length ? `${active.length} active` : "idle";
    refs.workerTabs.replaceChildren();
    if (!active.some((view) => view.job.id === selectedJobId)) selectedJobId = active[0]?.job.id || "";
    active.forEach((view) => {
      const button = node("button", "worker-tab", view.job.title);
      const selected = view.job.id === selectedJobId;
      button.type = "button";
      button.role = "tab";
      activateTab(button, selected, () => {
        selectedJobId = view.job.id;
        renderWorkers();
      });
      refs.workerTabs.append(button);
    });
    renderWorker(active.find((view) => view.job.id === selectedJobId));
  }
  function relativeTime(value) {
    const milliseconds = typeof value === "number" ? value * 1000 : Date.parse(value);
    if (!Number.isFinite(milliseconds)) return "—";
    const elapsed = Math.max(0, Date.now() - milliseconds);
    if (elapsed < 10_000) return "just now";
    if (elapsed < 60_000) return `${Math.floor(elapsed / 1000)}s ago`;
    if (elapsed < 3_600_000) return `${Math.floor(elapsed / 60_000)}m ago`;
    if (elapsed < 86_400_000) return `${Math.floor(elapsed / 3_600_000)}h ago`;
    if (elapsed < 604_800_000) return `${Math.floor(elapsed / 86_400_000)}d ago`;
    return new Date(milliseconds).toLocaleDateString();
  }
  function setResultPanel(title, message, result = null) {
    const hasResult = typeof result === "string";
    refs.resultTitle.textContent = title;
    refs.resultPlaceholder.textContent = message;
    refs.resultPlaceholder.hidden = hasResult;
    refs.historyResult.hidden = !hasResult;
    refs.historyResult.textContent = hasResult ? result : "";
  }
  async function showResult(job) {
    selectedResultId = job.id;
    if (!actionToken) {
      setResultPanel(job.title, "pair to view");
      return;
    }
    if (resultsByJob.has(job.id)) {
      setResultPanel(job.title, "", resultsByJob.get(job.id));
      return;
    }
    setResultPanel(job.title, "Loading result…");
    try {
      const payload = await authenticatedJson(`/jobs/${encodeURIComponent(job.id)}/result`, {cache: "no-store"});
      if (!payload) return;
      const result = typeof payload.result === "string" ? payload.result : "Result unavailable.";
      resultsByJob.set(job.id, result);
      if (selectedResultId === job.id) setResultPanel(job.title, "", result);
    } catch (_error) {
      if (selectedResultId === job.id) setResultPanel(job.title, "Result unavailable.");
    }
  }
  function renderHistory() {
    refs.history.replaceChildren();
    refs.historyCount.textContent = `${jobs.length} ${jobs.length === 1 ? "job" : "jobs"}`;
    if (jobs.length === 0) {
      refs.history.append(node("p", "empty", "No jobs yet."));
      return;
    }
    const ordered = [...jobs].sort((left, right) => right.updated_at - left.updated_at);
    ordered.forEach((job) => {
      const view = jobView(job, "span", "history-title", `job-state is-${job.status}`);
      const row = node("article", "history-row");
      const open = node("button", "history-open");
      open.type = "button";
      open.disabled = view.active;
      const topLine = node("div", "history-topline");
      topLine.append(view.title);
      const meta = node("span", "history-meta");
      meta.append(view.status);
      meta.append(node("time", "history-time", relativeTime(job.updated_at)));
      topLine.append(meta);
      open.append(topLine);
      open.append(node("p", "history-summary", view.summary));
      if (!view.active) open.addEventListener("click", () => showResult(job));
      row.append(open);
      if (view.active) row.append(cancelButton(view, "history-cancel", true));
      refs.history.append(row);
    });
  }
  async function cancelJob(jobId, button) {
    if (!actionToken) return;
    button.disabled = true;
    button.textContent = "Cancelling…";
    try {
      await authenticatedJson(`/jobs/${encodeURIComponent(jobId)}/cancel`, {
        method: "POST",
        headers: {
          "content-type": "application/json",
        },
        body: "{}",
        parse: false,
        ignoreHttpError: true,
      });
      await refreshJobs();
    } catch (_error) {
      button.disabled = false;
      button.textContent = "Cancel";
    }
  }
  function refreshJobs() {
    return runOnce("jobs", async () => {
      try {
        const payload = await publicJson("/jobs", {cache: "no-store", ignoreHttpError: true});
        if (!payload) return;
        jobs = Array.isArray(payload.jobs) ? payload.jobs : [];
        const active = jobs.map((job) => jobView(job)).filter((view) => view.active);
        if (!document.hidden) await Promise.all(active.map((view) => refreshEvents(view.job)));
        // The gate exists to skip a full rebuild every 2s while nothing
        // changes. Jobs alone were not enough: rendered rows also depend on
        // the clock ("just now" pinned forever once jobs stopped changing) and
        // on pairing (stale "Pair to cancel" buttons survived pairing). The
        // 30s bucket keeps the idle-rebuild rate at ~1 per 30s, not 1 per 2s.
        const signature = JSON.stringify({
          jobs,
          events: active.map((view) => [view.job.id, eventStore(view.job.id).length]),
          paired: Boolean(actionToken),
          clockBucket: Math.floor(Date.now() / 30000),
        });
        if (signature === jobsSignature) return;
        jobsSignature = signature;
        renderWorkers();
        renderHistory();
      } catch (_error) {
        return;
      }
    });
  }
  function renderMcp(servers) {
    refs.mcpList.replaceChildren();
    if (!Array.isArray(servers) || servers.length === 0) {
      refs.mcpList.append(node("p", "empty", "No MCP servers configured."));
      return;
    }
    servers.forEach((server) => {
      if (!isRecord(server)) return;
      const row = node("div", "mcp-row");
      const state = ["connecting", "connected", "not_configured", "error"].includes(server.state)
        ? server.state : (server.connected ? "connected" : "error");
      const name = node("strong", "status-name");
      name.append(node("span", `status-dot is-${state}`));
      name.append(document.createTextNode(stringValue(server.name)));
      row.append(name);
      const tools = Number.isInteger(server.tools) ? server.tools : 0;
      let detail = stringValue(server.detail || state);
      if (server.connected) detail = `${detail}, ${tools} ${tools === 1 ? "tool" : "tools"}`;
      if (server.name === "kb" && ["held", "none", "expired"].includes(server.session)) {
        detail = `${detail}, session ${server.session}`;
      }
      row.append(node("span", `status-detail is-${state}`, detail));
      refs.mcpList.append(row);
    });
  }
  function renderApps(apps) {
    refs.appsList.replaceChildren();
    if (!Array.isArray(apps) || apps.length === 0) {
      refs.appsList.append(node("p", "empty", "No desktop profiles configured."));
      return;
    }
    apps.forEach((app) => {
      if (!isRecord(app)) return;
      const state = ["configured", "not_configured", "error"].includes(app.state)
        ? app.state : "error";
      const row = node("div", "mcp-row");
      const name = node("strong", "status-name");
      name.append(node("span", `status-dot is-${state}`));
      name.append(document.createTextNode(stringValue(app.name)));
      row.append(name);
      row.append(node("span", `status-detail is-${state}`, stringValue(app.detail || state)));
      refs.appsList.append(row);
    });
  }
  function refreshSettings() {
    return runOnce("settings", async () => {
      try {
        const health = await publicJson("/health", {cache: "no-store", ignoreHttpError: true});
        if (health) {
          renderMcp(health.mcp);
          renderApps(health.apps);
          refs.claudeStatus.textContent = health.claude ? "Available" : "Unavailable";
          refs.claudeStatus.classList.toggle("is-unavailable", !health.claude);
        }
      } catch (_error) {
        refs.claudeStatus.textContent = "Unavailable";
        refs.claudeStatus.classList.add("is-unavailable");
      }
    });
  }
  function updateSettingsPolling(refresh = false) {
    if (settingsTimer) window.clearInterval(settingsTimer); settingsTimer = 0;
    if (document.hidden || currentView !== "settings") return;
    if (refresh) refreshSettings();
    settingsTimer = window.setInterval(refreshSettings, SETTINGS_INTERVAL_MS);
  }
  function updatePolling(resume = false) {
    if (stateTimer) window.clearInterval(stateTimer); if (jobsTimer) window.clearInterval(jobsTimer);
    const hidden = document.hidden;
    stateTimer = window.setInterval(refreshState, hidden ? HIDDEN_INTERVAL_MS : STATE_INTERVAL_MS);
    jobsTimer = window.setInterval(refreshJobs, hidden ? HIDDEN_INTERVAL_MS : JOBS_INTERVAL_MS);
    if (resume && !hidden) { refreshState(); refreshJobs(); }
    updateSettingsPolling(resume);
    updateLiveActivity();
  }
  async function pairWithToken(token) {
    const payload = await publicJson("/pair", {
      method: "POST",
      headers: {"content-type": "application/json"},
      body: JSON.stringify({token}),
      clearUnauthorized: true,
    });
    if (!setPairing(payload.action_token, payload.expires_at)) throw new Error("invalid pairing");
    await refreshJobs();
  }
  async function pairFromFragment() {
    const params = new URLSearchParams(window.location.hash.slice(1));
    const token = params.get("pair");
    if (!token) return false;
    history.replaceState(null, "", `${window.location.pathname}${window.location.search}#live`);
    selectView("live");
    refs.pairingStatus.textContent = "Pairing";
    try {
      await pairWithToken(token);
    } catch (_error) {
      clearPairing();
      refs.pairingStatus.textContent = "Pairing failed";
    }
    return true;
  }
  async function renewPairing() {
    if (!actionToken || actionExpiresAt * 1000 <= Date.now()) {
      clearPairing();
      return;
    }
    refs.repairButton.disabled = true;
    refs.pairingStatus.textContent = "Re-pairing";
    try {
      const payload = await authenticatedJson("/pair/bootstrap", {cache: "no-store"});
      if (!payload) return;
      if (typeof payload.token !== "string" || !payload.token) throw new Error("invalid bootstrap");
      await pairWithToken(payload.token);
    } catch (_error) {
      if (actionExpiresAt * 1000 <= Date.now()) clearPairing();
      else renderPairingStatus();
    }
  }
  async function handleHashChange() {
    if (await pairFromFragment()) return;
    routeFromHash();
  }

  window.addEventListener("hashchange", handleHashChange);
  document.addEventListener("visibilitychange", () => updatePolling(!document.hidden));
  refs.repairButton.addEventListener("click", renewPairing);
  restorePairing();
  handleHashChange();
  refreshState();
  refreshJobs();
  updatePolling();

  // Host-to-page only: the native title bar owns the window-control buttons in both window states
  // (WM_NCHITTEST returns HTMINBUTTON/HTMAXBUTTON/HTCLOSE there), so CSS :hover never fires and
  // worker/desktop.py mirrors its nonclient hover here. The click handlers above stay as the
  // fallback for when that hook is not installed. The argument is one of "minimize", "maximize",
  // "close" or "" and only toggles a class.
  const NC_HOVER_BUTTONS = {
    minimize: refs.windowMinimize, maximize: refs.windowMaximize, close: refs.windowClose,
  };
  root.__atlasNcHover = (state) => {
    Object.entries(NC_HOVER_BUTTONS).forEach(([name, button]) => {
      button.classList.toggle("is-nc-hover", name === state);
    });
  };
})(globalThis);
