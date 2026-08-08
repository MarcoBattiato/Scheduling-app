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
// Never 0: an empty or missing selector would draw nothing at all,
// which looks like the calendar being broken.
const weeksShown = () => Number($("weeks")?.value) || 2;
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
    `saved: alpha ${s.alpha} · max moves ${s.max_displacements} · `
    + `${sch.horizon_days}d ahead · `
    + (sch.auto_run ? `auto (urgent <${sch.urgency_hours}h)` : "manual only");
  // Do not fight the provider while they are typing in it.
  const horizon = $("horizon");
  if (horizon && document.activeElement !== horizon) horizon.value = sch.horizon_days;

  renderProposal();
  renderSettlement();
  renderAlerts();
  renderSchedule();
  renderGrid();
  renderRequests();
  renderBookings();
  renderQueue();
  renderCatalogue();
  renderClients();
  renderExceptions();
  $("log").innerHTML = state.log.slice().reverse()
    .map((l) => `<div class="log-line">${esc(l)}</div>`).join("");

  $("avail-for").textContent = isProvider() ? "(provider)" : `(${me})`;
  $("panel-request").style.display = isProvider() ? "none" : "";
  $("panel-bookings").style.display = isProvider() ? "none" : "";
  $("panel-queue").style.display = isProvider() ? "" : "none";
  $("panel-catalogue").style.display = isProvider() ? "" : "none";
  $("panel-clients").style.display = isProvider() ? "" : "none";
  $("try").style.display = isProvider() ? "" : "none";
  $("solve").style.display = isProvider() ? "" : "none";
}

// -- the schedule calendar -------------------------------------------

function scheduleLayers(state, me, hover) {
  // Pure: state in, calendar layers out. The most fiddly view logic here, and
  // the only way to test it without a DOM.
  const isProv = me === "provider";
  const mine = (a) => isProv || a.client_id === me;

  // Anyone the clinic has asked to move: their current slot is provisional, so
  // it reads as at-risk rather than settled, with the slot it would go to
  // drawn alongside and an arrow between the two.
  const asked = (state.approvals || []).filter(
    (a) => a.status === "pending" && a.kind === "reschedule" && mine(a));
  const movingIds = new Set(asked.map((a) => a.appointment_id));
  const pending = (state.approvals || []).filter(
    (a) => a.status === "pending" && mine(a));

  const appts = state.appointments || [];
  const blocks = [
    // Cancelled first: same stacking level, so anything live paints over it.
    ...appts.filter((a) => a.status.startsWith("cancelled") && mine(a)).map((a) => ({
      id: `x${a.id}`, start: a.start, end: a.end,
      label: isProv ? a.client_id : a.service, cls: "cancelled",
      client: a.client_id, order: 2,        // sits to the right of anything live
      data: hover ? hover(a) : null,
    })),
    ...appts.filter((a) => a.status === "booked" && mine(a)).map((a) => ({
      id: `a${a.id}`, start: a.start, end: a.end,
      label: isProv ? a.client_id : a.service,
      sub: `${a.start.slice(11, 16)}–${a.end.slice(11, 16)}`,
      cls: [movingIds.has(a.id) ? "moving" : "",
            a.origin === "displaced" ? "moved" : "",
            new Date(a.end) < new Date() ? "past" : ""].filter(Boolean).join(" "),
      client: a.client_id, order: 1,
      data: hover ? hover(a) : null,
    })),
    ...pending.map((a) => ({
      id: `p${a.id}`, start: a.now, end: a.now_end,
      label: a.client_id,
      sub: a.kind === "reschedule" ? "would move here" : "offered",
      cls: "proposed-slot", client: a.client_id, order: 0,   // new work on the left
      data: {Client: a.client_id,
             What: a.kind === "reschedule" ? "proposed new slot" : "offered booking",
             From: a.was ? a.was.slice(5, 16).replace("T", " ") : "—",
             Status: "waiting on the client"},
    })),
  ];

  return {
    blocks,
    arrows: asked.map((a) => ({from: `a${a.appointment_id}`, to: `p${a.id}`})),
  };
}
window.scheduleLayers = scheduleLayers;

