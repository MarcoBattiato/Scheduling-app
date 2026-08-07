// Every tab polls the one server, so several roles can be open at once and
// see each other's actions. Polling rather than websockets: a second of
// staleness is irrelevant for something driven by hand.

const params = new URLSearchParams(location.search);
let me = params.get("as") || "provider";
let state = null;
let pendingGrid = null;          // weekday -> Set(slot index), while editing
let picked = {};                 // plan item key -> ticked, survives the poll

const DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
const START_HOUR = 8, END_HOUR = 20, STEP = 30;
const SLOTS = ((END_HOUR - START_HOUR) * 60) / STEP;

const $ = (id) => document.getElementById(id);
const isProvider = () => me === "provider";
const whose = () => (isProvider() ? "provider-self" : me);

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
    ? "Provider" : (state.clients.find((c) => c.id === me)?.name || me);

  const sel = $("role");
  if (sel.options.length !== known.length) {
    sel.innerHTML = known.map((id) => `<option value="${id}">${id}</option>`).join("");
  }
  sel.value = me;

  const s = state.settings, sch = state.scheduler || {};
  $("settings").textContent =
    `alpha ${s.alpha} · max moves ${s.max_displacements} · `
    + (sch.auto_run ? `auto (urgent <${sch.urgency_hours}h)` : "manual only");

  renderProposal();
  renderAlerts();
  renderSchedule();
  renderGrid();
  renderRequests();
  renderCatalogue();
  $("log").innerHTML = state.log.slice().reverse()
    .map((l) => `<div class="log-line">${esc(l)}</div>`).join("");

  $("avail-for").textContent = isProvider() ? "(provider)" : `(${me})`;
  $("panel-request").style.display = isProvider() ? "none" : "";
  $("panel-catalogue").style.display = isProvider() ? "" : "none";
  $("try").style.display = isProvider() ? "" : "none";
  $("solve").style.display = isProvider() ? "" : "none";
}

// -- the schedule calendar -------------------------------------------

function renderSchedule() {
  const mine = (a) => isProvider() || a.client_id === me;
  const live = state.appointments.filter((a) => a.status === "booked" && mine(a));

  renderCalendar($("calendar"), {
    weeks: Number($("weeks").value),
    start: new Date(state.today + "T00:00"),
    availability: (state.availability[whose()] || []),
    blocks: live.map((a) => ({
      id: `a${a.id}`,
      start: a.start, end: a.end,
      label: isProvider() ? a.client_id : a.service,
      sub: `${a.start.slice(11, 16)}–${a.end.slice(11, 16)}`,
      cls: [a.origin === "displaced" ? "moved" : "",
            new Date(a.end) < new Date() ? "past" : ""].join(" "),
      data: hoverData(a),
    })),
    onSelect: (date, from, to, available) => setException(date, from, to, available),
  });
}

function setException(date, from, to, available) {
  // Drag adds availability on that date; shift-drag takes it away. Either way
  // it is a single-date exception, not a change to the weekly pattern.
  api("/api/exceptions", {
    client_id: whose(), date, from_time: from, to_time: to, available,
  }).then(refresh);
}

// -- proposals, each on their own calendar ---------------------------

function renderProposal() {
  const drafts = (state.plans || []).filter((p) => p.status === "draft");
  if (!isProvider() || !drafts.length) { $("proposal").innerHTML = ""; return; }

  const inFlight = (state.plans || []).some((p) => p.status === "awaiting_clients");
  $("proposal").innerHTML = `
    <div class="alert proposal">
      <div><b>${drafts.length === 1 ? "The scheduler has a proposal"
                                    : `${drafts.length} arrangements to compare`}</b>
        <span class="hint">Nothing is booked yet. Approving asks each client
          to confirm their own part.</span></div>
      ${inFlight ? `<p class="hint">Something is already out with the clients.
         Those slots are held, so this proposal plans around them.</p>` : ""}
      <div class="drafts">${drafts.map(draftShell).join("")}</div>
    </div>`;

  drafts.forEach(drawDraft);
  for (const [key, on] of Object.entries(picked)) {
    const box = document.querySelector(`input[data-key="${CSS.escape(key)}"]`);
    if (box) box.checked = on;
  }
  drafts.forEach((p) => applyDeps(p.id));
}

