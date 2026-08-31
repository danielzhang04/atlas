(root => {
  "use strict";

  if (typeof document === "undefined") return;

  const ROUTES = new Set([...document.querySelectorAll("[data-view]")].map((view) => view.dataset.view));
  const VOICE_STATES = new Set(["ASLEEP", "LISTENING", "THINKING", "SPEAKING"]);
  const ENGINE_STATES = new Set([...VOICE_STATES, "TOOL"]);
  const TRANSCRIPT_ROLES = new Set(["user", "atlas", "tool", "ambient", "system"]);
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
    const PARTICLE_COUNT = 42;
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
    const metrics = {samples: 0, lastMs: 0, averageMs: 0, maxMs: 0, running: false};
    let realState = "OFFLINE";
    let visualState = "OFFLINE";
    let energy = 0;
    let frameEnergy = 0;
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

    particleSprite.width = 32;
    particleSprite.height = 32;
    const particleContext = particleSprite.getContext("2d");
    const particleGlow = particleContext.createRadialGradient(16, 16, 0, 16, 16, 16);
    particleGlow.addColorStop(0, "rgb(255 255 255 / 0.95)");
    particleGlow.addColorStop(.16, "rgb(255 255 255 / 0.5)");
    particleGlow.addColorStop(.52, "rgb(255 255 255 / 0.1)");
    particleGlow.addColorStop(1, "rgb(255 255 255 / 0)");
    particleContext.fillStyle = particleGlow;
    particleContext.fillRect(0, 0, 32, 32);

    function copyValues(target, source) {
      for (let index = 0; index < target.length; index += 1) target[index] = source[index];
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

    function prepareFrameSignal(now, previewState) {
      frameEnergy = energy;
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

    function drawTickRing(now) {
      const visibility = visualState === "OFFLINE" ? .18 : 1;
      context.save();
      context.translate(centerX, centerY);
      context.rotate(now * .000018 * (1 + motion[0]));
      context.lineCap = "butt";
      context.lineWidth = Math.max(.45, scale * .0012);
      context.strokeStyle = colorString(primaryColor, (.15 + motion[1] * .1) * visibility);
      context.stroke(minorTickPath);
      context.lineWidth = Math.max(.7, scale * .0021);
      context.strokeStyle = colorString(secondaryColor, (.35 + motion[1] * .2) * visibility);
      context.stroke(majorTickPath);
      context.restore();
    }

    function drawSegmentRings(now) {
      const speed = .000035 + motion[0] * .00016;
      const visibility = visualState === "OFFLINE" ? .16 : 1;
      context.lineCap = "round";
      context.save();
      context.translate(centerX, centerY);
      context.rotate(-now * speed * 1.32);
      context.lineWidth = Math.max(.65, scale * .0022);
      context.strokeStyle = colorString(secondaryColor, (.22 + motion[1] * .42) * visibility);
      context.stroke(innerSegmentPath);
      context.restore();
    }

    function drawScanner(now) {
      if (motion[4] < .04) return;
      const rotation = now * (.00018 + motion[4] * .00034);
      const radius = .44 * scale;
      context.save();
      context.translate(centerX, centerY);
      context.rotate(rotation);
      const sweep = context.createLinearGradient(0, 0, radius, 0);
      sweep.addColorStop(0, colorString(primaryColor, 0));
      sweep.addColorStop(.72, colorString(primaryColor, .02 + motion[4] * .09));
      sweep.addColorStop(1, colorString(secondaryColor, .08 + motion[4] * .38));
      context.fillStyle = sweep;
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

    function drawParticles(now) {
      if (layerPresence <= 0) return;
      const seconds = now / 1000;
      const drift = .025 + motion[2] * .14 + frameEnergy * .2;
      const particleEnergy = .25 + motion[2] * .45 + frameEnergy * .45;
      context.save();
      context.globalCompositeOperation = "screen";
      for (let index = 0; index < PARTICLE_COUNT; index += 1) {
        const orbit = particleAngle[index] + seconds * particleSpeed[index] * drift;
        const tremor = Math.sin(seconds * (1 + particleSpeed[index]) + particlePhase[index]) * (.003 + frameEnergy * .006);
        const radius = (particleRadius[index] + tremor) * scale;
        const size = (2.2 + index % 3 * 1.35) * (1 + frameEnergy * .4);
        context.globalAlpha = layerPresence * particleEnergy * (.18 + .22 * Math.sin(particlePhase[index] + seconds * .7) ** 2);
        context.drawImage(
          particleSprite,
          centerX + Math.cos(orbit) * radius - size,
          centerY + Math.sin(orbit) * radius - size,
          size * 2,
          size * 2,
        );
      }
      context.restore();
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
      const barPath = new Path2D();
      const tipPath = new Path2D();
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
        const innerX = centerX + cosine[index] * innerRadius;
        const innerY = centerY + sine[index] * innerRadius;
        const outerX = centerX + cosine[index] * outerRadius;
        const outerY = centerY + sine[index] * outerRadius;
        barPath.moveTo(innerX, innerY);
        barPath.lineTo(outerX, outerY);
        tipPath.moveTo(outerX + Math.max(1, scale * .0026), outerY);
        tipPath.arc(outerX, outerY, Math.max(.8, scale * .0026), 0, TAU);
      }
      if (layerPresence <= 0) return;
      context.save();
      context.globalAlpha = layerPresence;
      context.lineCap = "round";
      context.lineWidth = Math.max(3, scale * .009);
      context.strokeStyle = colorString(primaryColor, .05 + motion[1] * .11);
      context.stroke(barPath);
      context.lineWidth = Math.max(1.15, scale * .0035);
      context.strokeStyle = colorString(secondaryColor, .34 + motion[3] * .56);
      context.stroke(barPath);
      context.fillStyle = colorString(secondaryColor, .28 + motion[3] * .62);
      context.fill(tipPath);
      context.restore();
    }

    function drawCore(now) {
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

      const coreGradient = context.createRadialGradient(
        centerX - coreRadius * .22, centerY - coreRadius * .25, coreRadius * .04,
        centerX, centerY, coreRadius,
      );
      coreGradient.addColorStop(0, colorString(secondaryColor, .7 + motion[3] * .25));
      coreGradient.addColorStop(.28, colorString(coreColor, .22 + motion[3] * .24));
      coreGradient.addColorStop(.72, colorString(primaryColor, .08 + motion[3] * .1));
      coreGradient.addColorStop(1, colorString(primaryColor, .015));
      context.beginPath();
      context.arc(centerX, centerY, coreRadius, 0, TAU);
      context.fillStyle = coreGradient;
      context.fill();
      context.lineWidth = Math.max(.8, scale * .0028);
      context.strokeStyle = colorString(secondaryColor, .24 + motion[3] * .58);
      context.stroke();

      context.beginPath();
      context.arc(centerX, centerY, coreRadius * .74, 0, TAU);
      context.lineWidth = Math.max(.55, scale * .0015);
      context.strokeStyle = colorString(coreColor, .18 + motion[3] * .24);
      context.stroke();

      context.save();
      context.translate(centerX, centerY);
      context.rotate(-now * (.000025 + motion[0] * .00014));
      context.setLineDash(coreDashPattern);
      context.beginPath();
      context.arc(0, 0, .19 * scale, 0, TAU);
      context.lineWidth = Math.max(.6, scale * .0018);
      context.strokeStyle = colorString(primaryColor, .16 + motion[1] * .24);
      context.stroke();
      context.restore();
    }

    function draw(now) {
      // boss-authorized preview override for headless review; visuals only
      const previewState = ENGINE_STATES.has(window.__atlasEnginePreview)
        ? window.__atlasEnginePreview
        : null;
      setVisualState(previewState || realState);
      prepareFrameSignal(now, previewState);
      mixColors(now);
      const dt = lastFrameNow ? Math.min(100, Math.max(0, now - lastFrameNow)) : 16.67;
      lastFrameNow = now;
      context.fillStyle = "#070a10";
      context.fillRect(0, 0, width, height);
      drawBackdrop();
      drawTickRing(now);
      drawSegmentRings(now);
      drawScanner(now);
      drawParticles(now);
      drawWaveform(now, dt);
      drawCore(now);
    }

    // Idle (ASLEEP/OFFLINE) pauses the rAF loop entirely; a live preview override
    // (headless review) keeps it running so any state can still be inspected.
    function isIdleNow() {
      return (realState === "ASLEEP" || realState === "OFFLINE")
        && !ENGINE_STATES.has(window.__atlasEnginePreview);
    }

    function pauseLoop() {
      running = false;
      metrics.running = false;
      canvas.dataset.animation = "paused";
      if (animationFrame) cancelAnimationFrame(animationFrame);
      animationFrame = 0;
    }

    function frame(now) {
      if (!running) return;
      if (isIdleNow()) { pauseLoop(); return; }
      const started = performance.now();
      draw(now);
      const elapsed = performance.now() - started;
      metrics.samples += 1;
      metrics.lastMs = elapsed;
      metrics.averageMs += (elapsed - metrics.averageMs) * (metrics.samples < 60 ? 1 / metrics.samples : .035);
      metrics.maxMs = Math.max(metrics.maxMs, elapsed);
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
        const signature = JSON.stringify({
          jobs,
          events: active.map((view) => [view.job.id, eventStore(view.job.id).length]),
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
})(globalThis);