function renderSchedule() {
  const {blocks, arrows} = scheduleLayers(state, me, hoverData);

  renderCalendar($("calendar"), {
    weeks: weeksShown(),
    start: new Date(state.today + "T00:00"),
    availability: (state.availability[whose()] || []),
    // The tint is already the resolved availability, so an override only needs
    // outlining: an added date is blue inside the dashes, a removed one bare.
    exceptions: ((state.exceptions || {})[whose()] || []).map((e) => ({
      start: `${e.date}T${e.from}:00`, end: `${e.date}T${e.to}:00`, kind: e.kind,
    })),
    blocks,
    arrows,
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

// -- the queue, and what a run is about -------------------------------

// Which requests the next run covers. Empty means "whatever the standing rule
// says", which is the normal case — the ticks are for the exceptions.
let runOver = null;

function planningWindowEnd() {
  const days = (state.scheduler || {}).horizon_days || 7;
  const end = new Date(state.today + "T00:00");
  end.setDate(end.getDate() + days);
  return end;
}

function renderQueue() {
  if (!isProvider()) return;
  const open = (state.requests || []).filter(
    (r) => r.status === "pending" || r.status === "on_hold");
  const scoped = (state.scheduler || {}).scope_to_horizon !== false;
  const end = planningWindowEnd();
  const inWindow = (r) => new Date(r.preferred) < end;

  if (!open.length) {
    runOver = null;
    $("queue").innerHTML = `<p class="hint">Nothing waiting.</p>`;
    return;
  }
  // Default the ticks to what the standing rule would do, so the box shows
  // what is about to happen rather than an empty selection.
  if (runOver === null) {
    runOver = new Set(open.filter((r) => !scoped || inWindow(r)).map((r) => r.id));
  }

  const later = open.filter((r) => !inWindow(r)).length;
  $("queue").innerHTML = `
    <div class="reqs">${open.map((r) => `
      <label class="req ${inWindow(r) ? "" : "later"}">
        <input type="checkbox" data-request="${r.id}" ${runOver.has(r.id) ? "checked" : ""}
               onchange="pickRequest(this)">
        <span>${esc(nameOf(r.client_id))} · ${esc(r.service)}<br>
          <span class="time">wants ${fmt(r.preferred)}</span></span>
        <span class="tag">${inWindow(r) ? r.status.replace(/_/g, " ") : "later"}</span>
      </label>`).join("")}</div>
    <div class="row" style="margin-top:8px">
      <button ${runOver.size ? "" : "disabled"} onclick="runOnPicked()">Run on ${
        runOver.size} selected</button>
      <button class="ghost" onclick="pickWindow()">Just this window</button>
      <button class="ghost" onclick="pickAll()">All</button>
    </div>
    <label class="row hint" style="margin-top:6px">
      <input type="checkbox" ${scoped ? "checked" : ""} onchange="setScoping(this.checked)">
      Runs cover only wishes inside the ${(state.scheduler || {}).horizon_days}-day window
    </label>
    <p class="hint">${later
      ? `${later} request(s) want a time beyond the window. Including them books
         them into it — the horizon crops their availability, so the earliest
         free slot becomes the only slot.`
      : "Everything waiting falls inside the planning window."}</p>`;
}

window.pickRequest = (box) => {
  const id = Number(box.dataset.request);
  if (box.checked) runOver.add(id); else runOver.delete(id);
  render();
};
window.pickWindow = () => {
  const end = planningWindowEnd();
  runOver = new Set((state.requests || [])
    .filter((r) => (r.status === "pending" || r.status === "on_hold")
                   && new Date(r.preferred) < end)
    .map((r) => r.id));
  render();
};
window.pickAll = () => {
  runOver = new Set((state.requests || [])
    .filter((r) => r.status === "pending" || r.status === "on_hold")
    .map((r) => r.id));
  render();
};
window.setScoping = async (on) => {
  runOver = null;                       // the default ticks change with the rule
  await api("/api/settings", {scope_to_horizon: on});
  refresh();
};
window.runOnPicked = async () => {
  const picked = [...runOver];
  runOver = null;                       // re-derive against the queue as it is now
  const outcome = await api("/api/solve", {request_ids: picked});
  if (outcome && outcome.ran === false) {
    alert(`Nothing to do: ${outcome.reason}.`);
  }
  refresh();
};

// -- what the answers add up to, and what to do about them ------------

function renderSettlement() {
  const out = (state.plans || []).filter(
    (p) => p.status === "awaiting_clients" || p.status === "answered");
  if (!isProvider() || !out.length) { $("settlement").innerHTML = ""; return; }

  $("settlement").innerHTML = out.map((plan) => {
    const asks = (state.approvals || []).filter((a) => a.plan_id === plan.id);
    const by = (s) => asks.filter((a) => a.status === s && !a.applied);
    const agreed = by("accepted"), waiting = by("pending");
    const said_no = asks.filter(
      (a) => a.status === "declined" || a.status === "refused");
    const done = asks.filter((a) => a.applied);

    // Nothing here is written down yet, so say so plainly: the provider is
    // reading answers, not a calendar.
    const row = (a) => `
      <div class="answer ${a.applied ? "applied" : a.status}">
        <span class="tag ${a.kind === "booking" ? "ok" : "move"}">${
          a.kind === "booking" ? "book" : "move"}</span>
        <span>${esc(nameOf(a.client_id))}</span>
        <span class="time">${a.was ? `${fmt(a.was)} → ` : ""}${fmt(a.now)}</span>
        <span class="tag">${a.applied ? "booked" : a.status}</span>
      </div>`;

    return `
      <div class="alert proposal">
        <div><b>Plan ${plan.id}: ${agreed.length} agreed, ${waiting.length} waiting,
          ${said_no.length} said no</b>
          <span class="hint">Agreeing is an answer, not a booking. Nothing below
            is in the calendar until you say so.</span></div>
        <div class="answers">${
          [...agreed, ...waiting, ...said_no, ...done].map(row).join("")}</div>
        <div class="row" style="margin-top:10px">
          <button ${agreed.length ? "" : "disabled"}
            onclick="settle(${plan.id}, 'agreed')">Apply the ${agreed.length} agreed</button>
          <button class="ghost" ${waiting.length ? "" : "disabled"}
            onclick="settle(${plan.id}, 'agreed_only')">Apply agreed, drop the ${
              waiting.length} waiting</button>
          <button class="ghost warn"
            onclick="settle(${plan.id}, 'reoptimise')">Reject all and re-plan</button>
        </div>
        <p class="hint">Dropping an unanswered ask puts that request back in the
          queue. Rejecting discards agreed answers too — nobody has been booked.</p>
      </div>`;
  }).join("");
}

// -- the client's own bookings ----------------------------------------

function renderBookings() {
  if (isProvider()) return;
  const mine = (state.appointments || []).filter(
    (a) => a.client_id === me && a.status === "booked");

  if (!mine.length) { $("bookings").innerHTML = `<p class="hint">Nothing booked.</p>`; return; }

  const label = {
    asked: ["being moved?", "We have asked you to move this. Answer above."],
    agreed: ["move agreed", "You said yes. It changes when the clinic confirms."],
    moving: ["you asked to move", "Kept until we find you somewhere else."],
  };

  $("bookings").innerHTML = `<div class="reqs">${mine.map((a) => {
    const p = a.pending, tag = p ? label[p.state] : null;
    return `
    <div class="req ${p ? "on_hold" : ""}">
      <span>${esc(a.service)}<br><span class="time">${fmt(a.start)}</span></span>
      <span class="tag" title="${tag ? esc(tag[1]) : "Confirmed."}">${
        tag ? tag[0] : "confirmed"}</span>
      ${p ? "" : `<button class="link" onclick="askToMove(${a.id})">move</button>`}
    </div>`;
  }).join("")}</div>
  <p class="hint">A booking with no tag is settled. Anything else has a question
    hanging over it — hover the tag to see what.</p>`;
}

window.askToMove = async (appointmentId) => {
  const appointment = appointmentById(appointmentId);
  const when = prompt(
    "When would you rather be seen? (YYYY-MM-DDTHH:MM)\n\n"
    + "A wish, not a demand — we look anywhere you are available.",
    appointment ? appointment.start.slice(0, 16) : "");
  if (!when) return;
  // The real decision, and the client's risk to take: give the hour up now and
  // it frees for everybody, but there may be nothing to replace it with.
  const release = confirm(
    "Give up your current slot straight away?\n\n"
    + "OK — release it now. It frees up for others, and you may end up with "
    + "nothing if we cannot find you another.\n\n"
    + "Cancel — keep it until we have somewhere to move you. Safer, but the "
    + "hour stays blocked meanwhile.");
  await api(`/api/appointments/${appointmentId}/reschedule-request`,
            {preferred_start: when, release_slot: release});
  refresh();
};

window.settle = async (planId, how) => {
  await api(`/api/plans/${planId}/settle`, {how});
  refresh();
};

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
        <span class="tag">off-wish ${m.preference_gap_minutes}m</span>
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
                  label: a.client_id, cls: "existing", client: a.client_id,
                  order: 1, data: hoverData(a)}));

  const key = (k) => k.replace(":", "-");
  const arriving = p.placements.map((x) => ({
    id: `d${p.id}-${key(x.key)}`,
    start: x.start, end: x.end, label: x.client_id, sub: "new",
    cls: "proposed", client: x.client_id, order: 0,
    data: proposedBookingData(x, p),
  }));
  const landing = p.displacements.map((d) => ({
    id: `d${p.id}-${key(d.key)}-to`,
    start: d.now, end: d.now_end, label: d.client_id, sub: "moved to",
    cls: "proposed moved", client: d.client_id, order: 0,
    data: proposedMoveData(d, "to", p),
  }));
  const leaving = p.displacements.map((d) => ({
    id: `d${p.id}-${key(d.key)}-from`,
    start: d.was, end: d.was_end, label: d.client_id, cls: "vacating",
    client: d.client_id, order: 2,     // the slot being given up, on the right
    data: proposedMoveData(d, "from", p),
  }));

  renderCalendar(el, {
    weeks: weeksShown(),
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
    // Being asked to move has three honest answers. "Not that time" and "not
    // at all" are worth a great deal to the scheduler and only the client can
    // tell them apart, so the choice is put to them rather than inferred.
    const buttons = isProvider() ? "" : `
      <div class="row">
        <button onclick="respond(${a.id}, 'accept')">Accept</button>
        <button class="ghost" onclick="respond(${a.id}, 'decline')">${
          isMove ? "Not that time" : "Decline"}</button>
        ${isMove ? `<button class="ghost warn"
          onclick="respond(${a.id}, 'refuse')">Keep my slot</button>` : ""}
      </div>
      ${isMove ? `<p class="hint"><b>Not that time</b> blocks that slot only —
         we will look for another. <b>Keep my slot</b> means we stop asking.
         Either way it helps to update your availability for that week.</p>` : ""}`;
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

function renderClients() {
  if (!isProvider()) return;
  $("clients").innerHTML = (state.clients || []).map((c) => `
    <div class="client-row">
      <span class="who">${esc(c.name)}</span>
      <span class="tag">${c.booked} booked</span>
      ${c.completed ? `<span class="tag">${c.completed} attended</span>` : ""}
      ${c.no_show ? `<span class="tag">${c.no_show} no-show</span>` : ""}
      ${c.moved_by_us ? `<span class="tag move">${c.moved_by_us} moved</span>` : ""}
      ${c.open_requests ? `<span class="tag">${c.open_requests} waiting</span>` : ""}
    </div>`).join("");
}

function renderExceptions() {
  // Single-date overrides are otherwise invisible once made — you can see
  // their effect on the tint but not what caused it, nor undo it.
  const mine = (state.exceptions || {})[whose()] || [];
  $("exceptions").innerHTML = !mine.length ? "" :
    `<span class="hint">This date only:</span>` + mine.map((e) => `
      <span class="exception ${e.kind === "remove" ? "away" : ""}">
        ${new Date(e.date + "T00:00").toLocaleDateString([], {weekday: "short", day: "numeric", month: "short"})}
        ${e.from}–${e.to} ${e.kind === "remove" ? "away" : "available"}
        <button class="link" onclick="clearException('${e.date}','${e.from}','${e.to}',${e.kind === "add"})">clear</button>
      </span>`).join("");
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

function clientFacts(clientId) {
  // The same background wherever a client appears — a proposal about moving
  // someone is exactly where their history is worth seeing.
  const client = (state.clients || []).find((c) => c.id === clientId);
  if (!client) return {Client: clientId};
  const facts = {Client: client.name};
  facts["History"] = `${client.completed} attended · ${client.no_show} no-show `
    + `· ${client.cancelled} cancelled · ${client.moved_by_us} moved`;
  if (client.open_requests) facts["Waiting on"] = `${client.open_requests} request(s)`;
  return facts;
}

const appointmentById = (id) => (state.appointments || []).find((a) => a.id === id);
const requestById = (id) => (state.requests || []).find((r) => r.id === id);

function hoverData(a) {
  const data = {
    ...clientFacts(a.client_id),
    Service: a.service,
    Price: money(a.price),
    When: `${fmt(a.start)} – ${a.end.slice(11, 16)}`,
    Status: a.status.replace(/_/g, " "),
  };
  if (a.preferred) data["Asked for"] = fmt(a.preferred);
  if (a.origin === "displaced") data["Note"] = "moved by the clinic, not chosen";
  if (a.notes) data["Notes"] = a.notes;
  return reorder(data);
}

function proposedBookingData(x, plan) {
  const request = requestById(x.request_id);
  return reorder({
    ...clientFacts(x.client_id),
    What: "proposed booking",
    Service: x.service,
    When: `${fmt(x.start)} – ${x.end.slice(11, 16)}`,
    "Asked for": request ? fmt(request.preferred) : "—",
    ...dependency(x, plan),
    Status: "not booked yet",
  });
}

function dependency(item, plan) {
  // The engine reports what has to happen first. Hiding that turns a
  // conditional proposal into one that looks free-standing.
  const on = (item.depends_on || []).map((key) => {
    const d = (plan.displacements || []).find((x) => x.key === key);
    return d ? nameOf(d.client_id) : key;
  });
  return on.length ? {"Only if": `${on.join(", ")} move${on.length > 1 ? "" : "s"} first`} : {};
}

function nameOf(clientId) {
  const client = (state.clients || []).find((c) => c.id === clientId);
  return client ? client.name : clientId;
}

function proposedMoveData(d, side, plan) {
  const appointment = appointmentById(d.appointment_id);
  const minutes = Math.round(
    Math.abs(new Date(d.now) - new Date(d.was)) / 60000);
  return reorder({
    ...clientFacts(d.client_id),
    What: side === "from" ? "would be moved out of here" : "would be moved here",
    Service: appointment ? appointment.service : "—",
    Currently: fmt(d.was),
    Proposed: fmt(d.now),
    Moves: `${Math.floor(minutes / 60)}h ${minutes % 60}m`,
    "Asked for": appointment && appointment.preferred ? fmt(appointment.preferred) : "—",
    ...dependency(d, plan),
  });
}

function reorder(data) {
  // Client first, background last: the answer to "who is this and when" should
  // not be pushed below their history.
  const {Client, History, ...rest} = data;
  const out = {Client};
  for (const [k, v] of Object.entries(rest)) out[k] = v;
  if (History) out["History"] = History;
  return out;
}

const CALENDARS = "#calendar, .draft-cal";

function peekAt(block) {
  // Whose hours are these? Answering that on the calendar itself is the point:
  // "moved to Wednesday 11:00" means little without seeing when they are free.
  // Works on whichever calendar the booking is in — the schedule or a proposal.
  const root = block && block.closest(CALENDARS);
  const client = block && block.dataset.client;
  if (!root) return;
  clearPeek();
  window.calendarOverlay(root, client ? (state.availability[client] || []) : [],
                         "cal-peek");
  // Everything that describes *general* availability gets out of the way,
  // single-date overrides included — while peeking, the only availability on
  // screen should be this client's.
  root.querySelectorAll(".cal-day").forEach((d) => d.classList.toggle("dimmed", !!client));
}

function clearPeek() {
  for (const root of document.querySelectorAll(CALENDARS)) {
    window.calendarOverlay(root, [], "cal-peek");
    root.querySelectorAll(".cal-day").forEach((d) => d.classList.remove("dimmed"));
  }
}

document.addEventListener("mouseover", (e) => {
  const block = e.target.closest("[data-hover]");
  const card = $("hover");
  if (!block) { card.style.display = "none"; clearPeek(); return; }
  peekAt(block);
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

window.respond = async (id, answer) => {
  await api(`/api/approvals/${id}`, {answer});
  refresh();
};
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
window.clearException = async (date, from, to, wasAvailable) => {
  await api("/api/exceptions", {client_id: whose(), date, from_time: from,
                                to_time: to, available: wasAvailable, clear: true});
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
$("horizon").onchange = async (e) => {
  await api("/api/settings", {horizon_days: Number(e.target.value)});
  refresh();
};
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
    preferred_start: f.get("preferred"),
  });
  refresh();
};
$("client-form").onsubmit = async (e) => {
  e.preventDefault();
  const f = new FormData(e.target);
  const name = String(f.get("name")).trim();
  await api("/api/clients", {
    id: name.toLowerCase().replace(/[^a-z0-9]+/g, "-"),
    name,
    mirror_provider: f.get("mirror") !== null,
  });
  e.target.reset();
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
