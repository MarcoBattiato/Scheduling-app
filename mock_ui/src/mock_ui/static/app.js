// Every tab polls the one server, so several roles can be open at once and
// see each other's actions. Polling rather than websockets: a second of
// staleness is irrelevant for something driven by hand.

const params = new URLSearchParams(location.search);
let me = params.get("as") || "provider";
let state = null;
let pendingGrid = null;          // weekday -> Set(slot index), while editing

const DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
const START_HOUR = 8, END_HOUR = 20, STEP = 30;
const SLOTS = ((END_HOUR - START_HOUR) * 60) / STEP;

const $ = (id) => document.getElementById(id);
const isProvider = () => me === "provider";

async function api(path, body) {
  const res = await fetch(path, body ? {
    method: "POST", headers: {"Content-Type": "application/json"},
    body: JSON.stringify(body),
  } : {method: "POST"});
  if (!res.ok) alert((await res.json()).detail || "request failed");
  return res.json().catch(() => ({}));
}

async function refresh() {
  state = await (await fetch("/api/state")).json();
  render();
}

// -- rendering ------------------------------------------------------

function render() {
  if (!state) return;
  const known = ["provider", ...state.clients.map((c) => c.id)];
  if (!known.includes(me)) me = "provider";

  $("who").textContent = isProvider()
    ? "Provider"
    : (state.clients.find((c) => c.id === me)?.name || me);

  const sel = $("role");
  if (sel.options.length !== known.length) {
    sel.innerHTML = known
      .map((id) => `<option value="${id}">${id}</option>`).join("");
  }
  sel.value = me;

  const s = state.settings;
  $("settings").textContent =
    `alpha ${s.alpha} · max moves ${s.max_displacements} · bounds -${s.bounds.earlier}/+${s.bounds.later}d`;

  renderAlerts();
  renderSchedule();
  renderGrid();
  renderRequests();
  $("log").innerHTML = state.log.slice().reverse()
    .map((l) => `<div class="log-line">${escapeHtml(l)}</div>`).join("");

  $("avail-for").textContent = isProvider() ? "(provider)" : `(${me})`;
  $("panel-request").style.display = isProvider() ? "none" : "";
}

function renderAlerts() {
  const mine = state.approvals.filter(
    (a) => a.status === "pending" && (isProvider() || a.client_id === me));
  if (!mine.length) { $("alerts").innerHTML = ""; return; }

  $("alerts").innerHTML = mine.map((a) => {
    const body = isProvider()
      ? `Waiting on <b>${a.client_id}</b> to agree to move
         ${fmt(a.was)} → ${fmt(a.now)}.`
      : `The clinic asks to move your appointment from <b>${fmt(a.was)}</b>
         to <b>${fmt(a.now)}</b>.`;
    const buttons = isProvider() ? "" : `
      <div class="row">
        <button onclick="respond(${a.id}, true)">Accept</button>
        <button class="ghost" onclick="respond(${a.id}, false)">Decline</button>
      </div>`;
    return `<div class="alert"><div>${body}</div>${buttons}</div>`;
  }).join("");
}

function renderSchedule() {
  const live = state.appointments.filter((a) => a.status === "booked");
  const shown = isProvider() ? live : live.filter((a) => a.client_id === me);

  const byDay = {};
  for (const a of shown) {
    const day = a.start.slice(0, 10);
    (byDay[day] = byDay[day] || []).push(a);
  }

  const days = Object.keys(byDay).sort();
  if (!days.length) {
    $("schedule").innerHTML = `<p class="hint">Nothing booked yet.</p>`;
  } else {
    $("schedule").innerHTML = days.map((day) => `
      <div class="day">
        <h3>${new Date(day + "T00:00").toDateString()}</h3>
        ${byDay[day].sort((x, y) => x.start < y.start ? -1 : 1).map((a) => `
          <div class="appt${a.origin === "displaced" ? " displaced" : ""}">
            <span class="time">${a.start.slice(11, 16)}–${a.end.slice(11, 16)}</span>
            <span class="name">${a.client_id}</span>
            ${a.origin === "displaced" ? `<span class="tag">moved by clinic</span>` : ""}
            ${a.locked ? `<span class="tag">locked</span>` : ""}
            ${(isProvider() || a.client_id === me)
              ? `<button class="link" onclick="cancelAppt(${a.id})">cancel</button>` : ""}
          </div>`).join("")}
      </div>`).join("");
  }

  if (isProvider()) renderHistory();
}

function renderHistory() {
  const past = state.appointments.filter((a) => a.status !== "booked");
  if (!past.length) return;
  $("schedule").innerHTML += `
    <div class="day history">
      <h3>History <span class="hint">(what anchoring will learn from)</span></h3>
      ${past.map((a) => `
        <div class="appt muted">
          <span class="time">${a.start.slice(5, 16).replace("T", " ")}</span>
          <span class="name">${a.client_id}</span>
          <span class="tag">${a.status}</span>
          <span class="tag ${a.origin}">${a.origin}</span>
        </div>`).join("")}
    </div>`;
}