function draftShell(p) {
  const m = p.metrics || {}, q = p.params || {};
  const item = (x, kind) => `
    <label class="item">
      <input type="checkbox" checked data-plan="${p.id}" data-key="${x.key}"
             data-needs="${(x.depends_on || []).join(",")}" onchange="tick(this)">
      <span class="tag ${kind === "book" ? "ok" : "move"}">${kind}</span>
      <span>${esc(x.client_id)}</span>
      <span class="time">${kind === "book" ? fmt(x.start)
                                           : `${fmt(x.was)} → ${fmt(x.now)}`}</span>
    </label>`;

  return `
    <div class="draft" id="draft-${p.id}">
      <h3>alpha ${q.alpha} · max moves ${q.max_displacements}</h3>
      <div class="metrics">
        <span class="tag">${m.placed} booked</span>
        ${m.unplaced ? `<span class="tag">${m.unplaced} unplaced</span>` : ""}
        ${m.displacements ? `<span class="tag move">${m.displacements} moved</span>` : ""}
        <span class="tag">waste ${m.fragmentation_minutes}m</span>
        <span class="tag">delay ${m.earliness_minutes}m</span>
      </div>
      <div class="draft-cal" id="draftcal-${p.id}"></div>
      ${p.placements.map((x) => item(x, "book")).join("")}
      ${p.displacements.map((x) => item(x, "move")).join("")}
      <div class="row" style="margin-top:10px">
        <button onclick="approvePlan(${p.id})">Approve selected</button>
        <button class="ghost" onclick="lockAndRerun(${p.id})">Lock selected, re-plan rest</button>
        <button class="ghost" onclick="discardPlan(${p.id})">Discard</button>
      </div>
    </div>`;
}

function drawDraft(p) {
  const el = $(`draftcal-${p.id}`);
  if (!el) return;
  const moving = new Set(p.displacements.map((d) => d.appointment_id));

  // What stays put, shown faintly, so the proposal reads against the calendar
  // it would actually land in rather than floating free.
  const staying = state.appointments
    .filter((a) => a.status === "booked" && !moving.has(a.id))
    .map((a) => ({id: `d${p.id}-a${a.id}`, start: a.start, end: a.end,
                  label: a.client_id, cls: "existing", data: hoverData(a)}));

  const key = (k) => k.replace(":", "-");
  const arriving = p.placements.map((x) => ({
    id: `d${p.id}-${key(x.key)}`,
    start: x.start, end: x.end, label: x.client_id, sub: "new",
    cls: "proposed", data: {Client: x.client_id, Service: x.service,
                            What: "proposed booking", Request: `#${x.request_id}`},
  }));
  const landing = p.displacements.map((d) => ({
    id: `d${p.id}-${key(d.key)}-to`,
    start: d.now, end: d.now_end, label: d.client_id, sub: "moved to",
    cls: "proposed moved", data: {Client: d.client_id,
                                  What: "would be moved here", From: fmt(d.was)},
  }));
  const leaving = p.displacements.map((d) => ({
    id: `d${p.id}-${key(d.key)}-from`,
    start: d.was, end: d.was_end, label: d.client_id, cls: "vacating",
  }));

  renderCalendar(el, {
    weeks: Number($("weeks").value),
    start: new Date(state.today + "T00:00"),
    availability: state.availability["provider-self"] || [],
    blocks: [...staying, ...arriving, ...landing],
    ghosts: leaving,
    arrows: p.displacements.map((d) => ({
      from: `d${p.id}-${key(d.key)}-from`,
      to: `d${p.id}-${key(d.key)}-to`,
    })),
  });
}

// -- approvals --------------------------------------------------------

