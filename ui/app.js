(() => {
  "use strict";

  const STATES = new Set(["ASLEEP", "LISTENING", "THINKING", "SPEAKING"]);
  const ACTIVE_JOB_STATES = new Set(["queued", "launching", "running"]);
  const CONFIG_PATHS = [
    "config/atlas.yaml",
    "config/apps.yaml",
    "config/mcp.yaml",
    "config/intents.yaml",
    "config/persona.md",
  ];
  const CAPTIONS = {
    ASLEEP: "wake word ready",
    LISTENING: "listening",
    THINKING: "thinking",
    SPEAKING: "speaking",
    OFFLINE: "waiting for Atlas",
  };

  const refs = {
    connection: document.querySelector("#connection"),
    engine: document.querySelector("#engine"),
    stateLabel: document.querySelector("#state-label"),
    stateCaption: document.querySelector("#state-caption"),
    transcript: document.querySelector("#transcript"),
    workerSummary: document.querySelector("#worker-summary"),
    workerTabs: document.querySelector("#worker-tabs"),
    workerOutput: document.querySelector("#worker-output"),
    history: document.querySelector("#history-list"),
    voiceStatus: document.querySelector("#voice-status"),
    outputStatus: document.querySelector("#output-status"),
    claudeStatus: document.querySelector("#claude-status"),
    mcpList: document.querySelector("#mcp-list"),
    configList: document.querySelector("#config-list"),
    pairingStatus: document.querySelector("#pairing-status"),
  };

  let actionToken = "";
  let jobs = [];
  let selectedJobId = "";
  const eventsByJob = new Map();
  const resultsByJob = new Map();

  function node(tag, className, text) {
    const element = document.createElement(tag);
    if (className) element.className = className;
    if (text !== undefined) element.textContent = text;
    return element;
  }

  function selectView(name) {
    document.querySelectorAll("[data-view]").forEach((view) => {
      const active = view.dataset.view === name;
      view.hidden = !active;
      view.classList.toggle("is-active", active);
    });
    document.querySelectorAll("[data-view-target]").forEach((button) => {
      button.classList.toggle("is-active", button.dataset.viewTarget === name);
    });
  }

  document.querySelectorAll("[data-view-target]").forEach((button) => {
    button.addEventListener("click", () => selectView(button.dataset.viewTarget));
  });

  function setConnection(online) {
    refs.connection.classList.toggle("is-online", online);
    refs.connection.classList.toggle("is-offline", !online);
    refs.connection.lastElementChild.textContent = online ? "connected" : "offline";
  }

  function setEngine(rawState) {
    const current = STATES.has(rawState) ? rawState : "OFFLINE";
    refs.engine.className = `engine engine-${current.toLowerCase()}`;
    refs.stateLabel.textContent = current;
    refs.stateCaption.textContent = CAPTIONS[current];
  }

  function renderTranscript(lines) {
    refs.transcript.replaceChildren();
    if (!Array.isArray(lines) || lines.length === 0) {
      refs.transcript.append(node("p", "empty", "No transcript yet."));
      return;
    }
    lines.forEach((line) => {
      if (!line || typeof line.text !== "string" || typeof line.role !== "string") return;
      const row = node("div", `transcript-line is-${line.role}`);
      row.append(node("span", "transcript-role", line.role));
      row.append(node("span", "transcript-text", line.text));
      if (typeof line.t === "string") {
        const stamp = new Date(line.t);
        row.append(node("time", "transcript-time", Number.isNaN(stamp.valueOf()) ? "" : stamp.toLocaleTimeString()));
      }
      refs.transcript.append(row);
    });
    refs.transcript.scrollTop = refs.transcript.scrollHeight;
  }

  function renderVoice(payload) {
    refs.voiceStatus.textContent = typeof payload.voice === "string" ? payload.voice : "Default voice";
    const output = payload.output_device;
    if (!output || typeof output !== "object") {
      refs.outputStatus.textContent = "Output follows the worker configuration.";
      return;
    }
    const resolved = typeof output.resolved === "string" ? output.resolved : "system default";
    refs.outputStatus.textContent = output.following ? `Following ${resolved}.` : `Output: ${resolved}.`;
  }

  async function refreshState() {
    try {
      const response = await fetch("/state", {cache: "no-store"});
      if (!response.ok) throw new Error("state unavailable");
      const payload = await response.json();
      setConnection(true);
      setEngine(payload.state);
      renderTranscript(payload.transcript);
      renderVoice(payload);
    } catch (_error) {
      setConnection(false);
      setEngine("OFFLINE");
    }
  }

  function eventStore(jobId) {
    if (!eventsByJob.has(jobId)) eventsByJob.set(jobId, []);
    return eventsByJob.get(jobId);
  }

  async function refreshEvents(job) {
    const existing = eventStore(job.id);
    const lastSequence = existing.length ? existing[existing.length - 1].sequence : 0;
    try {
      const response = await fetch(`/jobs/${encodeURIComponent(job.id)}/events?after=${lastSequence}`, {cache: "no-store"});
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
      cancel.addEventListener("click", () => cancelJob(job.id));
      header.append(cancel);
    }
    refs.workerOutput.append(header);
    const terminal = node("div", "terminal");
    const events = eventStore(job.id);
    if (events.length === 0) terminal.append(node("p", "quiet", "Waiting for output."));
    events.forEach((event) => {
      const row = node("div", `terminal-line is-${event.kind}`);
      row.append(node("span", "terminal-sequence", String(event.sequence).padStart(4, "0")));
      row.append(node("span", "terminal-kind", event.kind));
      row.append(node("span", "terminal-text", event.text));
      terminal.append(row);
    });
    refs.workerOutput.append(terminal);
  }

  function renderWorkers() {
    const active = jobs.filter((job) => ACTIVE_JOB_STATES.has(job.status));
    refs.workerSummary.textContent = active.length ? `${active.length} active` : "idle";
    refs.workerTabs.replaceChildren();
    if (!active.some((job) => job.id === selectedJobId)) selectedJobId = active[0]?.id || "";
    active.forEach((job) => {
      const button = node("button", "worker-tab", job.title);
      button.type = "button";
      button.role = "tab";
      button.classList.toggle("is-active", job.id === selectedJobId);
      button.addEventListener("click", () => {
        selectedJobId = job.id;
        renderWorkers();
      });
      refs.workerTabs.append(button);
    });
    renderWorker(active.find((job) => job.id === selectedJobId));
  }

  async function showResult(jobId, host) {
    if (!actionToken) return;
    if (!resultsByJob.has(jobId)) {
      try {
        const response = await fetch(`/jobs/${encodeURIComponent(jobId)}/result`, {
          cache: "no-store",
          headers: {"x-atlas-action-token": actionToken},
        });
        if (!response.ok) throw new Error("result unavailable");
        const payload = await response.json();
        resultsByJob.set(jobId, payload.result);
      } catch (_error) {
        resultsByJob.set(jobId, "The result is unavailable.");
      }
    }
    let output = host.querySelector(".history-result");
    if (!output) {
      output = node("pre", "history-result");
      host.append(output);
    }
    output.textContent = resultsByJob.get(jobId);
  }

  function renderHistory() {
    refs.history.replaceChildren();
    const terminal = jobs.filter((job) => !ACTIVE_JOB_STATES.has(job.status));
    if (terminal.length === 0) {
      refs.history.append(node("p", "empty", "No completed work yet."));
      return;
    }
    terminal.forEach((job) => {
      const card = node("article", "history-item");
      const heading = node("div", "history-heading");
      heading.append(node("strong", "", job.title));
      heading.append(node("span", `job-state is-${job.status}`, job.status));
      card.append(heading);
      card.append(node("p", "history-summary", job.summary || job.error || "No summary available."));
      if (job.status === "succeeded") {
        const button = node("button", "small-button", actionToken ? "Open result" : "Pair to open result");
        button.type = "button";
        button.disabled = !actionToken;
        button.addEventListener("click", () => showResult(job.id, card));
        card.append(button);
      }
      refs.history.append(card);
    });
  }

  async function cancelJob(jobId) {
    if (!actionToken) return;
    try {
      await fetch(`/jobs/${encodeURIComponent(jobId)}/cancel`, {
        method: "POST",
        headers: {
          "content-type": "application/json",
          "x-atlas-action-token": actionToken,
        },
        body: "{}",
      });
      await refreshJobs();
    } catch (_error) {
      return;
    }
  }

  async function refreshJobs() {
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
    }
  }

  function renderMcp(servers) {
    refs.mcpList.replaceChildren();
    if (!Array.isArray(servers) || servers.length === 0) {
      refs.mcpList.append(node("p", "empty", "No MCP servers configured."));
      return;
    }
    servers.forEach((server) => {
      const row = node("div", "mcp-row");
      row.append(node("strong", "", server.name));
      const detail = server.connected ? `${server.tools} tools connected` : (server.error || "disconnected");
      row.append(node("span", server.connected ? "is-good" : "is-bad", detail));
      refs.mcpList.append(row);
    });
  }

  async function refreshSettings() {
    try {
      const [mcpResponse, healthResponse] = await Promise.all([
        fetch("/mcp", {cache: "no-store"}),
        fetch("/health", {cache: "no-store"}),
      ]);
      if (mcpResponse.ok) renderMcp((await mcpResponse.json()).servers);
      if (healthResponse.ok) {
        const health = await healthResponse.json();
        refs.claudeStatus.textContent = health.claude ? "Available" : "Unavailable";
      }
    } catch (_error) {
      refs.claudeStatus.textContent = "Unavailable";
    }
  }

  async function pairFromFragment() {
    const fragment = new URLSearchParams(window.location.hash.slice(1));
    const token = fragment.get("pair");
    if (!token) return;
    history.replaceState(null, "", window.location.pathname + window.location.search);
    try {
      const response = await fetch("/pair", {
        method: "POST",
        headers: {"content-type": "application/json"},
        body: JSON.stringify({token}),
      });
      if (!response.ok) throw new Error("pairing failed");
      actionToken = (await response.json()).action_token || "";
      refs.pairingStatus.textContent = actionToken ? "Paired" : "Not paired";
      renderWorkers();
      renderHistory();
    } catch (_error) {
      refs.pairingStatus.textContent = "Pairing failed";
    }
  }

  CONFIG_PATHS.forEach((path) => refs.configList.append(node("li", "", path)));
  pairFromFragment();
  refreshState();
  refreshJobs();
  refreshSettings();
  setInterval(refreshState, 500);
  setInterval(refreshJobs, 1000);
  setInterval(refreshSettings, 5000);
})();
