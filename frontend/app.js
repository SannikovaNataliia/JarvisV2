// No app state here beyond transient UI mechanics (reconnect backoff, which
// history card is still streaming, console scroll/filter). The conversation
// and connection truth always comes from the backend via `ready`/`state`/
// `transcript`/`log` messages — a page refresh must never lose anything,
// because `ready` rebuilds the whole view from scratch.

const LEVELS = ["debug", "info", "warn", "error"];

const ui = {
  ws: null,
  reconnectDelay: 500,
  maxReconnectDelay: 8000,
  consolePaused: false,
  consoleLevel: "info",
  lastCard: null, // { el, role, final }
};

function $(sel) {
  return document.querySelector(sel);
}

function connect() {
  const url = `ws://${location.host}/ws`;
  const ws = new WebSocket(url);
  ui.ws = ws;

  ws.addEventListener("open", () => {
    ui.reconnectDelay = 500;
    setConnectionIndicator(true);
  });

  ws.addEventListener("close", () => {
    setConnectionIndicator(false);
    scheduleReconnect();
  });

  ws.addEventListener("error", () => {
    ws.close();
  });

  ws.addEventListener("message", (event) => {
    let msg;
    try {
      msg = JSON.parse(event.data);
    } catch (e) {
      return;
    }
    handleMessage(msg);
  });
}

function scheduleReconnect() {
  setStatusText(`reconnecting in ${Math.round(ui.reconnectDelay / 1000)}s...`);
  setTimeout(connect, ui.reconnectDelay);
  ui.reconnectDelay = Math.min(ui.reconnectDelay * 2, ui.maxReconnectDelay);
}

function handleMessage(msg) {
  switch (msg.type) {
    case "ready":
      renderReady(msg.data);
      break;
    case "state":
      renderState(msg.data);
      break;
    case "transcript":
      renderTranscript(msg.data);
      break;
    case "log":
      renderLog(msg.data);
      break;
    case "error":
      renderLog({ level: "error", source: "backend", text: msg.data.message });
      break;
  }
}

function renderReady(data) {
  $("#history").innerHTML = "";
  ui.lastCard = null;
  for (const turn of data.history) {
    appendCard(turn.role, turn.text, true);
  }
  renderState({ state: data.state, mode: data.mode, model: data.model });
}

function renderState(data) {
  $("#status-state").textContent = data.state;
  $("#status-mode").textContent = data.mode;
  $("#mode-text").classList.toggle("active", data.mode === "text");
  $("#mode-voice").classList.toggle("active", data.mode === "voice");
}

function renderTranscript(data) {
  const { role, text, final } = data;
  if (ui.lastCard && ui.lastCard.role === role && !ui.lastCard.final) {
    ui.lastCard.el.querySelector(".card-text").textContent += text;
    ui.lastCard.final = final;
    scrollHistoryToBottom();
  } else {
    appendCard(role, text, final);
  }
}

function appendCard(role, text, final) {
  const history = $("#history");
  const card = document.createElement("div");
  card.className = `card card-${role}`;

  const label = document.createElement("div");
  label.className = "card-label";
  label.textContent = role === "user" ? "You" : "Jarvis";

  const body = document.createElement("div");
  body.className = "card-text";
  body.textContent = text;

  card.appendChild(label);
  card.appendChild(body);
  history.appendChild(card);
  scrollHistoryToBottom();

  ui.lastCard = { el: card, role, final };
}

function scrollHistoryToBottom() {
  const history = $("#history");
  history.scrollTop = history.scrollHeight;
}

function renderLog(data) {
  const panel = $("#console-lines");
  const wasNearBottom = !isScrolledUp(panel);

  const line = document.createElement("div");
  line.className = "log-line";
  line.dataset.level = data.level;
  line.textContent = `[${data.level.toUpperCase()}] ${data.source}: ${data.text}`;
  applyConsoleFilter(line);
  panel.appendChild(line);

  if (!ui.consolePaused && wasNearBottom) {
    panel.scrollTop = panel.scrollHeight;
  }
}

function applyConsoleFilter(line) {
  const minIndex = LEVELS.indexOf(ui.consoleLevel);
  const lineIndex = LEVELS.indexOf(line.dataset.level);
  line.style.display = lineIndex >= minIndex ? "" : "none";
}

function isScrolledUp(panel) {
  return panel.scrollTop + panel.clientHeight < panel.scrollHeight - 20;
}

function setConnectionIndicator(connected) {
  const el = $("#status-conn");
  el.textContent = connected ? "connected" : "disconnected";
  el.classList.toggle("connected", connected);
  el.classList.toggle("disconnected", !connected);
}

function setStatusText(text) {
  $("#status-conn").textContent = text;
}

function sendText(text) {
  if (!text.trim() || !ui.ws || ui.ws.readyState !== WebSocket.OPEN) return;
  ui.ws.send(
    JSON.stringify({
      type: "send_text",
      id: crypto.randomUUID(),
      ts: Date.now() / 1000,
      data: { text },
    })
  );
}

function setMode(mode) {
  if (!ui.ws || ui.ws.readyState !== WebSocket.OPEN) return;
  ui.ws.send(
    JSON.stringify({
      type: "set_mode",
      id: crypto.randomUUID(),
      ts: Date.now() / 1000,
      data: { mode },
    })
  );
}

function initInput() {
  const input = $("#input-field");
  const sendBtn = $("#send-btn");

  const submit = () => {
    sendText(input.value);
    input.value = "";
  };

  sendBtn.addEventListener("click", submit);
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      submit();
    }
  });
}

function initConsole() {
  $("#console-toggle").addEventListener("click", () => {
    $("#console-panel").classList.toggle("collapsed");
  });

  $("#console-pause").addEventListener("click", (e) => {
    ui.consolePaused = !ui.consolePaused;
    e.target.textContent = ui.consolePaused ? "Resume" : "Pause";
  });

  $("#console-level").addEventListener("change", (e) => {
    ui.consoleLevel = e.target.value;
    $("#console-lines")
      .querySelectorAll(".log-line")
      .forEach(applyConsoleFilter);
  });
}

function initModeSwitch() {
  $("#mode-text").addEventListener("click", () => setMode("text"));
  $("#mode-voice").addEventListener("click", () => setMode("voice"));
}

window.addEventListener("DOMContentLoaded", () => {
  initInput();
  initConsole();
  initModeSwitch();
  connect();
});
