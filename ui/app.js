(() => {
  "use strict";

  const ROUTES = new Set(["live", "history", "settings"]);
  const VOICE_STATES = new Set(["ASLEEP", "LISTENING", "THINKING", "SPEAKING"]);
  const TRANSCRIPT_ROLES = new Set(["user", "atlas", "tool", "ambient", "system"]);
  const ACTIVE_JOB_STATES = new Set(["queued", "launching", "running"]);
  const CONFIG_PATHS = [
    "config/atlas.yaml",
    "config/apps.yaml",
    "config/mcp.yaml",
    "config/intents.yaml",
    "config/persona.md",
  ];
  const ACTION_HEADER = "x-atlas-action-token";
  const SIGNAL_INTERVAL_MS = 50;

  const refs = {
    connection: document.querySelector("#connection"),
    canvas: document.querySelector("#engine-canvas"),
    stateLabel: document.querySelector("#state-label"),
    audioLine: document.querySelector("#audio-line"),
    transcript: document.querySelector("#transcript"),
    workerSummary: document.querySelector("#worker-summary"),
    workerTabs: document.querySelector("#worker-tabs"),
    workerOutput: document.querySelector("#worker-output"),
    history: document.querySelector("#history-list"),
    historyCount: document.querySelector("#history-count"),
    resultTitle: document.querySelector("#result-title"),
    resultPlaceholder: document.querySelector("#result-placeholder"),
    historyResult: document.querySelector("#history-result"),
    voiceStatus: document.querySelector("#voice-status"),
    wakeStatus: document.querySelector("#wake-status"),
    audioInput: document.querySelector("#audio-input"),
    audioInputMode: document.querySelector("#audio-input-mode"),
    audioOutput: document.querySelector("#audio-output"),
    audioOutputMode: document.querySelector("#audio-output-mode"),
    claudeStatus: document.querySelector("#claude-status"),
    mcpList: document.querySelector("#mcp-list"),
    configList: document.querySelector("#config-list"),
    pairingStatus: document.querySelector("#pairing-status"),
  };

  let actionToken = "";
  let currentView = "live";
  let jobs = [];
  let selectedJobId = "";
  let selectedResultId = "";
  let transcriptSignature = "";
  let signalTimer = 0;
  let signalRequestPending = false;
  let stateRequestPending = false;
  let jobsRequestPending = false;
  let settingsRequestPending = false;
  const eventsByJob = new Map();
  const resultsByJob = new Map();

  function node(tag, className, text) {
    const element = document.createElement(tag);
    if (className) element.className = className;
    if (text !== undefined) element.textContent = text;
    return element;
  }

  function isRecord(value) {
    return value !== null && typeof value === "object" && !Array.isArray(value);
  }

  function stringValue(value) {
    return typeof value === "string" && value.trim() ? value.trim() : "—";
  }

  function clamp(value, minimum = 0, maximum = 1) {
    return Math.max(minimum, Math.min(maximum, value));
  }

  function createEngine(canvas) {
    const BAR_COUNT = 96;
    const UNIQUE_BANDS = 48;
    const INPUT_BANDS = 24;
    const TAU = Math.PI * 2;
    const palettes = {
      ASLEEP: {bar: [117, 119, 130], core: [137, 133, 155]},
      LISTENING: {bar: [124, 92, 255], core: [124, 92, 255]},
      THINKING: {bar: [168, 153, 255], core: [124, 92, 255]},
      SPEAKING: {bar: [244, 242, 255], core: [244, 242, 255]},
      OFFLINE: {bar: [148, 67, 76], core: [148, 67, 76]},
    };
    const context = canvas.getContext("2d", {alpha: false, desynchronized: true});
    const inputBands = new Float32Array(INPUT_BANDS);
    const expandedBands = new Float32Array(UNIQUE_BANDS);
    const barValues = new Float32Array(BAR_COUNT);
    const cosine = new Float32Array(BAR_COUNT);
    const sine = new Float32Array(BAR_COUNT);
    const barStartX = new Float32Array(BAR_COUNT);
    const barStartY = new Float32Array(BAR_COUNT);
    const arcPaths = new Array(3);
    const arcDirections = [1, -1, 1];
    const barColor = new Float32Array(3);
    const coreColor = new Float32Array(3);
    const fromBarColor = new Float32Array(palettes.OFFLINE.bar);
    const fromCoreColor = new Float32Array(palettes.OFFLINE.core);
    const toBarColor = new Float32Array(palettes.OFFLINE.bar);
    const toCoreColor = new Float32Array(palettes.OFFLINE.core);
    const glowSprite = document.createElement("canvas");
    const metrics = {samples: 0, lastMs: 0, averageMs: 0, maxMs: 0};
    let state = "OFFLINE";
    let energy = 0;
    let width = 0;
    let height = 0;
    let scale = 1;
    let centerX = .5;
    let centerY = .5;
    let transitionAt = performance.now();
    let animationFrame = 0;
    let running = false;

    for (let index = 0; index < BAR_COUNT; index += 1) {
      const angle = -Math.PI / 2 + index * TAU / BAR_COUNT;
      cosine[index] = Math.cos(angle);
      sine[index] = Math.sin(angle);
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

    function copyColor(target, source) {
      target[0] = source[0];
      target[1] = source[1];
      target[2] = source[2];
    }

    function mixColors(now) {
      const progress = clamp((now - transitionAt) / 300);
      for (let channel = 0; channel < 3; channel += 1) {
        barColor[channel] = fromBarColor[channel] + (toBarColor[channel] - fromBarColor[channel]) * progress;
        coreColor[channel] = fromCoreColor[channel] + (toCoreColor[channel] - fromCoreColor[channel]) * progress;
      }
    }

    function colorString(color, alpha = 1) {
      return `rgb(${Math.round(color[0])} ${Math.round(color[1])} ${Math.round(color[2])} / ${alpha})`;
    }

    function resize() {
      const bounds = canvas.getBoundingClientRect();
      const nextWidth = Math.max(1, Math.round(bounds.width));
      const nextHeight = Math.max(1, Math.round(bounds.height));
      const pixelRatio = Math.min(2, Math.max(1, window.devicePixelRatio || 1));
      if (
        nextWidth === width
        && nextHeight === height
        && canvas.width === Math.round(nextWidth * pixelRatio)
      ) return;
      width = nextWidth;
      height = nextHeight;
      scale = Math.min(width, height);
      centerX = width / 2;
      centerY = height / 2;
      const baseRadius = .34 * scale;
      for (let index = 0; index < BAR_COUNT; index += 1) {
        barStartX[index] = centerX + cosine[index] * baseRadius;
        barStartY[index] = centerY + sine[index] * baseRadius;
      }
      const radii = [.415, .452, .486];
      const starts = [-1.3, 1.04, 2.72];
      const lengths = [.82, 1.1, .62];
      for (let index = 0; index < arcPaths.length; index += 1) {
        const path = new Path2D();
        const end = starts[index] + lengths[index] * arcDirections[index];
        path.arc(0, 0, radii[index] * scale, starts[index], end, arcDirections[index] < 0);
        arcPaths[index] = path;
      }
      canvas.width = Math.round(width * pixelRatio);
      canvas.height = Math.round(height * pixelRatio);
      context.setTransform(pixelRatio, 0, 0, pixelRatio, 0, 0);
      context.imageSmoothingEnabled = true;
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

    function setState(nextState) {
      const resolved = VOICE_STATES.has(nextState) ? nextState : "OFFLINE";
      if (resolved === state) return;
      const now = performance.now();
      mixColors(now);
      copyColor(fromBarColor, barColor);
      copyColor(fromCoreColor, coreColor);
      copyColor(toBarColor, palettes[resolved].bar);
      copyColor(toCoreColor, palettes[resolved].core);
      transitionAt = now;
      state = resolved;
      canvas.setAttribute("aria-label", `Atlas engine ${state.toLowerCase()}`);
    }

    function drawArcs(now) {
      if (state === "OFFLINE") return;
      const rotation = state === "THINKING" ? now * .00022 : 0;
      context.lineWidth = Math.max(.55, scale * .0023);
      context.lineCap = "round";
      context.strokeStyle = colorString(barColor, state === "THINKING" ? .28 : .1);
      for (let index = 0; index < arcPaths.length; index += 1) {
        context.save();
        context.translate(centerX, centerY);
        context.rotate(rotation * arcDirections[index]);
        context.stroke(arcPaths[index]);
        context.restore();
      }
    }

    function drawBars(now) {
      const baseRadius = .34 * scale;
      const minimumLength = .02 * scale;
      const lengthRange = .14 * scale;
      const breathing = 1 + .03 * Math.sin(now * .001 * TAU * .2);
      const barPath = new Path2D();
      for (let index = 0; index < BAR_COUNT; index += 1) {
        const mirroredIndex = index < UNIQUE_BANDS ? index : BAR_COUNT - 1 - index;
        let target = 0;
        if (state === "LISTENING") target = clamp(expandedBands[mirroredIndex] * 1.3);
        if (state === "SPEAKING") target = expandedBands[mirroredIndex];
        if (state === "THINKING") {
          target = .16 + .09 * (.5 + .5 * Math.sin(index * .31 + now * .0014));
        }
        const current = barValues[index];
        barValues[index] += (target - current) * (target > current ? .5 : .12);
        if (state === "OFFLINE") continue;
        const length = state === "ASLEEP"
          ? minimumLength * breathing
          : minimumLength + lengthRange * barValues[index];
        const innerX = barStartX[index];
        const innerY = barStartY[index];
        barPath.moveTo(innerX, innerY);
        barPath.lineTo(innerX + cosine[index] * length, innerY + sine[index] * length);
      }
      if (state === "OFFLINE") return;
      context.lineWidth = Math.max(1.5, TAU * baseRadius / BAR_COUNT * .6);
      context.lineCap = "round";
      if (state === "THINKING" && typeof context.createConicGradient === "function") {
        const sweep = context.createConicGradient(now * .001, centerX, centerY);
        sweep.addColorStop(0, colorString(barColor, .24));
        sweep.addColorStop(.55, colorString(barColor, .42));
        sweep.addColorStop(.82, "rgb(244 242 255 / 0.96)");
        sweep.addColorStop(1, colorString(barColor, .24));
        context.strokeStyle = sweep;
      } else {
        const alpha = state === "ASLEEP" ? .34 : .88;
        context.strokeStyle = colorString(barColor, alpha);
      }
      context.stroke(barPath);
    }

    function drawCore(now) {
      const speakingPulse = state === "SPEAKING" ? 1 + energy * .1 * Math.sin(now * .018) : 1;
      const coreRadius = .18 * scale * speakingPulse;
      const glowRadius = coreRadius * (state === "ASLEEP" || state === "OFFLINE" ? 2.5 : 3.15);
      context.save();
      context.globalCompositeOperation = "screen";
      context.globalAlpha = state === "OFFLINE" ? .06 : state === "ASLEEP" ? .1 : .25 + energy * .12;
      context.drawImage(
        glowSprite,
        centerX - glowRadius,
        centerY - glowRadius,
        glowRadius * 2,
        glowRadius * 2,
      );
      context.restore();

      context.beginPath();
      context.arc(centerX, centerY, coreRadius, 0, TAU);
      context.fillStyle = colorString(coreColor, state === "OFFLINE" ? .055 : state === "ASLEEP" ? .09 : .18);
      context.fill();
      context.lineWidth = Math.max(.8, scale * .003);
      context.strokeStyle = state === "SPEAKING"
        ? "rgb(124 92 255 / 0.82)"
        : colorString(coreColor, state === "OFFLINE" ? .2 : .5);
      context.stroke();

      context.beginPath();
      context.arc(centerX, centerY, .30 * scale, 0, TAU);
      context.lineWidth = Math.max(.65, scale * .0022);
      context.strokeStyle = colorString(barColor, state === "OFFLINE" ? .34 : .24);
      context.stroke();
    }

    function draw(now) {
      mixColors(now);
      context.fillStyle = "#0b0c10";
      context.fillRect(0, 0, width, height);
      drawArcs(now);
      drawBars(now);
      drawCore(now);
    }

    function frame(now) {
      if (!running) return;
      const started = performance.now();
      draw(now);
      const elapsed = performance.now() - started;
      metrics.samples += 1;
      metrics.lastMs = elapsed;
      metrics.averageMs += (elapsed - metrics.averageMs) / metrics.samples;
      metrics.maxMs = Math.max(metrics.maxMs, elapsed);
      animationFrame = requestAnimationFrame(frame);
    }

    function start() {
      if (running || document.visibilityState !== "visible") return;
      resize();
      running = true;
      animationFrame = requestAnimationFrame(frame);
    }

    function stop() {
      running = false;
      if (animationFrame) cancelAnimationFrame(animationFrame);
      animationFrame = 0;
    }

    if (typeof ResizeObserver === "function") {
      const observer = new ResizeObserver(resize);
      observer.observe(canvas);
    } else {
      window.addEventListener("resize", resize);
    }
    resize();
    draw(performance.now());
    window.__atlasEngineMetrics = metrics;
    return {setSignal, setState, start, stop};
  }

  const engine = createEngine(refs.canvas);

  function selectView(name) {
    const resolved = ROUTES.has(name) ? name : "live";
    currentView = resolved;
    document.querySelectorAll("[data-view]").forEach((view) => {
      view.hidden = view.dataset.view !== resolved;
    });
    document.querySelectorAll("[data-view-target]").forEach((button) => {
      const active = button.dataset.viewTarget === resolved;
      button.classList.toggle("is-active", active);
      if (button.getAttribute("role") === "tab") {
        button.setAttribute("aria-selected", String(active));
        button.tabIndex = active ? 0 : -1;
      }
    });
    updateLiveActivity();
  }

  function routeFromHash() {
    const name = window.location.hash.slice(1).toLowerCase();
    const resolved = ROUTES.has(name) ? name : "live";
    if (name !== resolved) {
      history.replaceState(null, "", `${window.location.pathname}${window.location.search}#${resolved}`);
    }
    selectView(resolved);
  }

  document.querySelectorAll("[data-view-target]").forEach((button) => {
    button.addEventListener("click", () => {
      const target = button.dataset.viewTarget;
      if (window.location.hash === `#${target}`) selectView(target);
      else window.location.hash = target;
    });
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
      nextTab.focus();
      window.location.hash = nextTab.dataset.viewTarget;
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

  function setEngineState(rawState) {
    const state = VOICE_STATES.has(rawState) ? rawState : "OFFLINE";
    refs.stateLabel.textContent = state;
    engine.setState(state);
  }

  function renderTranscript(lines) {
    const safeLines = Array.isArray(lines) ? lines : [];
    const signature = JSON.stringify(safeLines);
    if (signature === transcriptSignature) return;
    transcriptSignature = signature;
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
    refs.audioLine.textContent = `mic: ${input.name} · speaker: ${output.name}`;
    refs.audioInput.textContent = input.name;
    refs.audioOutput.textContent = output.name;
    refs.audioInputMode.textContent = input.present && input.following ? "following system default" : "";
    refs.audioOutputMode.textContent = output.present && output.following ? "following system default" : "";
  }

  function firstString(...values) {
    return values.find((value) => typeof value === "string" && value.trim()) || "—";
  }

  function renderVoice(payload) {
    const config = isRecord(payload.config) ? payload.config : {};
    const wake = isRecord(payload.wake) ? payload.wake : {};
    refs.voiceStatus.textContent = firstString(payload.voice, payload.active_voice, config.active_voice);
    refs.wakeStatus.textContent = firstString(payload.wake_model, wake.model, config.wake_model);
  }

  async function refreshState() {
    if (stateRequestPending) return;
    stateRequestPending = true;
    try {
      const response = await fetch("/state", {cache: "no-store"});
      if (!response.ok) throw new Error("state unavailable");
      const payload = await response.json();
      setConnection(true);
      setEngineState(payload.state);
      renderTranscript(payload.transcript);
      renderVoice(payload);
      renderAudio(payload.audio);
    } catch (_error) {
      setConnection(false);
      setEngineState("OFFLINE");
      renderAudio(null);
    } finally {
      stateRequestPending = false;
    }
  }

  async function refreshSignal() {
    if (signalRequestPending || currentView !== "live" || document.visibilityState !== "visible") return;
    signalRequestPending = true;
    try {
      const response = await fetch("/signal", {cache: "no-store"});
      if (!response.ok) return;
      const payload = await response.json();
      engine.setSignal(payload.energy, payload.bands);
    } catch (_error) {
      engine.setSignal(0);
    } finally {
      signalRequestPending = false;
    }
  }

  function updateLiveActivity() {
    const active = currentView === "live" && document.visibilityState === "visible";
    if (active) {
      engine.start();
      if (!signalTimer) {
        refreshSignal();
        signalTimer = window.setInterval(refreshSignal, SIGNAL_INTERVAL_MS);
      }
      return;
    }
    engine.stop();
    if (signalTimer) window.clearInterval(signalTimer);
    signalTimer = 0;
  }

  function eventStore(jobId) {
    if (!eventsByJob.has(jobId)) eventsByJob.set(jobId, []);
    return eventsByJob.get(jobId);
  }

  function clearPairing() {
    actionToken = "";
    eventsByJob.clear();
    resultsByJob.clear();
    refs.pairingStatus.textContent = "Not paired";
    const selected = jobs.find((job) => job.id === selectedResultId);
    setResultPanel(
      selected ? selected.title : "No job selected",
      selected ? "pair to view" : "Select a completed job.",
    );
    renderWorkers();
    renderHistory();
  }

  async function refreshEvents(job) {
    if (!actionToken) return;
    const existing = eventStore(job.id);
    const lastSequence = existing.length ? existing[existing.length - 1].sequence : 0;
    try {
      const response = await fetch(`/jobs/${encodeURIComponent(job.id)}/events?after=${lastSequence}`, {
        cache: "no-store",
        headers: {[ACTION_HEADER]: actionToken},
      });
      if (response.status === 401) {
        clearPairing();
        return;
      }
      if (!response.ok) return;
      const payload = await response.json();
      if (Array.isArray(payload.events)) existing.push(...payload.events);
    } catch (_error) {
      return;
    }
  }

  function renderWorker(job) {
    refs.workerOutput.replaceChildren();
    if (!job) {
      refs.workerOutput.append(node("p", "empty", "No workers are active."));
      return;
    }
    const header = node("div", "worker-heading");
    const title = node("div", "worker-title");
    title.append(node("strong", "", job.title));
    title.append(node("span", "quiet", job.status));
    header.append(title);
    if (actionToken) {
      const cancel = node("button", "small-button", "Cancel");
      cancel.type = "button";
      cancel.addEventListener("click", () => cancelJob(job.id, cancel));
      header.append(cancel);
    }
    refs.workerOutput.append(header);
    const terminal = node("div", "terminal");
    const events = eventStore(job.id);
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
    const active = jobs.filter((job) => ACTIVE_JOB_STATES.has(job.status));
    refs.workerSummary.textContent = active.length ? `${active.length} active` : "idle";
    refs.workerTabs.replaceChildren();
    if (!active.some((job) => job.id === selectedJobId)) selectedJobId = active[0]?.id || "";
    active.forEach((job) => {
      const button = node("button", "worker-tab", job.title);
      const selected = job.id === selectedJobId;
      button.type = "button";
      button.role = "tab";
      button.classList.toggle("is-active", selected);
      button.setAttribute("aria-selected", String(selected));
      button.addEventListener("click", () => {
        selectedJobId = job.id;
        renderWorkers();
      });
      refs.workerTabs.append(button);
    });
    renderWorker(active.find((job) => job.id === selectedJobId));
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
      const response = await fetch(`/jobs/${encodeURIComponent(job.id)}/result`, {
        cache: "no-store",
        headers: {[ACTION_HEADER]: actionToken},
      });
      if (response.status === 401) {
        clearPairing();
        return;
      }
      if (!response.ok) throw new Error("result unavailable");
      const payload = await response.json();
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
      const active = ACTIVE_JOB_STATES.has(job.status);
      const row = node("article", "history-row");
      const open = node("button", "history-open");
      open.type = "button";
      open.disabled = active;
      const topLine = node("div", "history-topline");
      topLine.append(node("span", "history-title", job.title));
      const meta = node("span", "history-meta");
      meta.append(node("span", `job-state is-${job.status}`, job.status));
      meta.append(node("time", "history-time", relativeTime(job.updated_at)));
      topLine.append(meta);
      open.append(topLine);
      open.append(node("p", "history-summary", job.summary || job.error || (active ? "In progress" : "No summary available.")));
      if (!active) open.addEventListener("click", () => showResult(job));
      row.append(open);
      if (active) {
        const cancel = node("button", "small-button history-cancel", actionToken ? "Cancel" : "Pair to cancel");
        cancel.type = "button";
        cancel.disabled = !actionToken;
        cancel.addEventListener("click", () => cancelJob(job.id, cancel));
        row.append(cancel);
      }
      refs.history.append(row);
    });
  }

  async function cancelJob(jobId, button) {
    if (!actionToken) return;
    button.disabled = true;
    button.textContent = "Cancelling…";
    try {
      const response = await fetch(`/jobs/${encodeURIComponent(jobId)}/cancel`, {
        method: "POST",
        headers: {
          "content-type": "application/json",
          [ACTION_HEADER]: actionToken,
        },
        body: "{}",
      });
      if (response.status === 401) {
        clearPairing();
      }
      await refreshJobs();
    } catch (_error) {
      button.disabled = false;
      button.textContent = "Cancel";
    }
  }

  async function refreshJobs() {
    if (jobsRequestPending) return;
    jobsRequestPending = true;
    try {
      const response = await fetch("/jobs", {cache: "no-store"});
      if (!response.ok) return;
      const payload = await response.json();
      jobs = Array.isArray(payload.jobs) ? payload.jobs : [];
      const active = jobs.filter((job) => ACTIVE_JOB_STATES.has(job.status));
      await Promise.all(active.map(refreshEvents));
      renderWorkers();
      renderHistory();
    } catch (_error) {
      return;
    } finally {
      jobsRequestPending = false;
    }
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
      row.append(node("strong", "", stringValue(server.name)));
      const tools = Number.isInteger(server.tools) ? server.tools : 0;
      const detail = server.connected ? `${tools} ${tools === 1 ? "tool" : "tools"}` : stringValue(server.error || "disconnected");
      row.append(node("span", server.connected ? "is-connected" : "is-failed", detail));
      refs.mcpList.append(row);
    });
  }

  async function refreshSettings() {
    if (settingsRequestPending) return;
    settingsRequestPending = true;
    try {
      const [mcpResponse, healthResponse] = await Promise.all([
        fetch("/mcp", {cache: "no-store"}),
        fetch("/health", {cache: "no-store"}),
      ]);
      if (mcpResponse.ok) renderMcp((await mcpResponse.json()).servers);
      if (healthResponse.ok) {
        const health = await healthResponse.json();
        refs.claudeStatus.textContent = health.claude ? "Available" : "Unavailable";
        refs.claudeStatus.classList.toggle("is-unavailable", !health.claude);
      }
    } catch (_error) {
      refs.claudeStatus.textContent = "Unavailable";
      refs.claudeStatus.classList.add("is-unavailable");
    } finally {
      settingsRequestPending = false;
    }
  }

  async function pairFromFragment() {
    const params = new URLSearchParams(window.location.hash.slice(1));
    const token = params.get("pair");
    if (!token) return false;
    history.replaceState(null, "", `${window.location.pathname}${window.location.search}#live`);
    selectView("live");
    refs.pairingStatus.textContent = "Pairing";
    try {
      const response = await fetch("/pair", {
        method: "POST",
        headers: {"content-type": "application/json"},
        body: JSON.stringify({token}),
      });
      if (!response.ok) throw new Error("pairing failed");
      const payload = await response.json();
      actionToken = typeof payload.action_token === "string" ? payload.action_token : "";
      refs.pairingStatus.textContent = actionToken ? "Paired" : "Not paired";
      await refreshJobs();
    } catch (_error) {
      refs.pairingStatus.textContent = "Pairing failed";
    }
    return true;
  }

  async function handleHashChange() {
    if (await pairFromFragment()) return;
    routeFromHash();
  }

  CONFIG_PATHS.forEach((path) => refs.configList.append(node("li", "", path)));
  window.addEventListener("hashchange", handleHashChange);
  document.addEventListener("visibilitychange", updateLiveActivity);
  handleHashChange();
  refreshState();
  refreshJobs();
  refreshSettings();
  window.setInterval(refreshState, 500);
  window.setInterval(refreshJobs, 1000);
  window.setInterval(refreshSettings, 5000);
})();