function renderGrid() {
  const weekly = state.weekly[isProvider() ? "provider-self" : me] || [];
  if (!pendingGrid) {
    pendingGrid = {};
    for (let d = 0; d < 7; d++) pendingGrid[d] = new Set();
    for (const r of weekly) {
      for (let i = slotIndex(r.from); i < slotIndex(r.to); i++) {
        if (i >= 0 && i < SLOTS) pendingGrid[r.weekday].add(i);
      }
    }
  }

  let html = `<table class="grid"><tr><th></th>`;
  for (const d of DAYS) html += `<th>${d}</th>`;
  html += `</tr>`;
  for (let i = 0; i < SLOTS; i++) {
    const label = i % 2 === 0 ? slotLabel(i) : "";
    html += `<tr><td class="hour">${label}</td>`;
    for (let d = 0; d < 7; d++) {
      const on = pendingGrid[d].has(i);
      html += `<td class="cell${on ? " on" : ""}" data-d="${d}" data-i="${i}"></td>`;
    }
    html += `</tr>`;
  }
  $("grid").innerHTML = html + `</table>`;
}

function renderRequests() {
  const mine = state.requests.filter((r) => isProvider() || r.client_id === me);
  const open = mine.filter((r) => r.status === "pending");
  $("requests").innerHTML = !mine.length ? "" : `
    <div class="reqs">${mine.slice(-6).reverse().map((r) => `
      <div class="req ${r.status}">
        <span>${r.duration}m ${isProvider() ? "· " + r.client_id : ""}</span>
        <span class="tag">${r.status}</span>
        ${r.status === "pending"
          ? `<button class="link" onclick="withdraw(${r.id})">withdraw</button>` : ""}
      </div>`).join("")}</div>
    ${open.length ? `<p class="hint">${open.length} still unplaced — the
       scheduler will try again when something frees up.</p>` : ""}`;
}

// -- actions --------------------------------------------------------

window.respond = async (id, accept) => { await api(`/api/approvals/${id}`, {accept}); refresh(); };
window.cancelAppt = async (id) => {
  if (confirm("Cancel this appointment?")) { await api(`/api/appointments/${id}/cancel`); refresh(); }
};
window.withdraw = async (id) => { await api(`/api/requests/${id}/withdraw`); refresh(); };

$("role").onchange = (e) => {
  me = e.target.value;
  pendingGrid = null;
  history.replaceState({}, "", `?as=${me}`);
  render();
};
$("solve").onclick = async () => { await api("/api/solve"); refresh(); };
$("save").onclick = async () => { const r = await api("/api/snapshot/save"); alert("Saved " + r.saved); };
$("reset").onclick = async () => {
  if (confirm("Throw away this session and start again?")) {
    await api("/api/reset"); pendingGrid = null; refresh();
  }
};

$("save-avail").onclick = async () => {
  const ranges = [];
  for (let d = 0; d < 7; d++) {
    const slots = [...pendingGrid[d]].sort((a, b) => a - b);
    let run = null;
    for (const i of slots) {
      if (run && i === run.end) { run.end = i + 1; continue; }
      if (run) ranges.push(runToRange(d, run));
      run = {start: i, end: i + 1};
    }
    if (run) ranges.push(runToRange(d, run));
  }
  await api("/api/availability", {
    client_id: isProvider() ? "provider-self" : me, ranges,
  });
  pendingGrid = null;
  refresh();
};

$("request-form").onsubmit = async (e) => {
  e.preventDefault();
  const f = new FormData(e.target);
  await api("/api/requests", {
    client_id: me,
    duration_minutes: Number(f.get("duration")),
    windows: [{from: f.get("from"), to: f.get("to")}],
  });
  refresh();
};

// Drag-select on the availability grid.
let painting = null;
document.addEventListener("mousedown", (e) => {
  const cell = e.target.closest(".cell");
  if (!cell) return;
  const d = +cell.dataset.d, i = +cell.dataset.i;
  painting = pendingGrid[d].has(i) ? "off" : "on";
  paint(cell);
  e.preventDefault();
});
document.addEventListener("mouseover", (e) => {
  if (painting) { const c = e.target.closest(".cell"); if (c) paint(c); }
});
document.addEventListener("mouseup", () => { painting = null; });

function paint(cell) {
  const d = +cell.dataset.d, i = +cell.dataset.i;
  if (painting === "on") { pendingGrid[d].add(i); cell.classList.add("on"); }
  else { pendingGrid[d].delete(i); cell.classList.remove("on"); }
}

// -- helpers --------------------------------------------------------

const pad = (n) => String(n).padStart(2, "0");
const slotIndex = (hhmm) => {
  const [h, m] = hhmm.split(":").map(Number);
  return ((h - START_HOUR) * 60 + m) / STEP;
};
const slotLabel = (i) => {
  const mins = START_HOUR * 60 + i * STEP;
  return `${pad(Math.floor(mins / 60))}:${pad(mins % 60)}`;
};
const runToRange = (d, run) => ({weekday: d, from: slotLabel(run.start), to: slotLabel(run.end)});
const fmt = (iso) => new Date(iso).toLocaleString([], {
  weekday: "short", day: "numeric", month: "short", hour: "2-digit", minute: "2-digit",
});
const escapeHtml = (s) => s.replace(/[&<>]/g, (c) => ({"&": "&amp;", "<": "&lt;", ">": "&gt;"}[c]));

refresh();
setInterval(refresh, 2500);
