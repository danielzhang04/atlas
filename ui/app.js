(() => {
  "use strict";

  const STATES = new Set(["ASLEEP", "LISTENING", "THINKING", "ACTING", "SPEAKING"]);
  const ACTIVE_JOB_STATES = new Set(["queued", "running", "cancel_requested"]);
  const TERMINAL_JOB_STATES = new Set(["succeeded", "failed", "cancelled", "orphaned", "unavailable"]);
  const CAPTIONS = {
    ASLEEP: "wake word ready",
    LISTENING: "listening",
    THINKING: "thinking",
    ACTING: "running a confirmed action",
    SPEAKING: "speaking",
    OFFLINE: "waiting for Atlas",
    OBSERVER: "voice state lives on the worker surface",
  };
  const SOURCE_DEFS = [
    {
      id: "desktop", kinds: ["desktop", "local_files"], label: "Desktop + local files",
      detail: "Named desktop targets and reviewed local roots.",
      config: ["desktop_target_aliases", "local_file_roots"], guide: "source-desktop",
    },
    {
      id: "browser", kinds: ["browser"], label: "Browser",
      detail: "A trusted loopback browser bridge and exact allowed origins.",
      config: ["browser_bridge_url", "browser_allowed_origins"], guide: "source-browser",
    },
    {
      id: "google", kinds: ["google_drive", "google_docs", "gmail", "google_calendar"], label: "Google",
      detail: "Drive, Docs, Gmail, and Calendar through the external local credential broker.",
      config: ["google_broker_endpoint"], guide: "source-google",
    },
    {
      id: "spotify", kinds: ["spotify"], label: "Spotify",
      detail: "Named playback targets; account actions stay separately permissioned.",
      config: ["desktop_target_aliases"], guide: "source-spotify",
    },
  ];
  const GUIDED_DOCS = {
    voice: "voice", subscription: "subscription", desktop: "source-desktop",
    browser: "source-browser", google: "source-google", spotify: "source-spotify",
  };
  const GUIDES = {
    voice: {
      title: "Voice",
      sections: [
        ["What governs it", "The voice worker publishes ASLEEP, LISTENING, THINKING, ACTING, and SPEAKING. The Atlas core uses those real states; it does not infer wake state from transcript text."],
        ["Configuration", "Choose the speaking voice, wake model, microphone pin, output behavior, and silence timeout in the local Atlas configuration."],
      ],
      paths: ["config/atlas.yaml", "worker/app.py", "worker/state.py"],
      bullets: ["active_voice and voices", "wake_model, wake_input_device, and wake_threshold", "tts_output_device", "engagement_timeout_s"],
    },
    subscription: {
      title: "Subscription worker",
      sections: [
        ["What it is", "The separate local worker that claims durable slow-lane jobs and launches reviewed Claude subscription sessions. The main process never substitutes a metered heavy API."],
        ["What this page shows", "Only safe health codes, public job states, and bounded public lifecycle events. Raw terminal output and private results are not copied into the public dashboard."],
      ],
      paths: ["worker/subscription_cli.py", "worker/subscription_supervisor.py", "worker/worker_health_file.py", "docs/plans/2026-08-21-atlas-heavy-work-loop-plan.md"],
      bullets: ["available: admitting new heavy jobs", "degraded: alive but not fully ready", "unavailable: not admitting new heavy jobs"],
    },
    transcript: {
      title: "Transcript",
      sections: [
        ["Retention", "The worker keeps a ring of up to 50 final lines in process memory. Reloading the page reads that ring; the UI does not create a second transcript store."],
        ["Audio boundary", "The page never captures audio. While Atlas is engaged, the voice worker streams microphone audio to the configured speech-to-text provider. While asleep, that stream is detached."],
      ],
      paths: ["worker/state.py", "worker/app.py"],
      bullets: [],
    },
    display: {
      title: "Display",
      sections: [
        ["Motion", "The Atlas Engine combines frequency bars, telemetry arcs, a deforming signal ring, and particles. While listening or speaking, the bars and signal deformation follow a live normalized loudness value. Reduced-motion follows the operating-system preference."],
        ["Layout", "Home is fixed to the viewport. Transcript and task panes scroll internally; Sources, History, and Settings use a thin custom scrollbar."],
        ["Audio boundary", "The local wake listener reduces each 80 ms microphone frame to one ephemeral 0–1 loudness value. Raw samples and frequency bins never enter this page or state history."],
      ],
      paths: ["ui/styles.css", "ui/app.js"],
      bullets: [],
    },
    pairing: {
      title: "Action pairing",
      sections: [
        ["Purpose", "Pairing proves the page was opened by this Atlas runtime. It unlocks exact proposal confirmations, receipts, and encrypted private results for that runtime only."],
        ["How it works", "Atlas opens a one-use token in the URL fragment. Browsers do not send fragments over HTTP. The page consumes it, removes it from the address bar, and keeps the returned action token only in memory."],
        ["What it does not do", "It does not connect Google, pair a browser bridge, read credentials, or grant arbitrary command access."],
      ],
      paths: ["worker/actionauth.py", "worker/stateserver.py", "worker/ui_server.py"],
      bullets: [],
    },
    configuration: {
      title: "Configuration map",
      sections: [
        ["Runtime values", "Non-secret Atlas settings live in config/atlas.yaml. Changes take effect when the owning worker restarts."],
        ["Capability policy", "config/capabilities.yaml declares what operations exist, their risk tier, required connection, and confirmation rule."],
        ["Secrets", "Credentials remain outside these files and are never shown or edited by this UI."],
      ],
      paths: ["config/atlas.yaml", "config/capabilities.yaml", "CLAUDE.md"],
      bullets: ["Add or edit named values in atlas.yaml", "Remove a value to disable that connection", "Restart the relevant Atlas process", "The issue number clears when the runtime reports connected"],
    },
    "source-desktop": {
      title: "Configure desktop + local files",
      sections: [
        ["Keys", "Use desktop_target_aliases for named application targets. local_file_roots remains unavailable until its reviewed activation boundary is implemented."],
        ["Status", "Atlas clears this source badge only when the runtime reports an active adapter."],
      ],
      paths: ["config/atlas.yaml → desktop_target_aliases", "config/atlas.yaml → local_file_roots", "worker/runtime.py"],
      bullets: ["Add named targets rather than arbitrary paths", "Restart Atlas after editing", "Local file mutations remain human-confirmed"],
    },
    "source-browser": {
      title: "Configure browser",
      sections: [
        ["Keys", "Set browser_bridge_url to the reviewed loopback bridge and list exact http/https sites in browser_allowed_origins."],
        ["Boundary", "Pairing this dashboard is not browser pairing. The browser bridge is a separate explicit connection."],
      ],
      paths: ["config/atlas.yaml → browser_bridge_url", "config/atlas.yaml → browser_allowed_origins", "worker/browser_protocol.py"],
      bullets: ["Use a loopback bridge URL", "Allow exact origins only", "Restart Atlas and complete the bridge's own pairing"],
    },
    "source-google": {
      title: "Configure Google",
      sections: [
        ["Key", "google_broker_endpoint points to the separately reviewed local credential broker. Atlas receives typed results; it does not receive OAuth credentials."],
        ["Boundary", "Connecting Google remains an explicit human activation gate."],
      ],
      paths: ["config/atlas.yaml → google_broker_endpoint", "worker/connectors.py", "worker/runtime.py"],
      bullets: ["Start the external local broker", "Set its loopback endpoint", "Restart Atlas", "Complete scoped OAuth outside Atlas"],
    },
    "source-spotify": {
      title: "Configure Spotify",
      sections: [
        ["Desktop targets", "Named Spotify targets can be added under desktop_target_aliases for reviewed open/focus actions."],
        ["Account actions", "Playback through account authorization remains a separate OAuth capability."],
      ],
      paths: ["config/atlas.yaml → desktop_target_aliases", "config/capabilities.yaml → spotify.*"],
      bullets: ["Add a named spotify_uri target", "Restart Atlas", "Confirm any side effect in the paired UI"],
    },
  };

  const refs = {
    engine: document.querySelector("#atlas-engine"),
    engineCanvas: document.querySelector("#atlas-visual"),
    stateLabel: document.querySelector("#state-label"),
    stateCaption: document.querySelector("#state-caption"),
    observerNote: document.querySelector("#observer-note"),
    connection: document.querySelector("#connection-state"),
    transcript: document.querySelector("#transcript"),
    taskTabs: document.querySelector("#task-tabs"),
    taskPane: document.querySelector("#task-pane"),
    workSummary: document.querySelector("#work-summary"),
    workersBadge: document.querySelector("#workers-badge"),
    sources: document.querySelector("#sources"),
    history: document.querySelector("#history-card"),
    catalogCount: document.querySelector("#catalog-count"),
    voiceSetting: document.querySelector("#voice-setting"),
    workerHealth: document.querySelector("#worker-health"),
    workerHealthDetail: document.querySelector("#worker-health-detail"),
    pairingStatus: document.querySelector("#pairing-status"),
    pairingToken: document.querySelector("#pairing-token"),
    pairingSubmit: document.querySelector("#pairing-submit"),
    motionStatus: document.querySelector("#motion-status"),
    sourcesBadge: document.querySelector("#sources-badge"),
    settingsBadge: document.querySelector("#settings-badge"),
    alertsButton: document.querySelector("#alerts-button"),
    alertsBadge: document.querySelector("#alerts-badge"),
    docDialog: document.querySelector("#doc-dialog"),
    docTitle: document.querySelector("#doc-title"),
    docBody: document.querySelector("#doc-body"),
  };

  let snapshot = null;
  let capabilities = [];
  let actions = [];
  let jobs = [];
  let receiptHistory = [];
  let workerHealth = {status: "unavailable", reason: "checking"};
  let actionToken = "";
  let surfaceMode = "unknown";
  let online = false;
  let selectedJobId = "";
  let sourceIssueCount = 0;
  const jobEvents = new Map();
  const privateResults = new Map();
  let stateRequestActive = false;
  let signalRequestActive = false;
  let actionRequestActive = false;

  class AtlasVisual {
    constructor(canvas, engine) {
      this.canvas = canvas;
      this.engine = engine;
      this.context = canvas?.getContext("2d", {alpha: true}) || null;
      this.state = "OFFLINE";
      this.energy = 0;
      this.targetEnergy = 0;
      this.frame = 0;
      this.startedAt = performance.now();
      this.motion = !window.matchMedia("(prefers-reduced-motion: reduce)").matches;
      this.points = Array.from({length: 132}, (_, index) => ({
        angle: (index / 132) * Math.PI * 2 + Math.sin(index * 9.7) * .04,
        radius: .3 + ((index * 37) % 100) / 690,
        phase: (index * 1.618) % (Math.PI * 2),
        size: .45 + ((index * 19) % 10) / 16,
      }));
      this.resize = this.resize.bind(this);
      this.draw = this.draw.bind(this);
      if (this.context) {
        this.observer = new ResizeObserver(this.resize);
        this.observer.observe(canvas);
        this.resize();
        this.frame = requestAnimationFrame(this.draw);
      }
    }

    setState(state) {
      this.state = state;
      if (!this.motion) this.draw(performance.now(), true);
    }

    setEnergy(value) {
      const numeric = Number(value);
      this.targetEnergy = Number.isFinite(numeric) ? Math.max(0, Math.min(1, numeric)) : 0;
    }

    resize() {
      if (!this.context) return;
      const box = this.canvas.getBoundingClientRect();
      const ratio = Math.min(window.devicePixelRatio || 1, 2);
      this.canvas.width = Math.max(1, Math.round(box.width * ratio));
      this.canvas.height = Math.max(1, Math.round(box.height * ratio));
      this.context.setTransform(ratio, 0, 0, ratio, 0, 0);
      this.width = box.width;
      this.height = box.height;
    }

    palette() {
      return {
        OFFLINE: [102, 102, 98, .18, .05],
        OBSERVER: [154, 138, 255, .28, .12],
        ASLEEP: [150, 150, 145, .24, .07],
        LISTENING: [154, 138, 255, .95, .28],
        THINKING: [154, 138, 255, .9, .24],
        ACTING: [154, 138, 255, .92, .27],
        SPEAKING: [154, 138, 255, 1, .34],
      }[this.state] || [140, 140, 136, .25, .08];
    }

    draw(stamp, once = false) {
      if (!this.context || !this.width || !this.height) return;
      const ctx = this.context;
      const [red, green, blue, alpha, activity] = this.palette();
      const energyEnabled = this.state === "LISTENING" || this.state === "SPEAKING";
      const requestedEnergy = energyEnabled ? this.targetEnergy : 0;
      const smoothing = requestedEnergy > this.energy ? .34 : .13;
      this.energy += (requestedEnergy - this.energy) * smoothing;
      const liveEnergy = energyEnabled ? Math.max(.025, this.energy) : activity;
      const elapsed = this.motion ? (stamp - this.startedAt) / 1000 : 0;
      const speed = this.state === "SPEAKING" ? 2.4 : this.state === "THINKING" ? 1.25 : this.state === "ACTING" ? 1.7 : .55;
      const pulse = .5 + .5 * Math.sin(elapsed * speed * 3.1);
      const cx = this.width / 2;
      const cy = this.height / 2;
      const size = Math.min(this.width, this.height);
      ctx.clearRect(0, 0, this.width, this.height);
      ctx.save();
      ctx.translate(cx, cy);

      const glow = ctx.createRadialGradient(0, 0, size * .05, 0, 0, size * .48);
      glow.addColorStop(0, `rgba(${red},${green},${blue},${alpha * .13})`);
      glow.addColorStop(.46, `rgba(${red},${green},${blue},${alpha * .035})`);
      glow.addColorStop(1, "rgba(0,0,0,0)");
      ctx.fillStyle = glow;
      ctx.beginPath();
      ctx.arc(0, 0, size * .48, 0, Math.PI * 2);
      ctx.fill();

      [0, 1, 2].forEach((ring) => {
        const radius = size * (.255 + ring * .071);
        const direction = ring === 1 ? -1 : 1;
        const start = elapsed * (.16 + ring * .07) * direction + ring * 1.7;
        ctx.beginPath();
        ctx.setLineDash(ring === 1 ? [2, 7] : [18 - ring * 3, 6 + ring * 3]);
        ctx.lineWidth = ring === 0 ? 1.1 : .7;
        ctx.strokeStyle = `rgba(${red},${green},${blue},${alpha * (.42 - ring * .09)})`;
        ctx.arc(0, 0, radius, start, start + Math.PI * (1.1 + ring * .27));
        ctx.stroke();
      });
      ctx.setLineDash([]);

      const bars = 80;
      for (let index = 0; index < bars; index += 1) {
        const angle = (index / bars) * Math.PI * 2 - Math.PI / 2;
        const harmonic = .5 + .5 * Math.sin(index * .73 + elapsed * speed * 4.2);
        const carrier = .5 + .5 * Math.sin(index * .19 - elapsed * speed * 2.1);
        const spike = Math.pow(harmonic * .64 + carrier * .36, 2.1);
        const barLength = size * (.009 + liveEnergy * (.045 + spike * .13) * (.72 + pulse * .28));
        const radius = size * .405;
        ctx.save();
        ctx.rotate(angle);
        ctx.fillStyle = `rgba(${red},${green},${blue},${alpha * (.28 + spike * .66)})`;
        ctx.fillRect(-.65, -radius - barLength, 1.3, barLength);
        ctx.restore();
      }

      ctx.beginPath();
      const wavePoints = 150;
      for (let index = 0; index <= wavePoints; index += 1) {
        const angle = (index / wavePoints) * Math.PI * 2;
        const wave = Math.sin(angle * 6 - elapsed * speed * 3.5) * liveEnergy * size * .025;
        const fine = Math.sin(angle * 17 + elapsed * speed * 2.1) * liveEnergy * size * .009;
        const radius = size * .214 + wave + fine;
        const x = Math.cos(angle) * radius;
        const y = Math.sin(angle) * radius;
        if (index === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
      }
      ctx.closePath();
      ctx.strokeStyle = `rgba(${red},${green},${blue},${alpha * .72})`;
      ctx.lineWidth = 1;
      ctx.shadowColor = `rgba(${red},${green},${blue},${alpha * .55})`;
      ctx.shadowBlur = liveEnergy * 16;
      ctx.stroke();
      ctx.shadowBlur = 0;

      this.points.forEach((point, index) => {
        const drift = elapsed * (.018 + (index % 7) * .0015) * (index % 2 ? 1 : -1);
        const radius = size * point.radius + Math.sin(elapsed * .8 + point.phase) * size * liveEnergy * .012;
        const x = Math.cos(point.angle + drift) * radius;
        const y = Math.sin(point.angle + drift) * radius;
        const flicker = .35 + .65 * (.5 + .5 * Math.sin(elapsed * 1.8 + point.phase));
        ctx.fillStyle = `rgba(${red},${green},${blue},${alpha * .36 * flicker})`;
        ctx.fillRect(x, y, point.size, point.size);
      });

      ctx.restore();
      if (!once && this.motion) this.frame = requestAnimationFrame(this.draw);
    }
  }

  const atlasVisual = new AtlasVisual(refs.engineCanvas, refs.engine);

  function node(tag, className, text) {
    const element = document.createElement(tag);
    if (className) element.className = className;
    if (text !== undefined) element.textContent = text;
    return element;
  }

  function safeState(value) {
    return STATES.has(value) ? value : "ASLEEP";
  }

  function timeLabel(value) {
    if (typeof value === "number" && Number.isFinite(value)) {
      return new Date(value * 1000).toLocaleTimeString([], {hour: "2-digit", minute: "2-digit", second: "2-digit"});
    }
    if (typeof value !== "string" || !value) return "";
    const date = new Date(value);
    if (!Number.isNaN(date.valueOf())) {
      return date.toLocaleTimeString([], {hour: "2-digit", minute: "2-digit", second: "2-digit"});
    }
    const match = value.match(/T(\d{2}:\d{2}:\d{2})/);
    return match ? match[1] : "";
  }

  function connectedStatus(value) {
    return typeof value === "string" && ["connected", "ready", "available"].includes(value.toLowerCase());
  }

  function sourceState(definition) {
    const items = capabilities.filter((item) => item && typeof item === "object" && definition.kinds.includes(item.kind));
    const connected = items.some((item) => connectedStatus(item.status));
    const needsConfiguration = items.some((item) => typeof item.status === "string" &&
      ["configuration-needed", "needs-connection", "disconnected", "error"].includes(item.status.toLowerCase()));
    const detail = items.find((item) => typeof item.detail === "string")?.detail || definition.detail;
    return {items, detail, status: connected ? "connected" : needsConfiguration ? "configuration-needed" : "planned"};
  }

  function setBadge(element, count) {
    element.hidden = count < 1;
    element.textContent = count > 99 ? "99+" : String(count);
  }

  function effectiveWorkerHealth() {
    const status = typeof workerHealth.status === "string" ? workerHealth.status : "unavailable";
    if (status === "available" && typeof workerHealth.checked_at !== "number") {
      return {status: "unavailable", reason: "health_timestamp_missing"};
    }
    if (status === "available") {
      const age = (Date.now() / 1000) - workerHealth.checked_at;
      if (!Number.isFinite(age) || age < 0 || age > 30) {
        return {status: "unavailable", reason: "health_stale"};
      }
    }
    return {status, reason: typeof workerHealth.reason === "string" ? workerHealth.reason : ""};
  }

  function renderAttention() {
    const deviceIssue = Boolean(snapshot && snapshot.output_device &&
      snapshot.output_device.configured && !snapshot.output_device.resolved);
    const workerIssue = effectiveWorkerHealth().status !== "available";
    const observerIssue = surfaceMode === "observer";
    const settingsIssues = Number(deviceIssue) + Number(workerIssue) + Number(observerIssue);
    const workerAttention = actions.length;
    const total = sourceIssueCount + settingsIssues + workerAttention;
    setBadge(refs.sourcesBadge, sourceIssueCount);
    setBadge(refs.settingsBadge, settingsIssues);
    setBadge(refs.workersBadge, workerAttention);
    setBadge(refs.alertsBadge, total);
    refs.alertsButton.hidden = total < 1;
    document.querySelector("#setting-subscription")?.classList.toggle("needs-attention", workerIssue);
    document.querySelector("#setting-voice")?.classList.toggle("needs-attention", deviceIssue);
  }

  function renderState(isOnline) {
    online = isOnline;
    const state = isOnline && snapshot ? safeState(snapshot.state) : "OFFLINE";
    const displayState = isOnline && surfaceMode === "observer" ? "OBSERVER" : state;
    refs.engine.className = `atlas-engine engine--${displayState.toLowerCase()}`;
    refs.engine.setAttribute("aria-label", `Atlas is ${displayState.toLowerCase()}`);
    atlasVisual.setState(displayState);
    refs.stateLabel.textContent = displayState;
    refs.stateCaption.textContent = CAPTIONS[displayState];
    refs.observerNote.hidden = displayState !== "OBSERVER";
    refs.connection.className = `connection-state ${!isOnline ? "is-offline" : surfaceMode === "observer" ? "is-observer" : "is-online"}`;
    refs.connection.lastElementChild.textContent = !isOnline ? "Atlas unavailable" :
      surfaceMode === "observer" ? "observer surface" : "voice worker connected";
    if (snapshot && typeof snapshot.voice === "string") refs.voiceSetting.textContent = snapshot.voice;
    renderAttention();
  }

  function renderTranscript() {
    const lines = snapshot && Array.isArray(snapshot.transcript) ? snapshot.transcript : [];
    const pinnedToBottom = refs.transcript.scrollHeight - refs.transcript.scrollTop - refs.transcript.clientHeight < 40;
    refs.transcript.replaceChildren();
    if (!lines.length) {
      const message = surfaceMode === "observer"
        ? "This observer does not receive the live voice transcript."
        : "No transcript yet.";
      refs.transcript.append(node("p", "empty", message));
      return;
    }
    lines.forEach((line) => {
      const role = line && line.role === "user" ? "you" : "atlas";
      const row = node("div", `transcript-line ${role === "atlas" ? "is-atlas" : "is-user"}`);
      row.append(node("span", "transcript-role", role));
      row.append(node("span", "transcript-text", line && typeof line.text === "string" ? line.text : ""));
      row.append(node("span", "transcript-time", timeLabel(line && line.t)));
      refs.transcript.append(row);
    });
    if (pinnedToBottom) refs.transcript.scrollTop = refs.transcript.scrollHeight;
  }

  function proposalCard(action) {
    const card = node("article", "proposal-card");
    const heading = node("div", "proposal-heading");
    heading.append(node("strong", "", typeof action.label === "string" ? action.label : "Pending action"));
    heading.append(node("span", "proposal-status", typeof action.status === "string" ? action.status : "pending"));
    card.append(heading);
    card.append(node("p", "proposal-preview", typeof action.preview === "string" ? action.preview : "No preview supplied."));
    if (typeof action.proposal_hash === "string") card.append(node("p", "proposal-hash", `proposal ${action.proposal_hash}`));
    if (typeof action.risk === "string") card.append(node("p", "proposal-risk", action.risk));
    const pending = ["pending", "proposed", "confirmed", "awaiting-confirmation", "awaiting-human-confirmation"]
      .includes(String(action.status || "").toLowerCase().replaceAll("_", "-"));
    if (pending) {
      const controls = node("div", "proposal-controls");
      if (action.confirmable === true) {
        const confirm = node("button", "proposal-confirm", "Confirm & run");
        confirm.type = "button";
        confirm.addEventListener("click", () => submitAction(action, "run"));
        controls.append(confirm);
      } else {
        card.append(node("p", "proposal-warning",
          "This proposal cannot be confirmed because its exact parameters do not fit in the review preview. Cancel it and prepare a smaller action."));
      }
      const cancel = node("button", "proposal-cancel", "Cancel");
      cancel.type = "button";
      cancel.addEventListener("click", () => submitAction(action, "cancel"));
      controls.append(cancel);
      card.append(controls);
    }
    return card;
  }

  function eventDescription(event) {
    if (typeof event.summary === "string" && event.summary) return event.summary;
    if (typeof event.code === "string" && event.code) return event.code.replaceAll("_", " ");
    if (typeof event.reason === "string" && event.reason) return event.reason.replaceAll("_", " ");
    if (typeof event.worker_id === "string" && event.worker_id) return `claimed by ${event.worker_id}`;
    const kind = typeof event.kind === "string" ? event.kind.replaceAll("_", " ") : "event";
    return `${kind} → ${event.state || "unknown"}`;
  }

  function taskLabel(job) {
    const operation = typeof job.summary === "string" && job.summary
      ? job.summary
      : typeof job.operation === "string" && job.operation ? job.operation : "Atlas task";
    return operation.length > 24 ? `${operation.slice(0, 23)}…` : operation;
  }

  function renderWork() {
    const active = jobs
      .filter((job) => ACTIVE_JOB_STATES.has(String(job.status || "").toLowerCase()))
      .slice(0, 12);
    refs.workSummary.textContent = active.length ? `${active.length} active` : "idle";
    renderAttention();
    refs.taskTabs.replaceChildren();

    if (!active.length && !actions.length) {
      selectedJobId = "";
      refs.taskPane.replaceChildren(node("p", "empty", "No workers are active."));
      return;
    }
    if (!active.some((job) => job.id === selectedJobId)) {
      selectedJobId = (active[0] || {}).id || "";
    }
    active.forEach((job) => {
      const status = String(job.status || "unknown").toLowerCase();
      const button = node("button", `task-tab is-${status.replaceAll("_", "-")}`);
      button.type = "button";
      button.role = "tab";
      button.dataset.jobId = job.id;
      button.setAttribute("aria-selected", String(job.id === selectedJobId));
      button.classList.toggle("is-active", job.id === selectedJobId);
      button.append(node("span", "task-dot"));
      button.append(node("span", "task-tab-label", taskLabel(job)));
      button.addEventListener("click", () => {
        selectedJobId = job.id;
        renderWork();
        refreshSelectedJobEvents();
      });
      refs.taskTabs.append(button);
    });

    refs.taskPane.replaceChildren();
    if (actions.length) {
      const stack = node("div", "proposal-stack");
      actions.forEach((action) => stack.append(proposalCard(action)));
      refs.taskPane.append(stack);
    }
    const job = active.find((item) => item.id === selectedJobId);
    if (!job) {
      if (!actions.length) refs.taskPane.append(node("p", "empty", "No workers are active."));
      return;
    }
    const heading = node("div", "task-header");
    const title = node("div", "task-title");
    title.append(node("strong", "", taskLabel(job)));
    title.append(node("span", "", [job.lane, typeof job.id === "string" ? job.id.slice(0, 8) : ""].filter(Boolean).join(" · ")));
    heading.append(title);
    heading.append(node("span", "task-state", typeof job.status === "string" ? job.status : "unknown"));
    refs.taskPane.append(heading);

    const terminal = node("div", "terminal");
    const events = jobEvents.get(job.id) || [];
    if (!events.length) {
      const line = node("div", "terminal-line");
      line.append(node("span", "terminal-time", timeLabel(job.updated_at)));
      line.append(node("span", "terminal-state", job.status || "known"));
      line.append(node("span", "terminal-copy", job.code ? job.code.replaceAll("_", " ") : "waiting for lifecycle events"));
      terminal.append(line);
    } else {
      events.forEach((event) => {
        const line = node("div", "terminal-line");
        line.append(node("span", "terminal-time", timeLabel(event.timestamp)));
        line.append(node("span", "terminal-state", event.state || "event"));
        line.append(node("span", "terminal-copy", eventDescription(event)));
        terminal.append(line);
      });
    }
    if (ACTIVE_JOB_STATES.has(String(job.status || "").toLowerCase())) {
      terminal.append(node("span", "terminal-cursor"));
    }
    refs.taskPane.append(terminal);

    if (job.result_available === true && typeof job.id === "string") {
      const resultArea = node("div", "private-result");
      const cached = privateResults.get(job.id);
      const open = node("button", "", cached ? "Refresh result" : "Open result");
      open.type = "button";
      open.disabled = !actionToken;
      open.addEventListener("click", () => loadPrivateResult(job.id));
      resultArea.append(open);
      if (!actionToken) resultArea.append(node("span", "private-result-note", "Pair this UI to open the encrypted result."));
      if (cached && typeof cached.answer === "string") {
        const download = node("button", "", "Download");
        download.type = "button";
        download.addEventListener("click", () => downloadPrivateResult(job.id, cached));
        resultArea.append(download);
        resultArea.append(node("pre", "private-result-body", cached.answer));
        const count = Number.isInteger(cached.evidence_count) ? cached.evidence_count : 0;
        resultArea.append(node("span", "private-result-note", `independently reviewed · ${count} evidence receipt${count === 1 ? "" : "s"}`));
      }
      refs.taskPane.append(resultArea);
    }
  }

  function renderSources() {
    refs.sources.replaceChildren();
    sourceIssueCount = 0;
    let connectedCount = 0;
    SOURCE_DEFS.forEach((definition) => {
      const state = sourceState(definition);
      if (state.status === "connected") connectedCount += 1;
      if (state.status === "configuration-needed") sourceIssueCount += 1;
      const card = node("article", "source-card");
      card.id = `source-${definition.id}`;
      card.append(node("h2", "", definition.label));
      card.append(node("span", `source-status source-status--${state.status}`, state.status.replaceAll("-", " ")));
      card.append(node("p", "", state.detail));
      if (state.status === "configuration-needed") {
        const badge = node("span", "count-badge card-count", "1");
        badge.setAttribute("aria-label", "One configuration item");
        card.append(badge);
      }
      const actionsRow = node("div", "source-actions");
      const configure = node("button", "text-button", state.status === "connected" ? "Review configuration" : "Configure");
      configure.type = "button";
      configure.addEventListener("click", () => openGuide(definition.guide));
      actionsRow.append(configure);
      if (state.status !== "connected") {
        const guided = node("button", "text-button text-button--primary", "Guide me");
        guided.type = "button";
        guided.addEventListener("click", () => startGuidedSetup(definition.id));
        actionsRow.append(guided);
      }
      card.append(actionsRow);
      refs.sources.append(card);
    });
    refs.catalogCount.textContent = connectedCount
      ? `${connectedCount} connected source${connectedCount === 1 ? "" : "s"}`
      : "No connected sources";
    renderAttention();
  }

  function renderHistory() {
    refs.history.replaceChildren();
    const session = snapshot && snapshot.session_id;
    if (session) {
      const meta = node("div", "history-meta");
      meta.append(node("span", "", `session ${session}`));
      if (snapshot.since) meta.append(node("span", "", `started ${snapshot.since}`));
      refs.history.append(meta);
    }
    const completedJobs = jobs.filter((job) => TERMINAL_JOB_STATES.has(String(job.status || "").toLowerCase()));
    if (!completedJobs.length && !receiptHistory.length) {
      refs.history.append(node("p", "empty", actionToken ? "No completed runs or action receipts yet." : "No completed runs. Pair this UI to view action receipts."));
      return;
    }
    completedJobs.forEach((job) => {
      const item = node("article", "history-item history-run");
      const heading = node("div", "proposal-heading");
      heading.append(node("strong", "", taskLabel(job)));
      heading.append(node("span", `proposal-status status-${String(job.status || "unknown")}`, job.status || "unknown"));
      item.append(heading);
      item.append(node("p", "history-run-meta", [job.lane, timeLabel(job.updated_at), typeof job.id === "string" ? job.id.slice(0, 8) : ""].filter(Boolean).join(" · ")));
      if (job.result_available === true && typeof job.id === "string") {
        const resultArea = node("div", "private-result");
        const cached = privateResults.get(job.id);
        const open = node("button", "", cached ? "Refresh result" : "Open result");
        open.type = "button";
        open.disabled = !actionToken;
        open.addEventListener("click", () => loadPrivateResult(job.id));
        resultArea.append(open);
        if (!actionToken) resultArea.append(node("span", "private-result-note", "Pair this UI to open the encrypted result."));
        if (cached && typeof cached.answer === "string") {
          const download = node("button", "", "Download");
          download.type = "button";
          download.addEventListener("click", () => downloadPrivateResult(job.id, cached));
          resultArea.append(download);
          resultArea.append(node("pre", "private-result-body", cached.answer));
        }
        item.append(resultArea);
      }
      refs.history.append(item);
    });
    receiptHistory.forEach((receipt) => {
      const item = node("article", "history-item");
      const heading = node("div", "proposal-heading");
      heading.append(node("strong", "", receipt.capability_id || "Action"));
      heading.append(node("span", "proposal-status", receipt.status || "unknown"));
      item.append(heading);
      item.append(node("pre", "proposal-receipt", JSON.stringify(receipt, null, 2)));
      refs.history.append(item);
    });
  }

  function renderWorkerHealth() {
    const effective = effectiveWorkerHealth();
    const status = effective.status;
    refs.workerHealth.textContent = status;
    refs.workerHealthDetail.textContent = effective.reason
      ? effective.reason.replaceAll("_", " ")
      : status === "available" ? "Ready to admit subscription work." : "No worker detail reported.";
    renderAttention();
  }

  function clearPairing() {
    actionToken = "";
    privateResults.clear();
    actions = [];
    receiptHistory = [];
    refs.pairingStatus.textContent = "Pairing required";
    renderWork();
    renderHistory();
  }

  async function loadPrivateResult(jobId) {
    if (!actionToken) return;
    try {
      const response = await fetch(`/jobs/${encodeURIComponent(jobId)}/result`, {
        cache: "no-store", headers: {"x-atlas-action-token": actionToken},
      });
      if (response.status === 401) {
        clearPairing();
        return;
      }
      if (!response.ok) throw new Error("private result request failed");
      const payload = await response.json();
      if (!payload || payload.job_id !== jobId || typeof payload.answer !== "string") throw new Error("invalid private result");
      privateResults.set(jobId, payload);
    } catch (_error) {
      privateResults.delete(jobId);
    }
    renderWork();
    renderHistory();
  }

  function downloadPrivateResult(jobId, result) {
    if (!result || typeof result.answer !== "string") return;
    const requested = typeof result.artifact_name === "string" ? result.artifact_name : `atlas-result-${jobId}.md`;
    const filename = requested.replace(/[\\/:*?"<>|\u0000-\u001f]/g, "_").slice(0, 255) || `atlas-result-${jobId}.md`;
    const url = URL.createObjectURL(new Blob([result.answer], {type: "text/markdown;charset=utf-8"}));
    const anchor = document.createElement("a");
    anchor.href = url;
    anchor.download = filename;
    anchor.click();
    URL.revokeObjectURL(url);
  }

  async function submitAction(action, operation) {
    if (actionRequestActive || !action || typeof action.id !== "string" || typeof action.proposal_hash !== "string") return;
    if (operation === "run" && action.confirmable !== true) return;
    actionRequestActive = true;
    document.querySelectorAll(".proposal-controls button").forEach((button) => { button.disabled = true; });
    try {
      const response = await fetch(`/actions/${encodeURIComponent(action.id)}/${operation}`, {
        method: "POST",
        headers: {"content-type": "application/json", "x-atlas-action-token": actionToken},
        body: JSON.stringify({proposal_hash: action.proposal_hash}),
      });
      if (!response.ok) throw new Error("action request failed");
      await refreshActions();
      await refreshReceipts();
    } catch (_error) {
      refs.taskPane.prepend(node("p", "proposal-error", `Unable to ${operation} this proposal. Refresh and review it again.`));
    } finally {
      actionRequestActive = false;
      renderWork();
    }
  }

  async function pairUI() {
    const token = refs.pairingToken.value;
    if (!token) return;
    refs.pairingSubmit.disabled = true;
    try {
      const response = await fetch("/pair", {
        method: "POST",
        headers: {"content-type": "application/json"},
        body: JSON.stringify({token}),
      });
      const payload = response.ok ? await response.json() : {};
      actionToken = typeof payload.action_token === "string" ? payload.action_token : "";
      refs.pairingToken.value = "";
      refs.pairingStatus.textContent = response.ok && actionToken ? "Paired for this runtime" : "Pairing refused";
      if (actionToken) await Promise.all([refreshActions(), refreshReceipts()]);
    } catch (_error) {
      refs.pairingStatus.textContent = "Pairing unavailable";
    } finally {
      refs.pairingSubmit.disabled = false;
    }
  }

  async function startGuidedSetup(guideId) {
    const prior = refs.docBody.querySelector(".guide-feedback");
    prior?.remove();
    if (!actionToken) {
      openGuide("pairing");
      const message = node("p", "guide-feedback is-error", "Pair this Atlas page first, then start the guided setup again.");
      refs.docBody.append(message);
      return;
    }
    document.querySelectorAll("[data-start-guide]").forEach((button) => { button.disabled = true; });
    try {
      const response = await fetch(`/guided-setups/${encodeURIComponent(guideId)}`, {
        method: "POST",
        headers: {"content-type": "application/json", "x-atlas-action-token": actionToken},
        body: "{}",
      });
      const payload = await response.json().catch(() => ({}));
      await refreshJobs();
      if (!response.ok || payload.ok !== true) {
        if (!refs.docDialog.open) openGuide(GUIDED_DOCS[guideId]);
        refs.docBody.append(node("p", "guide-feedback is-error",
          payload.status === "unavailable"
            ? "The subscription worker is not available yet. Its setup run was recorded in History; start the reviewed worker, then try again."
            : "Atlas could not start this guided setup."));
        return;
      }
      if (refs.docDialog.open) refs.docDialog.close();
      activateView("home");
      selectedJobId = payload.job_id;
      await refreshJobs();
    } catch (_error) {
      if (!refs.docDialog.open) openGuide("subscription");
      refs.docBody.append(node("p", "guide-feedback is-error", "The guided setup service is unavailable."));
    } finally {
      document.querySelectorAll("[data-start-guide]").forEach((button) => { button.disabled = false; });
    }
  }

  async function refreshState() {
    if (stateRequestActive) return;
    stateRequestActive = true;
    try {
      const response = await fetch("/state", {cache: "no-store"});
      if (!response.ok) throw new Error("state request failed");
      surfaceMode = response.headers.get("x-atlas-surface") || "voice";
      snapshot = await response.json();
      renderState(true);
      renderTranscript();
      renderHistory();
    } catch (_error) {
      renderState(false);
    } finally {
      stateRequestActive = false;
    }
  }

  async function refreshSignal() {
    if (signalRequestActive) return;
    signalRequestActive = true;
    try {
      const response = await fetch("/signal", {cache: "no-store"});
      if (!response.ok) throw new Error("signal request failed");
      const payload = await response.json();
      atlasVisual.setEnergy(payload && payload.energy);
    } catch (_error) {
      atlasVisual.setEnergy(0);
    } finally {
      signalRequestActive = false;
    }
  }

  async function refreshCapabilities() {
    try {
      const response = await fetch("/capabilities", {cache: "no-store"});
      if (!response.ok) throw new Error("capability request failed");
      const payload = await response.json();
      capabilities = Array.isArray(payload) ? payload : [];
    } catch (_error) {
      capabilities = [];
    }
    renderSources();
  }

  async function refreshActions() {
    if (!actionToken) {
      actions = [];
      renderWork();
      return;
    }
    try {
      const response = await fetch("/actions", {
        cache: "no-store", headers: {"x-atlas-action-token": actionToken},
      });
      if (response.status === 401) {
        clearPairing();
        return;
      }
      if (!response.ok) throw new Error("action request failed");
      const payload = await response.json();
      actions = (Array.isArray(payload) ? payload : Array.isArray(payload.actions) ? payload.actions : [])
        .filter((action) => action && typeof action === "object").slice(0, 50);
      refs.pairingStatus.textContent = "Paired for this runtime";
    } catch (_error) {
      actions = [];
    }
    renderWork();
  }

  async function refreshReceipts() {
    if (!actionToken) {
      receiptHistory = [];
      renderHistory();
      return;
    }
    try {
      const response = await fetch("/receipts", {
        cache: "no-store", headers: {"x-atlas-action-token": actionToken},
      });
      if (response.status === 401) {
        clearPairing();
        return;
      }
      if (!response.ok) throw new Error("receipt request failed");
      const payload = await response.json();
      receiptHistory = Array.isArray(payload.receipts) ? payload.receipts.slice(0, 100) : [];
    } catch (_error) {
      receiptHistory = [];
    }
    renderHistory();
  }

  async function refreshSelectedJobEvents() {
    if (!selectedJobId) return;
    try {
      const response = await fetch(`/jobs/${encodeURIComponent(selectedJobId)}/events`, {cache: "no-store"});
      if (!response.ok) throw new Error("event request failed");
      const payload = await response.json();
      jobEvents.set(selectedJobId, Array.isArray(payload.events) ? payload.events.slice(0, 100) : []);
    } catch (_error) {
      jobEvents.set(selectedJobId, []);
    }
    renderWork();
  }

  async function refreshJobs() {
    try {
      const response = await fetch("/jobs", { cache: "no-store" });
      if (!response.ok) throw new Error("job request failed");
      const payload = await response.json();
      jobs = Array.isArray(payload.jobs) ? payload.jobs.slice(0, 50) : [];
    } catch (_error) {
      jobs = [];
    }
    renderWork();
    renderHistory();
    await refreshSelectedJobEvents();
  }

  async function refreshHealth() {
    try {
      const response = await fetch("/health", {cache: "no-store"});
      if (!response.ok) throw new Error("health request failed");
      const payload = await response.json();
      workerHealth = payload && typeof payload === "object" ? payload : {status: "unavailable", reason: "health_invalid"};
    } catch (_error) {
      workerHealth = {status: "unavailable", reason: "health_unreachable"};
    }
    renderWorkerHealth();
  }

  function activateView(name) {
    const target = document.querySelector(`[data-view="${name}"]`);
    if (!target) return;
    document.querySelectorAll("[data-view]").forEach((view) => {
      const active = view === target;
      view.hidden = !active;
      view.classList.toggle("is-active", active);
    });
    document.querySelectorAll("[data-view-target]").forEach((button) => {
      button.classList.toggle("is-active", button.dataset.viewTarget === name);
    });
  }

  function openGuide(key) {
    const guide = GUIDES[key];
    if (!guide) return;
    refs.docTitle.textContent = guide.title;
    refs.docBody.replaceChildren();
    guide.sections.forEach(([heading, copy]) => {
      refs.docBody.append(node("h3", "", heading));
      refs.docBody.append(node("p", "", copy));
    });
    if (guide.paths.length) {
      refs.docBody.append(node("h3", "", "Governing files"));
      guide.paths.forEach((path) => refs.docBody.append(node("div", "path", path)));
    }
    if (guide.bullets.length) {
      refs.docBody.append(node("h3", "", "What to do"));
      const list = node("ul");
      guide.bullets.forEach((item) => list.append(node("li", "", item)));
      refs.docBody.append(list);
    }
    refs.docDialog.showModal();
  }

  document.querySelectorAll("[data-view-target]").forEach((button) => {
    button.addEventListener("click", () => activateView(button.dataset.viewTarget));
  });
  document.querySelectorAll("[data-jump]").forEach((button) => {
    button.addEventListener("click", () => activateView(button.dataset.jump));
  });
  document.querySelectorAll("[data-doc]").forEach((button) => {
    button.addEventListener("click", () => openGuide(button.dataset.doc));
  });
  document.querySelectorAll("[data-start-guide]").forEach((button) => {
    button.addEventListener("click", () => startGuidedSetup(button.dataset.startGuide));
  });
  refs.alertsButton.addEventListener("click", () => activateView(actions.length ? "home" : sourceIssueCount ? "sources" : "settings"));
  refs.pairingSubmit.addEventListener("click", pairUI);
  refs.docDialog.addEventListener("click", (event) => {
    if (event.target === refs.docDialog) refs.docDialog.close();
  });

  if (window.matchMedia) {
    const motion = window.matchMedia("(prefers-reduced-motion: reduce)");
    const renderMotion = () => {
      refs.motionStatus.textContent = motion.matches ? "Reduced motion" : "Standard motion";
    };
    renderMotion();
    motion.addEventListener?.("change", renderMotion);
  }

  renderSources();
  renderWorkerHealth();
  const bootstrap = new URLSearchParams(window.location.hash.slice(1)).get("pair");
  if (bootstrap) {
    refs.pairingToken.value = bootstrap;
    window.history.replaceState(null, "", `${window.location.pathname}${window.location.search}`);
    pairUI();
  }

  refreshState();
  refreshSignal();
  refreshCapabilities();
  refreshJobs();
  refreshHealth();
  window.setInterval(refreshState, 1000);
  window.setInterval(refreshSignal, 80);
  window.setInterval(refreshActions, 1000);
  window.setInterval(refreshJobs, 1000);
  window.setInterval(refreshReceipts, 5000);
  window.setInterval(refreshHealth, 3000);
  window.setInterval(refreshCapabilities, 10000);
})();