function renderAlerts() {
  const mine = state.approvals.filter(
    (a) => a.status === "pending" && (isProvider() || a.client_id === me));
  if (!mine.length) { $("alerts").innerHTML = ""; return; }

  $("alerts").innerHTML = mine.map((a) => {
    const isMove = a.kind === "reschedule";
    const body = isProvider()
      ? `Waiting on <b>${esc(a.client_id)}</b> to confirm
         ${isMove ? `a move ${fmt(a.was)} → ${fmt(a.now)}` : `a booking at ${fmt(a.now)}`}.`
      : (isMove
          ? `We would like to move your appointment from <b>${fmt(a.was)}</b>
             to <b>${fmt(a.now)}</b>.`
          : `Your appointment is offered at <b>${fmt(a.now)}</b>. Does that suit?`);
    const buttons = isProvider() ? "" : `
      <div class="row">
        <button onclick="respond(${a.id}, true)">Accept</button>
        <button class="ghost" onclick="respond(${a.id}, false)">Decline</button>
      </div>`;
    return `<div class="alert"><div>${body}</div>${buttons}</div>`;
  }).join("");
}

// -- side panels ------------------------------------------------------

function renderGrid() {
  const weekly = state.weekly[whose()] || [];
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
    html += `<tr><td class="hour">${i % 2 === 0 ? slotLabel(i) : ""}</td>`;
    for (let d = 0; d < 7; d++) {
      html += `<td class="cell${pendingGrid[d].has(i) ? " on" : ""}" data-d="${d}" data-i="${i}"></td>`;
    }
    html += `</tr>`;
  }
  $("grid").innerHTML = html + `</table>`;
}

function money(minor) { return (minor / 100).toFixed(2); }

function renderCatalogue() {
  if (!isProvider()) return;
  const services = state.services || [];
  $("catalogue").innerHTML = services.map((s) => `
    <div class="req ${s.active ? "" : "withdrawn"}">
      <span>${esc(s.name)}</span>
      <span class="tag">${s.duration}m</span>
      <span class="tag">${money(s.price)}</span>
      ${s.client_bookable ? "" : `<span class="tag">provider only</span>`}
      <button class="link" onclick="setService('${s.id}', ${!s.active})">
        ${s.active ? "discontinue" : "re-list"}</button>
    </div>`).join("");

  const picker = $("service-picker");
  if (picker) {
    picker.innerHTML = services.filter((s) => s.active && s.client_bookable)
      .map((s) => `<option value="${s.id}">${esc(s.name)} · ${s.duration}m · ${money(s.price)}</option>`)
      .join("");
  }
}

function renderRequests() {
  const mine = state.requests.filter((r) => isProvider() || r.client_id === me);
  const open = mine.filter((r) => r.status === "pending" || r.status === "on_hold");
  $("requests").innerHTML = !mine.length ? "" : `
    <div class="reqs">${mine.slice(-6).reverse().map((r) => `
      <div class="req ${r.status}">
        <span>${esc(r.service)} ${isProvider() ? "· " + esc(r.client_id) : ""}</span>
        <span class="tag">${r.status.replace(/_/g, " ")}</span>
        ${r.status === "pending" || r.status === "on_hold"
          ? `<button class="link" onclick="withdraw(${r.id})">withdraw</button>` : ""}
      </div>`).join("")}</div>
    ${open.length ? `<p class="hint">${open.length} still unplaced.</p>` : ""}`;
}

// -- hover card -------------------------------------------------------

function hoverData(a) {
  const client = (state.clients || []).find((c) => c.id === a.client_id);
  const data = {
    Client: client ? client.name : a.client_id,
    Service: a.service,
    Price: money(a.price),
    When: `${fmt(a.start)} – ${a.end.slice(11, 16)}`,
    Status: a.status.replace(/_/g, " "),
  };
  if (a.origin === "displaced") data["Note"] = "moved by the clinic, not chosen";
  if (a.notes) data["Notes"] = a.notes;
  if (client) {
    data["History"] = `${client.completed} attended · ${client.no_show} no-show `
      + `· ${client.cancelled} cancelled · ${client.moved_by_us} moved`;
    if (client.open_requests) data["Open requests"] = client.open_requests;
  }
  return data;
}

document.addEventListener("mouseover", (e) => {
  const block = e.target.closest("[data-hover]");
  const card = $("hover");
  if (!block) { card.style.display = "none"; return; }
  let data;
  try { data = JSON.parse(block.dataset.hover); } catch { return; }
  card.innerHTML = Object.entries(data)
    .map(([k, v]) => `<div><span>${esc(k)}</span>${esc(v)}</div>`).join("");
  card.style.display = "block";
  const r = block.getBoundingClientRect();
  card.style.left = Math.min(r.right + 10, window.innerWidth - 280) + "px";
  card.style.top = Math.min(r.top + window.scrollY,
                            window.scrollY + window.innerHeight - 180) + "px";
});

