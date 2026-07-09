const POLL_MS = 1500;

const floor = document.getElementById("office-floor");
const eventLog = document.getElementById("event-log");
const summary = document.getElementById("office-summary");
const connection = document.getElementById("connection-text");
const eventCount = document.getElementById("event-count");

function node(tag, className, text) {
  const element = document.createElement(tag);
  if (className) element.className = className;
  if (text !== undefined) element.textContent = text;
  return element;
}

function titleCase(value) {
  return String(value || "idle").replace(/\b\w/g, (letter) => letter.toUpperCase());
}

function formatTime(timestamp) {
  const time = new Date(timestamp);
  return Number.isNaN(time.getTime()) ? "now" : time.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
}

function renderAgents(agents) {
  const entries = Object.entries(agents || {});
  floor.replaceChildren();
  if (!entries.length) {
    const card = node("div", "waiting-card");
    const lamp = node("span", "waiting-lamp");
    lamp.setAttribute("aria-hidden", "true");
    card.append(lamp, node("strong", "Office doors are closed"), node("p", "Start group_bot.py, then this room will fill with your enabled agents."));
    floor.append(card);
    summary.textContent = "Waiting for the group bot to open the office.";
    return;
  }

  const active = entries.filter(([, agent]) => agent.status !== "idle").length;
  summary.textContent = `${entries.length} teammate${entries.length === 1 ? "" : "s"} at their desks · ${active || "no"} active right now`;
  for (const [key, agent] of entries) {
    const desk = node("article", "desk");
    desk.dataset.key = key;
    const header = node("div", "agent-top");
    const identity = node("div");
    identity.append(node("span", "agent-name", agent.name || key), node("span", "agent-role", agent.role || "Team member"));
    const status = node("span", `status status-${agent.status || "idle"}`);
    status.append(node("i", "status-dot"), document.createTextNode(titleCase(agent.status)));
    header.append(identity, status);

    const avatar = node("div", `avatar avatar-${agent.status || "idle"}`);
    avatar.setAttribute("aria-hidden", "true");
    avatar.append(node("span"));
    desk.append(header, avatar);
    if (agent.message) desk.append(node("div", "bubble", agent.message));
    floor.append(desk);
  }
}

function renderEvents(events) {
  eventLog.replaceChildren();
  const rows = events || [];
  eventCount.textContent = String(rows.length);
  if (!rows.length) {
    eventLog.append(node("li", "empty-event", "New group activity will appear here."));
    return;
  }
  for (const event of rows) {
    const item = node("li", `event event-${event.kind || "activity"}`);
    const meta = node("div", "event-meta");
    meta.append(node("span", "", event.agent_key ? titleCase(event.agent_key) : "Telegram"), node("time", "", formatTime(event.timestamp)));
    item.append(meta, node("div", "event-text", event.text || "Activity recorded."));
    eventLog.append(item);
  }
}

async function refresh() {
  try {
    const response = await fetch("/api/office-state", { cache: "no-store" });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const state = await response.json();
    renderAgents(state.agents);
    renderEvents(state.events);
    connection.textContent = "Office connected";
  } catch (error) {
    connection.textContent = "Reconnecting…";
  }
}

refresh();
setInterval(refresh, POLL_MS);