// -- actions ----------------------------------------------------------

window.respond = async (id, accept) => { await api(`/api/approvals/${id}`, {accept}); refresh(); };
window.withdraw = async (id) => { await api(`/api/requests/${id}/withdraw`); refresh(); };
window.setService = async (id, active) => {
  await api(`/api/services/${id}/active?active=${active}`); refresh();
};
window.approvePlan = async (id) => {
  const items = [...document.querySelectorAll(`input[data-plan="${id}"]`)]
    .filter((b) => b.checked).map((b) => b.dataset.key);
  if (!items.length) { alert("Nothing selected."); return; }
  await api(`/api/plans/${id}/approve`, {items});
  picked = {};
  refresh();
};
window.discardPlan = async (id) => { await api(`/api/plans/${id}/discard`); refresh(); };
window.lockAndRerun = async (id) => {
  // Same mechanism as approving: the locked slots become holds, and the next
  // run simply sees them as taken.
  await approvePlan(id);
  await api("/api/solve", {alpha: Number($("try-alpha").value),
                           max_displacements: Number($("try-moves").value)});
  refresh();
};
window.tick = (box) => {
  picked[box.dataset.key] = box.checked;
  applyDeps(Number(box.dataset.plan));
};

function applyDeps(planId) {
  // A part that rests on a move cannot be sent on without it: ticking the
  // dependent forces its prerequisites on and locks them.
  const boxes = [...document.querySelectorAll(`input[data-plan="${planId}"]`)];
  const byKey = Object.fromEntries(boxes.map((b) => [b.dataset.key, b]));
  boxes.forEach((b) => { b.disabled = false; b.title = ""; });
  for (const box of boxes) {
    if (!box.checked) continue;
    for (const key of (box.dataset.needs || "").split(",").filter(Boolean)) {
      const dep = byKey[key];
      if (!dep) continue;
      dep.checked = true;
      dep.disabled = true;
      dep.title = "needed by another item you have selected";
      picked[key] = true;
    }
  }
}

$("role").onchange = (e) => {
  me = e.target.value;
  pendingGrid = null;
  history.replaceState({}, "", `?as=${me}`);
  render();
};
$("weeks").onchange = render;
$("solve").onclick = async () => {
  await api("/api/solve", {alpha: Number($("try-alpha").value),
                           max_displacements: Number($("try-moves").value)});
  refresh();
};
$("save").onclick = async () => { const r = await api("/api/snapshot/save"); alert("Saved " + r.saved); };
$("reset").onclick = async () => {
  if (confirm("Throw away this session and start again?")) {
    await api("/api/reset"); pendingGrid = null; picked = {}; refresh();
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
  await api("/api/availability", {client_id: whose(), ranges});
  pendingGrid = null;
  refresh();
};
$("request-form").onsubmit = async (e) => {
  e.preventDefault();
  const f = new FormData(e.target);
  await api("/api/requests", {
    client_id: me, service_id: f.get("service"),
    windows: [{from: f.get("from"), to: f.get("to")}],
  });
  refresh();
};
$("service-form").onsubmit = async (e) => {
  e.preventDefault();
  const f = new FormData(e.target);
  const name = f.get("name");
  await api("/api/services", {
    id: name.toLowerCase().replace(/[^a-z0-9]+/g, "-"), name,
    duration_minutes: Number(f.get("duration")),
    price_minor_units: Math.round(Number(f.get("price")) * 100),
  });
  e.target.reset();
  refresh();
};

// Drag-select on the weekly pattern grid.
let painting = null;
document.addEventListener("mousedown", (e) => {
  const cell = e.target.closest(".cell");
  if (!cell) return;
  painting = pendingGrid[+cell.dataset.d].has(+cell.dataset.i) ? "off" : "on";
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

// -- helpers ----------------------------------------------------------

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
const esc = (s) => String(s ?? "").replace(/[&<>]/g, (c) => ({"&": "&amp;", "<": "&lt;", ">": "&gt;"}[c]));

refresh();
setInterval(refresh, 2500);
