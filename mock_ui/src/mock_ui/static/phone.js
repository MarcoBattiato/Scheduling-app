// The phone front end: the same server and the same API as the desktop page,
// laid out for one thumb. Nothing here depends on hover or drag — every
// interaction the desktop does by painting or peeking is a tap, a sheet, or a
// picker instead.
//
// Views are plain functions from state to HTML. Every tap goes through one
// delegated handler keyed on `data-act`, so the markup carries no inline
// script and a view can be rendered (and tested) without a browser.
//
// Like the desktop page it polls, but it only redraws when the state actually
// changed and never while someone is typing in a field — a poll that resets an
// open picker every 2.5s is unusable on a phone.

const params = new URLSearchParams(location.search);
const remembered = () => { try { return localStorage.getItem("phone-as"); } catch { return null; } };
const remember = (who) => { try { localStorage.setItem("phone-as", who); } catch { /* private mode */ } };

let me = params.get("as") || remembered() || "provider";
let state = null;
let stateText = "";
let tab = (location.hash || "").slice(1) || "home";

// Everything the person has chosen but not yet sent. Kept here rather than in
// the DOM so a redraw from a poll cannot throw it away.
const ui = {
  pickers: {},          // picker id -> {client, duration, day, time}
  bookService: null,
  bookEvery: 0,         // 0 = once; otherwise weeks between sessions
  diaryDay: null,
  runOver: null,        // Set of request ids the next run covers
  runSeen: null,        // ids already given a default tick, so a choice sticks
  alpha: null,
  moves: null,
  picked: {},           // plan id -> {item key -> ticked}
  away: {mode: "days", action: "flag"},
  exceptionAvailable: false,
};

const $ = (id) => document.getElementById(id);
const PROVIDER = "provider-self";
const isProvider = () => me === "provider";
const whose = () => (isProvider() ? PROVIDER : me);

// -- formatting ------------------------------------------------------

const DOW = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
const WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];   // the server's 0..6
const WEEKDAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday",
                       "Saturday", "Sunday"];
const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep",
                "Oct", "Nov", "Dec"];

const pad = (n) => String(n).padStart(2, "0");
const esc = (s) => String(s ?? "").replace(/[&<>"]/g,
  (c) => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;"}[c]));
const parse = (iso) => new Date(iso.length === 10 ? `${iso}T00:00` : iso);
const dayKey = (d) => `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
const addDays = (key, n) => { const d = parse(key); d.setDate(d.getDate() + n); return dayKey(d); };
const toMin = (hhmm) => { const [h, m] = hhmm.split(":").map(Number); return h * 60 + m; };
const hhmm = (mins) => `${pad(Math.floor(mins / 60))}:${pad(mins % 60)}`;
const minOf = (iso) => toMin(iso.slice(11, 16));
const fmtTime = (iso) => iso.slice(11, 16);
const fmtDay = (iso) => {
  const d = parse(iso.slice(0, 10));
  return `${DOW[d.getDay()]} ${d.getDate()} ${MONTHS[d.getMonth()]}`;
};
const relDay = (iso) => {
  const key = iso.slice(0, 10);
  if (key === todayKey()) return "Today";
  if (key === addDays(todayKey(), 1)) return "Tomorrow";
  return fmtDay(key);
};
const fmtWhen = (iso) => `${relDay(iso)}, ${fmtTime(iso)}`;
const money = (minor) => (minor / 100).toFixed(2);
const todayKey = () => (state && state.today) || dayKey(new Date());
const nowMinutes = () => { const d = new Date(); return d.getHours() * 60 + d.getMinutes(); };
const isPast = (iso) => parse(iso) < new Date();
const plural = (n, one, many) => `${n} ${n === 1 ? one : (many || one + "s")}`;

function datebox(iso) {
  const d = parse(iso.slice(0, 10));
  return `<div class="datebox"><span>${DOW[d.getDay()]}</span><b>${d.getDate()}</b></div>`;
}

// -- lookups ---------------------------------------------------------

const clientById = (id) => (state.clients || []).find((c) => c.id === id);
const nameOf = (id) => (id === PROVIDER ? "You" : (clientById(id) || {}).name || id);
const appointmentById = (id) => (state.appointments || []).find((a) => a.id === id);
const requestById = (id) => (state.requests || []).find((r) => r.id === id);
const planById = (id) => (state.plans || []).find((p) => p.id === id);
const serviceById = (id) => (state.services || []).find((s) => s.id === id);
const bookable = () => (state.services || []).filter((s) => s.active && s.client_bookable);
const openRequest = (r) => r.status === "pending" || r.status === "on_hold";

// Still in the air: the client has not answered, or has and the provider has
// not yet written it down. Both are unsettled; a "yes" must not make it vanish.
const unsettled = (a) => !a.applied && (a.status === "pending" || a.status === "accepted");

function approvalService(a) {
  if (a.kind === "reschedule") return (appointmentById(a.appointment_id) || {}).service || "Appointment";
  const plan = planById(a.plan_id);
  const placed = plan && plan.placements.find(
    (x) => x.client_id === a.client_id && x.start === a.now);
  return (placed && placed.service) || "Appointment";
}

// Where a person is free on one date, as minute ranges clipped to that day.
function segmentsOn(segments, key) {
  const start = parse(key), end = parse(addDays(key, 1));
  return (segments || []).map((s) => {
    const from = Math.max(parse(s.start), start), to = Math.min(parse(s.end), end);
    return to > from ? {from: (from - start) / 60000, to: (to - start) / 60000} : null;
  }).filter(Boolean);
}

// Times someone could start something this long on this date, on the half hour.
function startsOn(clientId, key, duration) {
  const earliest = key === todayKey() ? nowMinutes() + 30 : -1;
  const out = new Set();
  for (const seg of segmentsOn((state.availability || {})[clientId], key)) {
    for (let t = Math.ceil(seg.from / 30) * 30; t + duration <= seg.to; t += 30) {
      if (t > earliest) out.add(t);
    }
  }
  return [...out].sort((a, b) => a - b);
}

const windowDays = () => {
  const days = [];
  for (let key = todayKey(); key < (state.horizon || addDays(todayKey(), 28)); key = addDays(key, 1)) {
    days.push(key);
  }
  return days;
};

// -- money helpers ---------------------------------------------------

const sgd = (cents) => `S$${(cents / 100).toLocaleString("en-SG", {minimumFractionDigits: 2, maximumFractionDigits: 2})}`;
const chargesOf = (who, status) => (state.charges || []).filter(
  (c) => (!who || c.client_id === who) && (!status || c.status === status));
const paymentOf = (charge) => (state.payments || []).find((p) => p.id === charge.paid_by);
const methodWords = (m) => ((state.billing || {}).methods || {})[m] || m || "";
const chargeForAppointment = (id, kind = "session") =>
  (state.charges || []).find((c) => c.appointment_id === id && c.kind === kind);

function payRow(c, action) {
  const paid = c.status === "paid" ? paymentOf(c) : null;
  return `<div class="row">
    <div class="main"><div class="title">${sgd(c.amount)}</div>
      <div class="meta">${esc(c.description)}</div>
      ${paid ? `<div class="meta">Paid ${fmtDay(paid.created)} · ${esc(methodWords(paid.method))}</div>` : ""}
      ${c.gst ? `<div class="meta">includes ${sgd(c.gst)} GST</div>` : ""}</div>
    ${action || ""}</div>`;
}

const limitWords = (c) => [c.per_day ? `${c.per_day} a day` : "", c.per_week ? `${c.per_week} a week` : ""]
  .filter(Boolean).join(", ") || "no limit";
const everyWords = (n) => (n === 1 ? "every week" : `every ${n} weeks`);
const FREQUENCIES = [[0, "Once"], [1, "Weekly"], [2, "Every 2 weeks"], [4, "Every 4 weeks"]];

function seriesLine(x) {
  const d = parse(x.first_start);
  return `${everyWords(x.every_weeks)} · ${DOW[d.getDay()]}s ${fmtTime(x.first_start)}`;
}

function seriesMeta(x) {
  const bits = [];
  if (x.next) bits.push(`next ${fmtDay(x.next)}`);
  if (x.until) bits.push(`until ${fmtDay(x.until)}`);
  else if (x.status === "active") bits.push("until stopped");
  if (x.status === "finished") bits.push("all sessions raised");
  return bits.join(" · ");
}

function frequencyPicker(current, act) {
  return `<div class="segmented">${FREQUENCIES.map(([n, label]) => `
    <button type="button" data-act="${act}" data-n="${n}" aria-pressed="${current === n}">${label}</button>`).join("")}</div>`;
}

// The optional last date, inside a form so a redraw keeps what was typed.
function untilField(name) {
  return `<form data-form="${name}">
    <label class="field" style="margin-top:12px"><span>Last session (optional)</span>
      <input type="date" name="until" min="${todayKey()}"></label></form>
    <p class="hint" style="margin-top:-4px">Leave it empty to keep going until you stop it.</p>`;
}

// -- the clinician chat: fetched through the gate, never in /api/state ---

// Conversations are clinical, so they are not in the shared state every
// screen polls: each person asks the gate for their own, as themselves.
const chat = {mine: null, summary: [], thread: {}};

async function command(name, args) {
  const res = await fetch(`/api/commands/${name}?as=${encodeURIComponent(me)}`, {
    method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({args}),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.detail || "That did not work");
  return data.result;
}

const openThread = () => (isProvider() && tab.startsWith("more/messages/") ? tab.split("/")[2] : null);

async function fetchChat() {
  try {
    if (isProvider()) {
      chat.mine = null;
      chat.summary = (await command("message.list", {})) || [];
      const open = openThread();
      if (open) chat.thread[open] = await command("message.list", {client_id: open});
    } else {
      chat.summary = [];
      chat.mine = (await command("message.list", {})) || null;
    }
  } catch { /* messaging off, or a person who has gone: nothing to show */ }
  return JSON.stringify([chat.mine, chat.summary, chat.thread[openThread()] || null]);
}

// -- talking to the server --------------------------------------------

async function api(path, body) {
  const res = await fetch(path, body === undefined ? {method: "POST"} : {
    method: "POST", headers: {"Content-Type": "application/json"},
    body: JSON.stringify(body),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    toast(data.detail || "That did not work");
    return null;
  }
  return data;
}

let chatText = "";

async function refresh(force) {
  const text = await (await fetch("/api/state")).text();
  const conversation = await fetchChat();
  if (!force && text === stateText && conversation === chatText) return;
  stateText = text;
  chatText = conversation;
  state = JSON.parse(text);
  render(force);
  markSeen();
}

// Opening a conversation reads it. Done after drawing, and only once per
// batch of new messages, so the other side's "read" is honest.
let marking = false;
async function markSeen() {
  const who = isProvider() ? openThread() : (currentView() === "messages" ? me : null);
  const convo = isProvider() ? chat.thread[who] : chat.mine;
  if (!who || !convo || !convo.unread || marking) return;
  marking = true;
  try { await command("message.mark_read", isProvider() ? {client_id: who} : {}); }
  catch { /* try again next time */ }
  marking = false;
  refresh(true);
}

// -- rendering -------------------------------------------------------

const TABS = {
  client: [
    ["home", "Home", "M3 11l9-7 9 7v9a1 1 0 0 1-1 1h-5v-6h-6v6H4a1 1 0 0 1-1-1z"],
    ["book", "Book", "M12 5v14M5 12h14"],
    ["bookings", "Bookings", "M4 6h16v14H4zM4 10h16M8 3v4M16 3v4"],
    ["hours", "My hours", "M12 7v5l3 2M12 3a9 9 0 1 0 0 18a9 9 0 0 0 0-18z"],
    ["messages", "Messages", "M4 5h16v11H9l-5 4z"],
  ],
  provider: [
    ["home", "Home", "M3 11l9-7 9 7v9a1 1 0 0 1-1 1h-5v-6h-6v6H4a1 1 0 0 1-1-1z"],
    ["diary", "Diary", "M4 6h16v14H4zM4 10h16M8 3v4M16 3v4"],
    ["queue", "Requests", "M4 13h4l2 3h4l2-3h4M4 13l2-8h12l2 8v6H4z"],
    ["plans", "Plans", "M6 3v12M6 15a3 3 0 1 0 0 6a3 3 0 0 0 0-6zM18 9a3 3 0 1 0 0-6a3 3 0 0 0 0 6zM18 9c0 6-12 3-12 6"],
    ["more", "More", "M4 12a1.6 1.6 0 1 0 3.2 0a1.6 1.6 0 1 0-3.2 0M10.4 12a1.6 1.6 0 1 0 3.2 0a1.6 1.6 0 1 0-3.2 0M16.8 12a1.6 1.6 0 1 0 3.2 0a1.6 1.6 0 1 0-3.2 0"],
  ],
};

function badges() {
  if (!isProvider()) {
    return {home: (state.approvals || []).filter(
      (a) => a.client_id === me && !a.applied && a.status === "pending").length
      + chargesOf(me, "due").length,
      messages: (chat.mine && chat.mine.unread) || 0};
  }
  const plans = state.plans || [];
  const agreedWaiting = plans.filter((p) => {
    if (p.status !== "awaiting_clients" && p.status !== "answered") return false;
    const asks = (state.approvals || []).filter((a) => a.plan_id === p.id);
    return asks.some((a) => a.status === "accepted" && !a.applied && !blockedBy(p, a, asks).length);
  });
  return {
    queue: (state.requests || []).filter(openRequest).length,
    plans: plans.filter((p) => p.status === "draft").length + agreedWaiting.length,
    home: stranded().filter((a) => !a.pending).length + chargesOf(null, "proposed").length
      + chat.summary.filter((t) => t.unread).length,
  };
}

const VIEWS = {
  client: {home: clientHome, book: clientBook, bookings: clientBookings, hours: hoursView,
           messages: clientMessages},
  provider: {home: providerHome, diary: diaryView, queue: queueView, plans: plansView,
             more: moreView},
};

function currentView() {
  const views = VIEWS[isProvider() ? "provider" : "client"];
  const [base] = tab.split("/");
  return views[base] ? base : "home";
}

function render(force) {
  if (!state) return;
  const known = ["provider", ...state.clients.map((c) => c.id)];
  if (!known.includes(me)) { me = "provider"; tab = "home"; }

  // Do not pull a field out from under somebody's thumb. The next poll, or
  // their own action, draws it.
  const active = document.activeElement;
  const typing = active && active.closest && active.closest("#view")
    && /^(INPUT|SELECT|TEXTAREA)$/.test(active.tagName);
  if (typing && !force) return;

  renderHeader();
  renderTabs();

  const base = currentView();
  const scrolls = keepScrolls($("view"));
  const kept = keepForms($("view"));
  $("view").innerHTML = VIEWS[isProvider() ? "provider" : "client"][base]();
  restoreForms($("view"), kept);
  restoreScrolls($("view"), scrolls);
  // The sheet is redrawn too, so it never shows a booking that has since
  // changed — unless someone is in the middle of one of its fields.
  const inSheet = active && active.closest && active.closest("#sheet")
    && /^(INPUT|SELECT|TEXTAREA)$/.test(active.tagName);
  if (!inSheet) redrawSheet();
}

// Horizontal strips keep their scroll position across a redraw; otherwise
// every poll snaps the day picker back to today. A strip drawn for the first
// time scrolls to its chosen day instead.
function keepScrolls(root) {
  const scrolls = {};
  root.querySelectorAll("[data-keep-scroll]").forEach((el) => {
    scrolls[el.dataset.keepScroll] = el.scrollLeft;
  });
  return scrolls;
}

function restoreScrolls(root, scrolls) {
  root.querySelectorAll("[data-keep-scroll]").forEach((el) => {
    if (scrolls[el.dataset.keepScroll] !== undefined) el.scrollLeft = scrolls[el.dataset.keepScroll];
    else if (el.dataset.scrollTo) {
      const target = el.querySelector(`[data-day="${el.dataset.scrollTo}"]`);
      if (target) {
        el.scrollLeft += target.getBoundingClientRect().left - el.getBoundingClientRect().left - 16;
      }
    }
  });
}

// What has been typed or chosen in a form survives a redraw. Without this a
// poll, or tapping an option further down, resets the dates picked above it.
function keepForms(root) {
  const kept = {};
  root.querySelectorAll("[data-form]").forEach((form) => {
    kept[form.dataset.form] = [...form.elements].filter((e) => e.name)
      .map((e) => [e.name, e.type === "checkbox" ? e.checked : e.value]);
  });
  return kept;
}

function restoreForms(root, kept) {
  root.querySelectorAll("[data-form]").forEach((form) => {
    for (const [name, value] of kept[form.dataset.form] || []) {
      const field = form.elements[name];
      if (!field || field.type === "submit") continue;
      if (field.type === "checkbox") field.checked = value; else field.value = value;
    }
  });
}

function renderHeader() {
  const client = clientById(me);
  const name = isProvider() ? "Provider" : (client ? client.name : me);
  $("who").textContent = name;
  $("avatar").textContent = name.slice(0, 1).toUpperCase();
  $("subtitle").textContent = isProvider() ? "Practice diary" : "Your appointments";
  const sel = $("role");
  const options = [["provider", "Provider"], ...state.clients.map((c) => [c.id, c.name])];
  const wanted = options.map(([id, label]) => `<option value="${esc(id)}">${esc(label)}</option>`).join("");
  if (sel.dataset.options !== wanted) { sel.innerHTML = wanted; sel.dataset.options = wanted; }
  sel.value = me;
  document.title = `${name} · Appointments`;
}

function renderTabs() {
  const list = TABS[isProvider() ? "provider" : "client"];
  const count = badges();
  const current = currentView();
  $("tabs").innerHTML = list.map(([id, label, path]) => `
    <button class="tab" data-act="tab" data-tab="${id}" ${id === current ? 'aria-current="page"' : ""}>
      <svg viewBox="0 0 24 24" aria-hidden="true"><path d="${path}"/></svg>
      ${label}
      ${count[id] ? `<span class="badge">${count[id]}</span>` : ""}
    </button>`).join("");
}

// On an iPhone in Safari, say once how to make this an app. Not shown once
// it is installed (navigator.standalone), nor anywhere else.
const isIOSSafari = () => {
  const nav = typeof navigator === "undefined" ? {} : navigator;
  return /iPhone|iPad|iPod/.test(nav.userAgent || "") && nav.standalone === false;
};
const tipDismissed = () => { try { return localStorage.getItem("install-tip") === "no"; } catch { return false; } };

function installTip() {
  if (!isIOSSafari() || tipDismissed()) return "";
  return `<div class="install-tip"><img src="/static/icon-180.png" alt="">
    <div class="main"><b>Use it like an app.</b> Tap <b>Share</b>
      <svg viewBox="0 0 24 24" width="16" height="16" style="vertical-align:-3px" aria-label="the Share icon"
        fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
        <path d="M12 3v12M8 7l4-4 4 4M5 12v7a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2v-7"/></svg>,
      then <b>Add to Home Screen</b>.</div>
    <button data-act="install-tip-dismiss" aria-label="Dismiss">×</button></div>`;
}

function empty(title, body, action) {
  return `<div class="empty"><b>${title}</b>${body || ""}${action || ""}</div>`;
}

// -- the client: home --------------------------------------------------

function clientHome() {
  const client = clientById(me) || {name: me};
  const asks = (state.approvals || []).filter((a) => a.client_id === me && unsettled(a));
  const upcoming = (state.appointments || [])
    .filter((a) => a.client_id === me && a.status === "booked" && !isPast(a.end))
    .sort((a, b) => a.start.localeCompare(b.start));
  const waiting = (state.requests || []).filter((r) => r.client_id === me && openRequest(r));
  const next = upcoming[0];

  const owes = chargesOf(me, "due").length > 0;
  let html = `${installTip()}<h1>Hi ${esc(client.name)}</h1>`;
  html += owes && !asks.some((a) => a.status === "pending")
    ? `<p class="lead">You have something to pay.</p>`
    : asks.length
    ? `<p class="lead">${asks.some((a) => a.status === "pending")
        ? "The clinic has a question for you." : "Nothing needs you right now."}</p>`
    : `<p class="lead">${next ? "You're all set." : "Nothing booked yet."}</p>`;

  html += asks.map(askCard).join("");

  const owed = chargesOf(me, "due");
  if (owed.length) {
    html += `<div class="card attention"><span class="tag accent">to pay</span>
      <h3 style="margin-top:8px">${sgd(owed.reduce((t, c) => t + c.amount, 0))} to pay</h3>
      <div class="list" style="box-shadow:none;margin:8px 0 0">${owed.map((c) => payRow(c,
        `<button class="btn small" data-act="pay" data-id="${c.id}">Pay</button>`)).join("")}</div>
      <p class="hint" style="margin:8px 0 0">PayNow (works with DBS PayLah! and any bank app), card or GrabPay.</p>
    </div>`;
  }

  if (next) {
    html += `<h2>Next appointment</h2>
      <button class="card tap" data-act="booking" data-id="${next.id}">
        ${datebox(next.start)}
        <div class="main">
          <div class="when">${relDay(next.start)}, ${fmtTime(next.start)}</div>
          <div class="meta">${esc(next.service)} · until ${fmtTime(next.end)}</div>
          <div class="tags">${bookingTag(next)}</div>
        </div>
        <span class="chev">›</span>
      </button>`;
  }

  if (waiting.length) {
    html += `<h2>Waiting for a time</h2><div class="list">${waiting.map(requestRow).join("")}</div>
      <p class="hint">The clinic plans the week in one go, so a request is answered
        when the next schedule is made — not straight away.</p>`;
  }

  if (!next && !waiting.length && !asks.length) {
    html += empty("No appointments",
      `<p>Ask for one and the clinic will find you a time when you are free.</p>`,
      `<button class="btn" data-act="tab" data-tab="book">Book an appointment</button>`);
  } else {
    html += `<div class="stack"><button class="btn secondary" data-act="tab" data-tab="book">
      Book another appointment</button></div>`;
  }
  return html;
}

function askCard(a) {
  const move = a.kind === "reschedule";
  const service = esc(approvalService(a));
  if (a.status === "accepted") {
    return `<div class="card held">
      <span class="tag held">waiting for the clinic</span>
      <h3 style="margin-top:8px">${move ? "Move agreed" : "Booking agreed"}</h3>
      <p>${service}, <b>${fmtWhen(a.now)}</b>${move ? ` (was ${fmtWhen(a.was)})` : ""}.</p>
      <p class="hint" style="margin:6px 0 0">You said yes. The time is held for you
        and becomes final once the clinic confirms it.</p>
    </div>`;
  }
  if (!move) {
    return `<div class="card attention">
      <span class="tag accent">needs your answer</span>
      <h3 style="margin-top:8px">A time for your ${service}</h3>
      <div class="when">${fmtWhen(a.now)}</div>
      <p class="meta">until ${fmtTime(a.now_end)}</p>
      <div class="pair">
        <button class="btn ok" data-act="respond" data-id="${a.id}" data-answer="accept">Accept</button>
        <button class="btn secondary" data-act="respond" data-id="${a.id}" data-answer="decline">Decline</button>
      </div>
    </div>`;
  }
  // Being asked to move has three honest answers. "Not that time" and "not at
  // all" are worth a great deal to the scheduler, and only the client can tell
  // them apart — so the choice is put to them rather than inferred.
  return `<div class="card attention">
    <span class="tag accent">needs your answer</span>
    <h3 style="margin-top:8px">Could you move your ${service}?</h3>
    <p class="meta" style="text-decoration:line-through">${fmtWhen(a.was)}</p>
    <div class="when">${fmtWhen(a.now)}</div>
    <div class="stack">
      <button class="btn ok" data-act="respond" data-id="${a.id}" data-answer="accept">Yes, move me</button>
      <div class="pair" style="margin-top:0">
        <button class="btn secondary" data-act="respond" data-id="${a.id}" data-answer="decline">Not that time</button>
        <button class="btn danger" data-act="respond" data-id="${a.id}" data-answer="refuse">Keep my slot</button>
      </div>
    </div>
    <p class="hint" style="margin:10px 0 0"><b>Not that time</b> rules out that
      slot only, and they may ask about another. <b>Keep my slot</b> means they
      stop asking.</p>
  </div>`;
}

function bookingTag(a) {
  const p = a.pending;
  if (p && p.state === "asked") return `<span class="tag accent">asked to move</span>`;
  if (p && p.state === "agreed") return `<span class="tag held">move agreed</span>`;
  if (p && p.state === "moving") return `<span class="tag move">you asked to move</span>`;
  if (a.origin === "displaced") return `<span class="tag ok">confirmed</span> <span class="tag">moved by the clinic</span>`;
  return `<span class="tag ok">confirmed</span>`;
}

const REQUEST_WORDS = {
  pending: ["finding a time", "accent"],
  on_hold: ["still looking", "warn"],
  awaiting_client: ["answer needed", "accent"],
};

function requestRow(r) {
  const [word, cls] = REQUEST_WORDS[r.status] || [r.status.replace(/_/g, " "), ""];
  const replacing = r.replaces ? appointmentById(r.replaces) : null;
  return `<div class="row">
    ${datebox(r.preferred)}
    <div class="main">
      <div class="title">${esc(r.service)}${replacing ? " (moving)" : ""}</div>
      <div class="meta">ideally ${fmtWhen(r.preferred)}</div>
      <div class="tags"><span class="tag ${cls}">${word}</span>${
        r.series_id ? ` <span class="tag accent">${esc(r.note || "repeating")}</span>` : ""}</div>
    </div>
    ${openRequest(r) ? `<button class="btn small danger" data-act="withdraw" data-id="${r.id}">Withdraw</button>` : ""}
  </div>`;
}

// -- the client: book --------------------------------------------------

function picker(id, client, duration) {
  const p = ui.pickers[id] || (ui.pickers[id] = {client, duration, day: null, time: null});
  p.client = client; p.duration = duration;
  const days = windowDays();
  const free = Object.fromEntries(days.map((d) => [d, startsOn(client, d, duration).length]));
  if (!p.day) p.day = days.find((d) => free[d]) || days[0];
  const starts = startsOn(client, p.day, duration);
  if (p.time !== null && !starts.includes(p.time)) p.time = null;

  const strip = `<div class="days" data-keep-scroll="${id}-days" data-scroll-to="${p.day}">${days.map((d) => {
    const date = parse(d);
    return `<button class="day ${free[d] ? "" : "off"} ${d === todayKey() ? "today" : ""}"
      data-act="pick-day" data-picker="${id}" data-day="${d}" aria-pressed="${d === p.day}">
      <span>${DOW[date.getDay()]}</span><b>${date.getDate()}</b>
      <i class="dot ${free[d] ? "" : "none"}"></i></button>`;
  }).join("")}</div>`;

  const chips = starts.length
    ? `<div class="chips">${starts.map((t) => `
        <button class="chip" data-act="pick-time" data-picker="${id}" data-time="${t}"
          aria-pressed="${t === p.time}">${hhmm(t)}</button>`).join("")}</div>`
    : `<p class="hint">${client === me
        ? `You are not free on ${fmtDay(p.day)} for ${duration} minutes. Pick a day with a dot, or
           <button class="link" data-act="tab" data-tab="hours" style="min-height:0;padding:0">change your hours</button>.`
        : `${esc(nameOf(client))} is not free on ${fmtDay(p.day)} for ${duration} minutes.`}</p>`;

  return `${strip}<h2>${fmtDay(p.day)}</h2>${chips}`;
}

function pickedStart(id) {
  const p = ui.pickers[id];
  return p && p.day && p.time !== null ? `${p.day}T${hhmm(p.time)}` : null;
}

function clientBook() {
  const services = bookable();
  const hasHours = ((state.availability || {})[me] || []).length > 0;
  if (!ui.bookService || !services.some((s) => s.id === ui.bookService)) {
    // What they usually have, if anything; most people book the same thing.
    const usual = (state.appointments || []).filter((a) => a.client_id === me)
      .map((a) => a.service_id).reverse().find((id) => services.some((s) => s.id === id));
    // Otherwise the shortest: the plain session, not whatever sorts first.
    const shortest = services.slice().sort((a, b) => a.duration - b.duration)[0];
    ui.bookService = usual || (shortest ? shortest.id : null);
  }
  const service = serviceById(ui.bookService);

  let html = `<h1>Book</h1>
    <p class="lead">Choose what you need and the time that suits you best. The
      clinic will get you as close to it as it can, whenever you are free.</p>`;

  if (!hasHours) {
    return html + `<div class="card warn"><h3>Tell us when you are free first</h3>
      <p>Without your hours there is nowhere to put you.</p>
      <div class="stack"><button class="btn" data-act="tab" data-tab="hours">Set my hours</button></div></div>`;
  }
  if (!service) return html + empty("Nothing to book", "<p>The clinic has no services listed.</p>");

  html += `<h2>What</h2><div class="choices">${services.map((s) => `
    <button class="choice" data-act="book-service" data-id="${esc(s.id)}" aria-pressed="${s.id === ui.bookService}">
      <span><b>${esc(s.name)}</b><br><span class="meta">${s.duration} minutes</span></span>
      <span class="meta">${money(s.price)}</span>
    </button>`).join("")}</div>`;

  html += `<h2>When would suit you best?</h2>${picker("book", me, service.duration)}`;

  html += `<h2>How often</h2>${frequencyPicker(ui.bookEvery, "book-every")}`;
  if (ui.bookEvery) html += untilField("book-until");

  const start = pickedStart("book");
  const label = !start ? "Pick a time"
    : ui.bookEvery ? `Book ${everyWords(ui.bookEvery)} from ${fmtDay(start)}`
    : `Request ${fmtWhen(start)}`;
  html += `<div class="stack">
    <button class="btn block" data-act="book-submit" ${start ? "" : "disabled"}>${label}</button></div>
    <p class="hint">${ui.bookEvery
      ? `You won't need to ask again: each session is requested for you as it
         comes up, and you are offered a time for it like any booking.`
      : "This is a wish rather than a booking: you will be offered a time to accept."}</p>`;
  return html;
}

// -- the client: bookings ----------------------------------------------

function clientBookings() {
  const mine = (state.appointments || []).filter((a) => a.client_id === me)
    .sort((a, b) => a.start.localeCompare(b.start));
  const upcoming = mine.filter((a) => a.status === "booked" && !isPast(a.end));
  const done = mine.filter((a) => a.status !== "booked" || isPast(a.end));
  const waiting = (state.requests || []).filter((r) => r.client_id === me && openRequest(r));

  let html = `<h1>Bookings</h1>
    <button class="card tap" data-act="calendar-open">
      <div class="main"><div class="title">Put these in your calendar</div>
        <div class="meta">Google, Apple, Outlook — kept up to date when anything moves</div></div>
      <span class="chev">›</span></button>`;
  if (!upcoming.length && !waiting.length) {
    html += empty("Nothing coming up", "", `<button class="btn" data-act="tab" data-tab="book">Book an appointment</button>`);
  }
  if (upcoming.length) {
    html += `<h2>Coming up</h2><div class="list">${upcoming.map((a) => `
      <button class="row" data-act="booking" data-id="${a.id}">
        ${datebox(a.start)}
        <div class="main">
          <div class="title">${fmtTime(a.start)} · ${esc(a.service)}</div>
          <div class="meta">${relDay(a.start)}</div>
          <div class="tags">${bookingTag(a)}</div>
        </div><span class="chev">›</span>
      </button>`).join("")}</div>`;
  }
  const series = (state.series || []).filter((x) => x.client_id === me && x.status !== "stopped");
  if (series.length) {
    html += `<h2>Repeating</h2><div class="list">${series.map((x) => `
      <div class="row"><div class="main">
        <div class="title">${esc(x.service)}</div>
        <div class="meta">${seriesLine(x)}</div>
        <div class="meta">${seriesMeta(x)}</div></div>
        ${x.status === "active" ? `<button class="btn small danger" data-act="series-stop" data-id="${x.id}">Stop</button>` : ""}
      </div>`).join("")}</div>`;
  }
  if (waiting.length) html += `<h2>Requested</h2><div class="list">${waiting.map(requestRow).join("")}</div>`;
  const due = chargesOf(me, "due"), paid = chargesOf(me, "paid");
  if (due.length || paid.length) {
    html += `<h2>Payments</h2><div class="list">${[
      ...due.map((c) => payRow(c, `<button class="btn small" data-act="pay" data-id="${c.id}">Pay</button>`)),
      ...paid.slice().reverse().map((c) => payRow(c, `<span class="tag ok">paid</span>`)),
    ].join("")}</div>`;
  }
  if (done.length) {
    html += `<h2>Earlier and cancelled</h2><div class="list">${done.map((a) => `
      <div class="row dim">${datebox(a.start)}
        <div class="main"><div class="title">${fmtTime(a.start)} · ${esc(a.service)}</div>
          <div class="meta">${a.status.replace(/_/g, " ")}</div></div></div>`).join("")}</div>`;
  }
  return html;
}

function bookingSheet(id) {
  const a = appointmentById(id);
  if (!a) return `<h3>Gone</h3><p>This booking no longer exists.</p>`;
  const p = a.pending;
  const facts = [
    ["When", `${fmtDay(a.start)}, ${fmtTime(a.start)}–${fmtTime(a.end)}`],
    ["What", esc(a.service)],
    ["Price", money(a.price)],
  ];
  if (a.preferred && a.preferred !== a.start) facts.push(["You asked for", fmtWhen(a.preferred)]);
  if (a.origin === "displaced") facts.push(["Note", "The clinic moved this one"]);
  const status = p ? {
    asked: "The clinic has asked whether you could move. Answer on the Home tab.",
    agreed: `You agreed to move to ${fmtWhen(p.now)}. It changes when the clinic confirms.`,
    moving: "You asked to move this. You keep it until somewhere else is found.",
  }[p.state] : "Confirmed.";

  return `<h3>${esc(a.service)}</h3>
    <div class="when">${relDay(a.start)}, ${fmtTime(a.start)}</div>
    ${factsHtml(facts)}
    <p class="hint" style="margin:0 0 8px">${status}</p>
    <div class="stack">
      ${p ? "" : `<button class="btn secondary" data-act="move-open" data-id="${a.id}">Ask to move</button>`}
      <button class="btn danger" data-act="cancel-booking" data-id="${a.id}">Cancel booking</button>
      <button class="btn ghost" data-act="close">Close</button>
    </div>`;
}

function moveSheet(id) {
  const a = appointmentById(id);
  if (!a) return `<h3>Gone</h3>`;
  const duration = Math.round((parse(a.end) - parse(a.start)) / 60000);
  const start = pickedStart(`move-${id}`);
  const release = ui.pickers[`move-${id}`]?.release || false;
  return `<h3>Move your ${esc(a.service)}</h3>
    <p class="hint" style="margin:0 0 8px">Now ${fmtWhen(a.start)}. When would you rather come?</p>
    ${picker(`move-${id}`, a.client_id, duration)}
    <h2>Meanwhile</h2>
    <div class="segmented">
      <button data-act="move-release" data-id="${id}" data-on="0" aria-pressed="${!release}">Keep my slot</button>
      <button data-act="move-release" data-id="${id}" data-on="1" aria-pressed="${release}">Give it up now</button>
    </div>
    <p class="hint">${release
      ? "Your current time frees up for someone else straight away — but if nothing else can be found, you will have no appointment."
      : "You keep your current time until there is somewhere to move you. Safer, but it stays blocked meanwhile."}</p>
    <div class="stack">
      <button class="btn block" data-act="move-submit" data-id="${id}" ${start ? "" : "disabled"}>
        ${start ? `Ask for ${fmtWhen(start)}` : "Pick a time"}</button>
      <button class="btn ghost" data-act="close">Never mind</button>
    </div>`;
}

function factsHtml(facts) {
  return `<dl class="facts">${facts.map(([k, v]) => `<dt>${k}</dt><dd>${v}</dd>`).join("")}</dl>`;
}

// -- weekly hours and single dates (client and provider alike) --------

function hoursView() {
  const weekly = (state.weekly || {})[whose()] || [];
  const exceptions = ((state.exceptions || {})[whose()] || []).filter((e) => e.date >= todayKey());
  const mine = isProvider();

  let html = `${mine ? backLink() : ""}<h1>${mine ? "Working hours" : "My hours"}</h1>
    <p class="lead">${mine
      ? "Your normal week. Clients can only be booked inside it."
      : "When you can usually come in. You will only ever be offered times inside these."}</p>
    <h2>Every week</h2><div class="list">`;
  for (let d = 0; d < 7; d++) {
    const ranges = weekly.map((r, i) => ({...r, i})).filter((r) => r.weekday === d)
      .sort((a, b) => a.from.localeCompare(b.from));
    html += `<div class="weekday"><div class="name">${WEEKDAYS[d]}</div><div class="ranges">
      ${ranges.length ? ranges.map((r) => `<span class="range-pill">${r.from}–${r.to}
          <button data-act="range-remove" data-index="${r.i}" aria-label="Remove ${WEEKDAYS[d]} ${r.from} to ${r.to}">×</button>
        </span>`).join("") : `<span class="closed">${mine ? "Closed" : "Not free"}</span>`}
      <button class="add-range" data-act="range-add" data-weekday="${d}">+ Add</button>
    </div></div>`;
  }
  html += `</div>`;

  html += `<h2>Just one date</h2>`;
  html += exceptions.length ? `<div class="list">${exceptions.map((e) => `
    <div class="row">${datebox(e.date)}
      <div class="main"><div class="title">${e.kind === "remove" ? "Away" : "Free"} ${e.from}–${e.to}</div>
        <div class="meta">${fmtDay(e.date)} only</div></div>
      <button class="btn small danger" data-act="exception-clear" data-date="${e.date}"
        data-from="${e.from}" data-to="${e.to}" data-kind="${e.kind}">Remove</button>
    </div>`).join("")}</div>` : `<p class="hint">Nothing different from usual.</p>`;
  html += `<div class="stack"><button class="btn secondary" data-act="exception-add">Change one date</button></div>`;
  return html;
}

function timeOptions(selected, from = 6 * 60, to = 22 * 60) {
  let out = "";
  for (let t = from; t <= to; t += 30) {
    out += `<option value="${hhmm(t)}" ${hhmm(t) === selected ? "selected" : ""}>${hhmm(t)}</option>`;
  }
  return out;
}

function rangeSheet(weekday) {
  const existing = ((state.weekly || {})[whose()] || []).filter((r) => r.weekday === weekday);
  const last = existing.map((r) => r.to).sort().pop();
  const from = last && toMin(last) <= 20 * 60 ? hhmm(toMin(last) + 60) : "09:00";
  const to = hhmm(Math.min(toMin(from) + 4 * 60, 22 * 60));
  return `<h3>${WEEKDAY_NAMES[weekday]}s</h3>
    <form data-form="range" data-weekday="${weekday}">
      <div class="field-row">
        <label class="field"><span>From</span><select name="from">${timeOptions(from)}</select></label>
        <label class="field"><span>Until</span><select name="to">${timeOptions(to)}</select></label>
      </div>
      <div class="stack"><button class="btn block" type="submit">Add</button>
        <button class="btn ghost" type="button" data-act="close">Cancel</button></div>
    </form>`;
}

function exceptionSheet(presetDate) {
  const available = ui.exceptionAvailable;
  return `<h3>Change one date</h3>
    <p class="hint" style="margin:0 0 10px">Only that day changes. Your normal week stays as it is.</p>
    <form data-form="exception">
      <div class="segmented" style="margin-bottom:12px">
        <button type="button" data-act="exception-kind" data-on="0" aria-pressed="${!available}">I'm away</button>
        <button type="button" data-act="exception-kind" data-on="1" aria-pressed="${available}">I'm free</button>
      </div>
      <label class="field"><span>Date</span>
        <input type="date" name="date" required min="${todayKey()}" value="${presetDate || todayKey()}"></label>
      <div class="field-row">
        <label class="field"><span>From</span><select name="from">${timeOptions("09:00", 0, 23 * 60 + 30)}</select></label>
        <label class="field"><span>Until</span><select name="to">${timeOptions("17:00", 0, 23 * 60 + 30)}</select></label>
      </div>
      ${isProvider() && !available ? `<p class="hint" style="margin-top:0">Bookings already in that
        time stay put and are flagged on Home. For more than a day, use <b>Time off</b> in More.</p>` : ""}
      <div class="stack"><button class="btn block" type="submit">Save</button>
        <button class="btn ghost" type="button" data-act="close">Cancel</button></div>
    </form>`;
}

// -- the clinician chat ----------------------------------------------

function bubbles(messages, mineAuthor) {
  if (!messages.length) return `<p class="hint" style="text-align:center">No messages yet.</p>`;
  return `<div class="bubbles">${messages.map((m) => `
    <div class="bubble ${m.author === mineAuthor ? "mine" : "theirs"}">
      <div>${esc(m.text).replace(/\n/g, "<br>")}</div>
      <div class="bubble-meta">${fmtWhen(m.at)}${m.author === mineAuthor && m.read ? " · read" : ""}</div>
    </div>`).join("")}</div>`;
}

function composer(placeholder) {
  return `<form data-form="message" class="composer">
    <textarea name="text" rows="2" maxlength="4000" placeholder="${esc(placeholder)}"></textarea>
    <button class="btn" type="submit">Send</button></form>`;
}

function clientMessages() {
  const convo = chat.mine;
  if (!convo) return `<h1>Messages</h1>${empty("Not available", "<p>Messaging is not offered right now.</p>")}`;
  const settings = convo.settings || {};
  if (!settings.enabled) {
    return `<h1>Messages</h1>${empty("Messaging is switched off", `<p>${esc(settings.emergency_note || "")}</p>`)}`;
  }
  return `<h1>Messages</h1>
    <p class="lead">Write to your clinician. ${esc(settings.reply_expectation || "")}</p>
    <div class="card warn" style="font-size:14px">${esc(settings.emergency_note || "")}</div>
    ${bubbles(convo.messages, "client")}
    ${composer("Write a message")}
    <p class="hint">Only you and your clinician see this conversation. It never goes to an AI assistant.</p>`;
}

function messagesView() {
  const open = openThread();
  if (open) {
    const convo = chat.thread[open];
    return `<button class="link" data-act="tab" data-tab="more/messages" style="margin:-8px 0 0 -4px">‹ Messages</button>
      <h1>${esc(nameOf(open))}</h1>
      ${convo ? bubbles(convo.messages, "provider") : `<p class="hint">Loading…</p>`}
      ${composer(`Reply to ${nameOf(open)}`)}`;
  }
  const c = state.chat || {};
  return `${backLink()}<h1>Messages</h1>
    ${chat.summary.length ? `<div class="list">${chat.summary.map((t) => `
      <button class="row" data-act="tab" data-tab="more/messages/${esc(t.client_id)}">
        <span class="avatar">${esc(t.name.slice(0, 1).toUpperCase())}</span>
        <div class="main"><div class="title">${esc(t.name)}${t.unread ? ` <span class="tag accent">${t.unread} new</span>` : ""}</div>
          <div class="meta">${t.last_author === "provider" ? "You: " : ""}${esc(t.preview)}</div></div>
        <span class="meta">${relDay(t.last_at)}</span></button>`).join("")}</div>`
      : empty("No conversations yet", "<p>Clients can write to you from the Messages tab in their app.</p>")}
    <h2>What clients are told</h2>
    <form data-form="chat-settings" class="card">
      <label class="row" style="padding:0;min-height:44px;border:0">
        <span class="main"><span class="title">Offer messaging</span></span>
        <input type="checkbox" name="enabled" ${c.enabled !== false ? "checked" : ""}></label>
      <label class="field"><span>When to expect a reply</span>
        <input name="reply" value="${esc(c.reply_expectation || "")}" autocomplete="off"></label>
      <label class="field"><span>Where to get urgent help</span>
        <textarea name="emergency" rows="3">${esc(c.emergency_note || "")}</textarea></label>
      <p class="hint" style="margin-top:-4px">Shown above every client's conversation. Confirm the numbers and wording are right for your practice.</p>
      <button class="btn secondary block" type="submit">Save</button>
    </form>`;
}

// -- the provider: home ------------------------------------------------

const stranded = () => (state.appointments || []).filter((a) => a.orphaned && a.status === "booked");

function providerHome() {
  const drafts = (state.plans || []).filter((p) => p.status === "draft");
  const out = (state.plans || []).filter((p) => p.status === "awaiting_clients" || p.status === "answered");
  const asks = (state.approvals || []).filter((a) => out.some((p) => p.id === a.plan_id) && !a.applied);
  const agreed = asks.filter((a) => a.status === "accepted"
    && !blockedBy(planById(a.plan_id), a, (state.approvals || []).filter((x) => x.plan_id === a.plan_id)).length).length;
  const waitingOn = asks.filter((a) => a.status === "pending").length;
  const queue = (state.requests || []).filter(openRequest);
  const stuck = stranded();

  let html = `${installTip()}<h1>${fmtDay(todayKey())}</h1>`;
  const cards = [];
  if (stuck.length) {
    const idle = stuck.filter((a) => !a.pending).length;
    cards.push(`<button class="card warn tap" data-act="stranded">
      <div class="main"><span class="tag warn">needs a decision</span>
        <h3 style="margin-top:6px">${plural(stuck.length, "booking is", "bookings are")} in time you're not working</h3>
        <p class="meta">${idle ? "Nothing has been done about them yet." : "Already being moved."}</p></div>
      <span class="chev">›</span></button>`);
  }
  const unreadThreads = chat.summary.filter((t) => t.unread);
  if (unreadThreads.length) {
    cards.push(`<button class="card attention tap" data-act="tab" data-tab="${
      unreadThreads.length === 1 ? `more/messages/${esc(unreadThreads[0].client_id)}` : "more/messages"}">
      <div class="main"><span class="tag accent">messages</span>
        <h3 style="margin-top:6px">${plural(unreadThreads.reduce((n, t) => n + t.unread, 0), "new message")}</h3>
        <p class="meta">From ${unreadThreads.slice(0, 3).map((t) => esc(t.name)).join(", ")}</p></div>
      <span class="chev">›</span></button>`);
  }
  const fees = chargesOf(null, "proposed");
  if (fees.length) {
    cards.push(`<button class="card warn tap" data-act="more-open" data-page="payments">
      <div class="main"><span class="tag warn">your decision</span>
        <h3 style="margin-top:6px">${plural(fees.length, "fee")} to confirm or waive</h3>
        <p class="meta">${fees.slice(0, 3).map((c) => esc(nameOf(c.client_id))).join(", ")} — nothing is charged until you confirm.</p></div>
      <span class="chev">›</span></button>`);
  }
  if (drafts.length) {
    cards.push(`<button class="card attention tap" data-act="tab" data-tab="plans">
      <div class="main"><span class="tag accent">to review</span>
        <h3 style="margin-top:6px">${drafts.length === 1 ? "A new schedule is ready" : `${drafts.length} schedules to compare`}</h3>
        <p class="meta">Nobody has been asked anything yet.</p></div>
      <span class="chev">›</span></button>`);
  }
  if (out.length) {
    cards.push(`<button class="card ${agreed ? "attention" : ""} tap" data-act="tab" data-tab="plans">
      <div class="main"><span class="tag ${agreed ? "ok" : ""}">${agreed ? "ready to confirm" : "with clients"}</span>
        <h3 style="margin-top:6px">${agreed} ready · ${waitingOn} waiting</h3>
        <p class="meta">${agreed ? "Confirm to put them in the diary." : "Waiting for clients to answer."}</p></div>
      <span class="chev">›</span></button>`);
  }
  if (queue.length) {
    cards.push(`<button class="card tap" data-act="tab" data-tab="queue">
      <div class="main"><h3>${plural(queue.length, "request")} waiting</h3>
        <p class="meta">${queue.slice(0, 3).map((r) => esc(nameOf(r.client_id))).join(", ")}${queue.length > 3 ? "…" : ""}</p></div>
      <span class="chev">›</span></button>`);
  }
  html += cards.length ? `<h2>Needs you</h2>${cards.join("")}`
                       : `<p class="lead">Nothing needs you right now.</p>`;

  // Today if there is anything left of it, otherwise the next day with work.
  const live = (state.appointments || []).filter((a) => a.status === "booked" && !isPast(a.end))
    .sort((a, b) => a.start.localeCompare(b.start));
  if (live.length) {
    const day = live[0].start.slice(0, 10);
    const that = live.filter((a) => a.start.startsWith(day));
    html += `<h2>${day === todayKey() ? "Still to come today" : `Next working day · ${relDay(day)}`}</h2>
      <div class="list">${that.map(diaryRow).join("")}</div>
      <div class="stack"><button class="btn secondary" data-act="diary-open" data-day="${day}">Open the diary</button></div>`;
  } else {
    html += empty("Nothing booked", "<p>The next four weeks are empty.</p>");
  }
  return html;
}

function diaryRow(a) {
  const self = a.client_id === PROVIDER;
  return `<button class="row" data-act="slot" data-id="${a.id}">
    <div class="datebox"><b style="font-size:15px">${fmtTime(a.start)}</b><span>${fmtTime(a.end)}</span></div>
    <div class="main"><div class="title">${self ? esc(a.service) : esc(nameOf(a.client_id))}</div>
      <div class="meta">${self ? "your own time" : esc(a.service)}</div>
      ${slotTags(a)}</div>
    <span class="chev">›</span></button>`;
}

function slotTags(a) {
  const tags = [];
  if (a.orphaned) tags.push(`<span class="tag warn">you're not working</span>`);
  if (a.pending && a.pending.state === "asked") tags.push(`<span class="tag accent">asked to move</span>`);
  if (a.pending && a.pending.state === "agreed") tags.push(`<span class="tag held">move agreed</span>`);
  if (a.pending && a.pending.state === "moving") tags.push(`<span class="tag move">wants to move</span>`);
  if (a.origin === "displaced") tags.push(`<span class="tag move">moved by you</span>`);
  return tags.length ? `<div class="tags">${tags.join("")}</div>` : "";
}

function strandedSheet() {
  const stuck = stranded();
  const idle = stuck.filter((a) => !a.pending);
  return `<h3>Bookings in time you're not working</h3>
    <p class="hint" style="margin:0 0 8px">They are still in the diary and still the
      clients'. Nothing happens to them until you decide.</p>
    <div class="list">${stuck.map((a) => `<div class="row">${datebox(a.start)}
      <div class="main"><div class="title">${esc(nameOf(a.client_id))} · ${fmtTime(a.start)}</div>
        <div class="meta">${esc(a.service)}</div></div>
      <span class="tag ${a.pending ? "move" : "warn"}">${a.pending ? "being moved" : "stuck"}</span></div>`).join("")}</div>
    <div class="stack">
      <button class="btn" data-act="stranded-fix" data-how="rehouse" ${idle.length ? "" : "disabled"}>
        Find ${idle.length === 1 ? "it" : `the ${idle.length}`} a new time</button>
      <button class="btn danger" data-act="stranded-fix" data-how="cancel" ${idle.length ? "" : "disabled"}>Cancel them</button>
      <button class="btn ghost" data-act="close">Not now</button>
    </div>
    <p class="hint">Finding a new time keeps each client's hour until there is
      somewhere to move it, and goes through the queue — they are asked, not told.</p>`;
}

// -- the provider: diary -----------------------------------------------

const PX_PER_MIN = 1.15;

function diaryView() {
  const days = windowDays();
  if (!ui.diaryDay || !days.includes(ui.diaryDay)) ui.diaryDay = todayKey();
  const key = ui.diaryDay;
  const busy = new Set((state.appointments || []).filter((a) => a.status === "booked")
    .map((a) => a.start.slice(0, 10)));

  let html = `<h1>Diary</h1>
    <div class="days" data-keep-scroll="diary-days" data-scroll-to="${key}">${days.map((d) => {
      const date = parse(d);
      const working = segmentsOn((state.availability || {})[PROVIDER], d).length;
      return `<button class="day ${working ? "" : "off"} ${d === todayKey() ? "today" : ""}" data-act="diary-day"
        data-day="${d}" aria-pressed="${d === key}"><span>${DOW[date.getDay()]}</span><b>${date.getDate()}</b>
        <i class="dot ${busy.has(d) ? "" : "none"}"></i></button>`;
    }).join("")}</div>
    <h2>${relDay(key)}${relDay(key) !== fmtDay(key) ? ` · ${fmtDay(key)}` : ""}</h2>`;

  html += timeline(key);

  const cancelled = (state.appointments || []).filter(
    (a) => a.start.startsWith(key) && a.status.startsWith("cancelled"));
  if (cancelled.length) {
    html += `<h2>Cancelled</h2><div class="list">${cancelled.map((a) => `
      <div class="row dim"><div class="main"><div class="title">${esc(nameOf(a.client_id))} · ${fmtTime(a.start)}</div>
        <div class="meta">${a.status.replace(/_/g, " ")}</div></div></div>`).join("")}</div>`;
  }
  html += `<div class="pair">
    <button class="btn secondary" data-act="exception-add" data-day="${key}">Change this day</button>
    <button class="btn secondary" data-act="more-open" data-page="away" data-day="${key}">Time off</button>
  </div>`;
  return html;
}

function timeline(key) {
  const open = segmentsOn((state.availability || {})[PROVIDER], key);
  const booked = (state.appointments || []).filter((a) => a.status === "booked" && a.start.startsWith(key));
  const offered = (state.approvals || []).filter((a) => unsettled(a) && a.now.startsWith(key));

  const items = [
    ...booked.map((a) => ({
      start: minOf(a.start), end: minOf(a.end),
      cls: [a.client_id === PROVIDER ? "self" : "",
            a.origin === "displaced" ? "moved" : "",
            a.pending && a.pending.state !== "moving" ? "asked" : "",
            a.orphaned ? "stranded" : "", isPast(a.end) ? "past" : ""].join(" "),
      act: `data-act="slot" data-id="${a.id}"`,
      title: a.client_id === PROVIDER ? a.service : nameOf(a.client_id),
      sub: `${fmtTime(a.start)}–${fmtTime(a.end)} · ${a.client_id === PROVIDER ? "your own time" : a.service}`,
    })),
    // Offered but not written down: drawn dashed, so it reads as a question
    // rather than a booking, and solid-edged once the client has said yes.
    ...offered.map((a) => ({
      start: minOf(a.now), end: minOf(a.now_end),
      cls: `offered ${a.status === "accepted" ? "agreed" : ""}`,
      act: `data-act="offer" data-id="${a.id}"`,
      title: nameOf(a.client_id),
      sub: `${a.status === "accepted" ? "agreed" : "offered"}${a.kind === "reschedule" ? " · moving here" : ""}`,
    })),
  ];

  if (!open.length && !items.length) {
    return empty("Not working", `<p>You have no hours on ${fmtDay(key)}.</p>`);
  }

  const edges = [...open.flatMap((s) => [s.from, s.to]), ...items.flatMap((i) => [i.start, i.end])];
  const from = Math.floor(Math.min(...edges) / 60) * 60;
  const to = Math.ceil(Math.max(...edges) / 60) * 60;
  const y = (m) => (m - from) * PX_PER_MIN;

  // Overlapping items share the width rather than hiding each other — an
  // offered slot usually sits exactly on the booking it would replace.
  items.sort((a, b) => a.start - b.start || b.end - a.end);
  let cluster = [], clusterEnd = -1;
  const flush = () => {
    const lanes = [];
    for (const it of cluster) {
      let lane = lanes.findIndex((end) => end <= it.start);
      if (lane < 0) { lane = lanes.length; lanes.push(0); }
      lanes[lane] = it.end; it.lane = lane;
    }
    for (const it of cluster) it.lanes = lanes.length;
    cluster = [];
  };
  for (const it of items) {
    if (it.start >= clusterEnd) { flush(); clusterEnd = it.end; }
    cluster.push(it); clusterEnd = Math.max(clusterEnd, it.end);
  }
  flush();

  let html = `<div class="card" style="padding:12px 8px 12px 0"><div class="timeline" style="height:${y(to)}px">`;
  html += open.map((s) => `<div class="open" style="top:${y(s.from)}px;height:${y(s.to) - y(s.from)}px"></div>`).join("");
  for (let h = from; h <= to; h += 60) {
    html += `<div class="hourline" style="top:${y(h)}px"></div><div class="hour" style="top:${y(h)}px">${hhmm(h)}</div>`;
  }
  if (key === todayKey() && nowMinutes() > from && nowMinutes() < to) {
    html += `<div class="now" style="top:${y(nowMinutes())}px"></div>`;
  }
  html += items.map((it) => {
    const w = `(100% - 12px) / ${it.lanes}`;
    const short = it.end - it.start < 45;
    return `<button class="slot ${it.cls}" ${it.act}
      style="top:${y(it.start) + 1}px;height:${y(it.end) - y(it.start) - 2}px;left:calc(6px + ${w} * ${it.lane});width:calc(${w} - 4px);right:auto">
      <b>${esc(it.title)}</b>${short ? "" : esc(it.sub)}</button>`;
  }).join("");
  return html + `</div></div>`;
}

function slotSheet(id) {
  const a = appointmentById(id);
  if (!a) return `<h3>Gone</h3>`;
  const self = a.client_id === PROVIDER;
  const c = clientById(a.client_id);
  const facts = [
    ["When", `${fmtDay(a.start)}, ${fmtTime(a.start)}–${fmtTime(a.end)}`],
    ["What", esc(a.service)],
  ];
  if (!self) facts.push(["Price", money(a.price)]);
  if (a.preferred && a.preferred !== a.start) facts.push(["Asked for", fmtWhen(a.preferred)]);
  if (a.origin === "displaced") facts.push(["Note", "Moved by you, not chosen"]);
  if (a.pending && a.pending.now) facts.push([a.pending.state === "agreed" ? "Agreed move" : "Asked to move", fmtWhen(a.pending.now)]);
  if (a.orphaned) facts.push(["Warning", "You are not working at this time"]);
  const sessionCharge = chargeForAppointment(a.id);
  if (sessionCharge) facts.push(["Payment", `${sgd(sessionCharge.amount)} — ${sessionCharge.status}`]);
  const fee = chargeForAppointment(a.id, "no_show") || chargeForAppointment(a.id, "late_cancel");
  if (fee) facts.push(["Fee", `${sgd(fee.amount)} — ${fee.status === "proposed" ? "to decide" : fee.status}`]);
  if (c) facts.push(["History", `${c.completed} attended · ${c.no_show} no-show · ${c.cancelled} cancelled · ${c.moved_by_us} moved by you`]);
  const started = isPast(a.start) && a.status === "booked" && !self;

  return `<h3>${self ? esc(a.service) : esc(nameOf(a.client_id))}</h3>
    ${factsHtml(facts)}
    <div class="stack">
      ${started ? `<div class="pair" style="margin-top:0">
        <button class="btn ok" data-act="attendance" data-id="${a.id}" data-attended="1">Attended</button>
        <button class="btn secondary" data-act="attendance" data-id="${a.id}" data-attended="0">Didn't show</button></div>` : ""}
      ${!self ? `<button class="btn secondary" data-act="view-as" data-who="${esc(a.client_id)}">See ${esc(nameOf(a.client_id))}'s view</button>` : ""}
      ${a.status === "booked" ? `<button class="btn danger" data-act="provider-cancel" data-id="${a.id}">${self ? "Remove" : "Cancel appointment"}</button>` : ""}
      <button class="btn ghost" data-act="close">Close</button>
    </div>`;
}

function offerSheet(id) {
  const a = (state.approvals || []).find((x) => x.id === id);
  if (!a) return `<h3>Gone</h3>`;
  const facts = [
    ["Client", esc(nameOf(a.client_id))],
    ["What", esc(approvalService(a))],
    [a.kind === "reschedule" ? "Moving to" : "Offered", fmtWhen(a.now)],
  ];
  if (a.was) facts.push(["Currently", fmtWhen(a.was)]);
  facts.push(["Status", a.status === "accepted" ? "Agreed — confirm it in Plans" : "Waiting for the client"]);
  return `<h3>${a.kind === "reschedule" ? "Proposed move" : "Offered booking"}</h3>${factsHtml(facts)}
    <div class="stack"><button class="btn secondary" data-act="tab" data-tab="plans">Go to Plans</button>
      <button class="btn ghost" data-act="close">Close</button></div>`;
}

// -- the provider: requests --------------------------------------------

function planningEnd() {
  return addDays(todayKey(), (state.scheduler || {}).horizon_days || 7);
}

function queueView() {
  const open = (state.requests || []).filter(openRequest)
    .sort((a, b) => a.preferred.localeCompare(b.preferred));
  const out = (state.requests || []).filter((r) => r.status === "awaiting_client");
  const end = planningEnd();
  const inWindow = (r) => r.preferred.slice(0, 10) < end;
  const scoped = (state.scheduler || {}).scope_to_horizon !== false;

  if (ui.alpha === null) ui.alpha = state.settings.alpha;
  if (ui.moves === null) ui.moves = state.settings.max_displacements;
  // Default the ticks to what the standing rule would do, so the screen shows
  // what is about to happen rather than an empty selection. A request that
  // arrives while the screen is open gets the same default; one somebody has
  // already ticked or unticked keeps their choice.
  if (ui.runOver === null) { ui.runOver = new Set(); ui.runSeen = new Set(); }
  for (const r of open) {
    if (ui.runSeen.has(r.id)) continue;
    ui.runSeen.add(r.id);
    if (!scoped || inWindow(r)) ui.runOver.add(r.id);
  }
  for (const id of [...ui.runOver]) if (!open.some((r) => r.id === id)) ui.runOver.delete(id);
  for (const id of [...ui.runSeen]) if (!open.some((r) => r.id === id)) ui.runSeen.delete(id);

  let html = `<h1>Requests</h1>`;
  if (!open.length) {
    html += empty("Nothing waiting", "<p>New requests from clients appear here.</p>");
  } else {
    html += `<p class="lead">Tick what the next schedule should cover.</p>
      <div class="list">${open.map((r) => `
      <label class="row ${inWindow(r) ? "" : "dim"}">
        <input type="checkbox" data-act="pick-request" data-id="${r.id}" ${ui.runOver.has(r.id) ? "checked" : ""}>
        <div class="main"><div class="title">${esc(nameOf(r.client_id))} · ${esc(r.service)}</div>
          <div class="meta">wants ${fmtWhen(r.preferred)}</div>
          <div class="tags">${r.status === "on_hold" ? `<span class="tag warn">couldn't place last time</span>` : ""}
            ${inWindow(r) ? "" : `<span class="tag">beyond ${(state.scheduler || {}).horizon_days} days</span>`}
            ${r.replaces ? `<span class="tag move">moving an existing booking</span>` : ""}
            ${r.series_id ? `<span class="tag accent">${esc(r.note || "repeating")}</span>` : ""}</div></div>
        <button class="btn small secondary" data-act="place-open" data-id="${r.id}">Place</button>
      </label>`).join("")}</div>
      <div class="pair">
        <button class="btn small ghost" data-act="pick-window">Next ${(state.scheduler || {}).horizon_days} days</button>
        <button class="btn small ghost" data-act="pick-all">All</button>
      </div>`;

    html += `<h2>How to plan</h2><div class="card">
      <label class="field" style="margin:0"><span>Priority</span>
        <input type="range" min="0" max="1" step="0.05" value="${ui.alpha}" data-act="alpha" aria-label="Priority"></label>
      <div class="range-ends"><span>Times people asked for</span><span>No unusable gaps</span></div>
      <p class="hint" style="margin:10px 0 4px">Move people already booked, at most</p>
      <div class="segmented">${[0, 1, 2, 3].map((n) => `
        <button data-act="moves" data-n="${n}" aria-pressed="${ui.moves === n}">${n === 0 ? "Nobody" : n}</button>`).join("")}</div>
    </div>
    <button class="btn block" data-act="run" ${ui.runOver.size ? "" : "disabled"}>
      Make a schedule for ${plural(ui.runOver.size, "request")}</button>
    <p class="hint">Nothing is booked or sent yet: you get a proposal to look at first.</p>`;
  }

  if (out.length) {
    html += `<h2>Out with clients</h2><div class="list">${out.map((r) => `
      <div class="row"><div class="main"><div class="title">${esc(nameOf(r.client_id))} · ${esc(r.service)}</div>
        <div class="meta">wanted ${fmtWhen(r.preferred)}</div></div><span class="tag accent">asked</span></div>`).join("")}</div>`;
  }
  return html;
}

function placeSheet(id) {
  const r = requestById(id);
  if (!r) return `<h3>Gone</h3>`;
  const start = pickedStart(`place-${id}`);
  return `<h3>Book ${esc(nameOf(r.client_id))} yourself</h3>
    <p class="hint" style="margin:0 0 8px">${esc(r.service)} · wanted ${fmtWhen(r.preferred)}.
      Showing when ${esc(nameOf(r.client_id))} is free.</p>
    ${picker(`place-${id}`, r.client_id, r.duration)}
    <div class="stack">
      <button class="btn block" data-act="place-submit" data-id="${id}" ${start ? "" : "disabled"}>
        ${start ? `Book ${fmtWhen(start)}` : "Pick a time"}</button>
      <button class="btn ghost" data-act="close">Cancel</button>
    </div>
    <p class="hint">Locked, so the next schedule plans around it instead of moving it.</p>`;
}

// -- the provider: plans -----------------------------------------------

const describeAlpha = (alpha) => (alpha <= 0.3 ? "favours the times people asked for"
  : alpha >= 0.7 ? "favours a diary without gaps" : "balanced");

function plansView() {
  const drafts = (state.plans || []).filter((p) => p.status === "draft");
  const out = (state.plans || []).filter((p) => p.status === "awaiting_clients" || p.status === "answered");
  let html = `<h1>Plans</h1>`;
  if (!drafts.length && !out.length) {
    return html + empty("Nothing to review",
      "<p>Make a schedule from the waiting requests and it appears here first.</p>",
      `<button class="btn" data-act="tab" data-tab="queue">Go to Requests</button>`);
  }
  if (out.length) html += `<h2>With clients</h2>${out.map(settlementCard).join("")}`;
  if (drafts.length) {
    html += `<h2>${drafts.length === 1 ? "Proposed" : `${drafts.length} options to compare`}</h2>
      ${drafts.length > 1 ? `<p class="hint">Same requests, planned with different priorities. Approve the one you prefer.</p>` : ""}
      ${drafts.map((p, i) => draftCard(p, drafts.length > 1 ? `Option ${"ABCDEFGH"[i] || i + 1}` : "")).join("")}`;
  }
  return html;
}

function ticked(plan) {
  const picks = ui.picked[plan.id] || {};
  const items = [...plan.placements, ...plan.displacements];
  const on = new Set(items.filter((x) => picks[x.key] !== false).map((x) => x.key));
  // A part that rests on a move cannot go ahead without it: ticking the
  // dependent forces its prerequisites on.
  const forced = new Set();
  let grew = true;
  while (grew) {
    grew = false;
    for (const x of items) {
      if (!on.has(x.key) && !forced.has(x.key)) continue;
      for (const need of x.depends_on || []) {
        if (!forced.has(need)) { forced.add(need); grew = true; }
      }
    }
  }
  return {on: new Set([...on, ...forced]), forced};
}

function draftCard(plan, label) {
  const m = plan.metrics || {}, q = plan.params || {};
  const {on, forced} = ticked(plan);
  const byKey = Object.fromEntries(plan.displacements.map((d) => [d.key, d]));
  const because = (x) => (x.depends_on || []).map((k) => byKey[k] ? nameOf(byKey[k].client_id) : k);

  const change = (x, move) => {
    const needs = because(x);
    return `<label class="change">
      <input type="checkbox" data-act="tick" data-plan="${plan.id}" data-key="${x.key}"
        ${on.has(x.key) ? "checked" : ""} ${forced.has(x.key) ? "disabled" : ""}>
      <div style="flex:1"><div><span class="tag ${move ? "move" : "ok"}">${move ? "move" : "book"}</span>
        <b>${esc(nameOf(x.client_id))}</b></div>
        <div class="meta">${move ? `${fmtWhen(x.was)} → <b>${fmtWhen(x.now)}</b>` : `${esc(x.service)} · <b>${fmtWhen(x.start)}</b>`}</div>
        ${needs.length ? `<div class="because">Only if ${needs.map(esc).join(" and ")} move${needs.length > 1 ? "" : "s"} first</div>` : ""}
        ${forced.has(x.key) ? `<div class="meta">Needed by something you've ticked</div>` : ""}
      </div></label>`;
  };

  return `<div class="card">
    ${label ? `<span class="tag accent">${label}</span>` : ""}
    <h3 style="margin-top:${label ? 8 : 0}px">${describeAlpha(q.alpha)[0].toUpperCase() + describeAlpha(q.alpha).slice(1)}</h3>
    <p class="meta">priority ${q.alpha} · up to ${plural(q.max_displacements, "move")}</p>
    <div class="metrics">
      <div class="metric"><b>${m.placed}</b><span>booked</span></div>
      <div class="metric"><b>${m.displacements || 0}</b><span>moved</span></div>
      <div class="metric"><b>${m.fragmentation_minutes}m</b><span>unusable</span></div>
      <div class="metric"><b>${hoursOff(m.preference_gap_minutes)}</b><span>off-wish</span></div>
    </div>
    ${m.unplaced ? `<p class="because">${plural(m.unplaced, "request")} could not be fitted in.</p>` : ""}
    ${planStrips(plan)}
    <div style="margin-top:8px">${plan.placements.map((x) => change(x, false)).join("")}
      ${plan.displacements.map((x) => change(x, true)).join("")}</div>
    <div class="stack">
      <button class="btn block" data-act="approve" data-id="${plan.id}" ${on.size ? "" : "disabled"}>
        Ask ${plural(new Set([...plan.placements, ...plan.displacements].filter((x) => on.has(x.key)).map((x) => x.client_id)).size, "client")}</button>
      <div class="pair" style="margin-top:0">
        <button class="btn secondary" data-act="lock-rerun" data-id="${plan.id}">Keep ticked, re-plan rest</button>
        <button class="btn danger" data-act="discard" data-id="${plan.id}">Discard</button>
      </div>
    </div>
  </div>`;
}

const hoursOff = (mins) => (mins >= 1440 ? `${Math.round(mins / 144) / 10}d`
  : mins >= 60 ? `${Math.round(mins / 6) / 10}h` : `${mins}m`);

// One strip per day the plan touches: what stays, what arrives, and where a
// move comes from. A proposal is only legible against the day it lands in.
function planStrips(plan) {
  const moving = new Set(plan.displacements.map((d) => d.appointment_id));
  const days = new Set([
    ...plan.placements.map((x) => x.start.slice(0, 10)),
    ...plan.displacements.flatMap((d) => [d.was.slice(0, 10), d.now.slice(0, 10)]),
  ]);
  return [...days].sort().map((key) => {
    const blocks = [
      ...(state.appointments || []).filter((a) => a.status === "booked" && a.start.startsWith(key) && !moving.has(a.id))
        .map((a) => ({s: minOf(a.start), e: minOf(a.end), cls: "stay",
                      label: a.client_id === PROVIDER ? a.service : nameOf(a.client_id)})),
      ...plan.placements.filter((x) => x.start.startsWith(key))
        .map((x) => ({s: minOf(x.start), e: minOf(x.end), cls: "new", label: nameOf(x.client_id)})),
      ...plan.displacements.filter((d) => d.now.startsWith(key))
        .map((d) => ({s: minOf(d.now), e: minOf(d.now_end), cls: "to", label: `${nameOf(d.client_id)} ←`})),
    ];
    const ghosts = plan.displacements.filter((d) => d.was.startsWith(key))
      .map((d) => ({s: minOf(d.was), e: minOf(d.was_end), cls: "from", label: `${nameOf(d.client_id)} left`}));
    const open = segmentsOn((state.availability || {})[PROVIDER], key);
    const edges = [...open.flatMap((o) => [o.from, o.to]), ...blocks.flatMap((b) => [b.s, b.e]),
                   ...ghosts.flatMap((b) => [b.s, b.e])];
    const from = Math.floor(Math.min(...edges) / 60) * 60, to = Math.ceil(Math.max(...edges) / 60) * 60;
    const pct = (m) => ((m - from) / (to - from)) * 100;
    const overlaps = (a, b) => a.s < b.e && b.s < a.e;
    const draw = (b, half) => `<div class="blk ${b.cls} ${half}" style="left:${pct(b.s)}%;width:calc(${pct(b.e) - pct(b.s)}% - 2px)">${esc(b.label)}</div>`;
    return `<div class="strip"><div class="strip-label">${fmtDay(key)}</div>
      <div class="strip-track">
        ${open.map((o) => `<div class="open" style="left:${pct(o.from)}%;width:${pct(o.to) - pct(o.from)}%"></div>`).join("")}
        ${ghosts.map((g) => draw(g, blocks.some((b) => overlaps(b, g)) ? "bottom" : "")).join("")}
        ${blocks.map((b) => draw(b, ghosts.some((g) => overlaps(b, g)) ? "top" : "")).join("")}
      </div>
      <div class="strip-scale"><span>${hhmm(from)}</span><span>${hhmm(to)}</span></div></div>`;
  }).join("");
}

// Who an answer is still waiting on. The server will not write down a booking
// whose slot somebody has yet to agree to vacate, so a "yes" can be blocked —
// and a button promising to confirm it would then confirm nothing, silently.
function blockedBy(plan, approval, asks) {
  const item = approval.kind === "reschedule"
    ? plan.displacements.find((d) => d.appointment_id === approval.appointment_id)
    : plan.placements.find((x) => x.client_id === approval.client_id && x.start === approval.now);
  if (!item) return [];
  return (item.depends_on || []).map((key) => plan.displacements.find((d) => d.key === key))
    .filter(Boolean)
    .filter((d) => !asks.some((a) => a.kind === "reschedule" && a.appointment_id === d.appointment_id
                                   && (a.applied || a.status === "accepted")))
    .map((d) => nameOf(d.client_id));
}

function settlementCard(plan) {
  const asks = (state.approvals || []).filter((a) => a.plan_id === plan.id);
  const agreed = asks.filter((a) => a.status === "accepted" && !a.applied);
  const ready = agreed.filter((a) => !blockedBy(plan, a, asks).length);
  const waiting = asks.filter((a) => a.status === "pending" && !a.applied);
  const saidNo = asks.filter((a) => a.status === "declined" || a.status === "refused");
  const done = asks.filter((a) => a.applied);
  const words = {accepted: ["agreed", "ok"], pending: ["waiting", ""], declined: ["not that time", "warn"],
                 refused: ["keeping their slot", "warn"]};
  const row = (a) => {
    let [word, cls] = a.applied ? ["in the diary", "ok"] : (words[a.status] || [a.status, ""]);
    const blocked = a.status === "accepted" && !a.applied ? blockedBy(plan, a, asks) : [];
    if (blocked.length) [word, cls] = [`agreed · waits for ${blocked.join(", ")}`, "held"];
    return `<div class="row"><div class="main">
      <div class="title">${esc(nameOf(a.client_id))} <span class="tag ${a.kind === "reschedule" ? "move" : ""}">${a.kind === "reschedule" ? "move" : "book"}</span></div>
      <div class="meta">${a.was ? `${fmtWhen(a.was)} → ` : ""}${fmtWhen(a.now)}</div></div>
      <span class="tag ${cls}">${word}</span></div>`;
  };
  const held = agreed.length - ready.length;
  return `<div class="card ${ready.length ? "attention" : ""}">
    <h3>${agreed.length} agreed · ${waiting.length} waiting · ${saidNo.length} said no</h3>
    <p class="hint" style="margin:2px 0 10px">Agreeing is an answer, not a booking — nothing here is in the diary until you confirm.</p>
    <div class="list" style="box-shadow:none">${[...agreed, ...waiting, ...saidNo, ...done].map(row).join("")}</div>
    <div class="stack">
      <button class="btn ok" data-act="settle" data-id="${plan.id}" data-how="agreed" ${ready.length ? "" : "disabled"}>
        ${ready.length ? `Confirm the ${ready.length} agreed` : "Nothing ready to confirm yet"}</button>
      ${held ? `<p class="hint" style="margin:0">${plural(held, "agreed change waits", "agreed changes wait")}
        for someone else to move first. It goes in once they say yes.</p>` : ""}
      <button class="btn secondary" data-act="settle" data-id="${plan.id}" data-how="agreed_only" ${waiting.length ? "" : "disabled"}>
        Confirm agreed, stop waiting for ${waiting.length}</button>
      <button class="btn danger" data-act="settle" data-id="${plan.id}" data-how="reoptimise">Drop it all and re-plan</button>
    </div>
    <p class="hint">Whoever you stop waiting for goes back to Requests, not lost.</p>
  </div>`;
}

// -- the provider: more ------------------------------------------------

const MORE = [
  ["hours", "Working hours", "Your normal week"],
  ["away", "Time off", "Ill, on holiday, or just not in"],
  ["clients", "Clients", "Everyone and their history"],
  ["services", "Services", "What can be booked, for how long"],
  ["calendar", "Calendar sync", "Your diary in Google, Apple or Outlook"],
  ["messages", "Messages", "Conversations with clients"],
  ["payments", "Payments", "Fees to decide, what is owed, what is paid"],
  ["billing", "Billing settings", "Fees, GST, how clients pay"],
  ["settings", "Scheduling", "How far ahead to plan, and when"],
  ["activity", "Activity", "Everything that has happened"],
];

const backLink = () => `<button class="link" data-act="tab" data-tab="more" style="margin:-8px 0 0 -4px">‹ More</button>`;

function moreView() {
  const page = tab.split("/")[1];
  const pages = {hours: hoursView, away: awayView, clients: clientsView, services: servicesView,
                 settings: settingsView, activity: activityView, calendar: calendarView,
                 payments: paymentsView, billing: billingView, messages: messagesView};
  if (pages[page]) return pages[page]();
  return `<h1>More</h1><div class="list">${MORE.map(([id, title, meta]) => `
    <button class="row" data-act="more-open" data-page="${id}">
      <div class="main"><div class="title">${title}</div><div class="meta">${meta}</div></div>
      <span class="chev">›</span></button>`).join("")}</div>
    <h2>Demo</h2><div class="card">
      <label class="field"><span>Start again from</span>
        <select data-role="scenario"><option value="simple">Small example</option>
          <option value="busy">Realistic practice</option><option value="empty">Empty</option></select></label>
      <button class="btn danger block" data-act="reset">Start over</button>
      <p class="hint">Throws away everything in this session.</p>
    </div>
    <div class="stack"><a class="btn ghost" href="/?as=provider&desktop=1">Open the desktop view</a></div>`;
}

function awayView() {
  const a = ui.away;
  const day = a.day || todayKey();
  return `${backLink()}<h1>Time off</h1>
    <p class="lead">Marks the time as not working. Bookings already in it are
      dealt with however you choose below.</p>
    <form data-form="away" class="card">
      <div class="segmented" style="margin-bottom:12px">
        <button type="button" data-act="away-mode" data-mode="days" aria-pressed="${a.mode === "days"}">Whole days</button>
        <button type="button" data-act="away-mode" data-mode="part" aria-pressed="${a.mode === "part"}">Part of a day</button>
      </div>
      ${a.mode === "days" ? `<div class="field-row">
          <label class="field"><span>From</span><input type="date" name="from_date" required min="${todayKey()}" value="${day}"></label>
          <label class="field"><span>Until</span><input type="date" name="to_date" required min="${todayKey()}" value="${day}"></label>
        </div>` : `<label class="field"><span>Date</span><input type="date" name="from_date" required min="${todayKey()}" value="${day}"></label>
        <div class="field-row">
          <label class="field"><span>From</span><select name="from">${timeOptions("09:00", 0, 23 * 60 + 30)}</select></label>
          <label class="field"><span>Until</span><select name="to">${timeOptions("13:00", 0, 23 * 60 + 30)}</select></label>
        </div>`}
      <span class="hint" style="margin:0 0 6px;display:block">Bookings already in that time</span>
      <div class="choices">${[
        ["flag", "Leave them, just flag them", "Nothing changes until you decide. Safest if you might have the date wrong."],
        ["reschedule", "Find them a new time", "Each client keeps their hour until somewhere else is agreed."],
        ["cancel", "Cancel them", "Nobody is offered anything else."],
      ].map(([id, title, meta]) => `<button type="button" class="choice" data-act="away-action" data-action="${id}" aria-pressed="${a.action === id}">
        <span><b>${title}</b><br><span class="meta">${meta}</span></span></button>`).join("")}</div>
      <div class="stack"><button class="btn block" type="submit">Mark me away</button></div>
    </form>`;
}

function clientsView() {
  return `${backLink()}<h1>Clients</h1><div class="list">${(state.clients || []).map((c) => `
    <button class="row" data-act="client-open" data-id="${esc(c.id)}">
      <span class="avatar">${esc(c.name.slice(0, 1).toUpperCase())}</span>
      <div class="main"><div class="title">${esc(c.name)}</div>
        <div class="meta">${c.booked} booked${c.open_requests ? ` · ${c.open_requests} waiting` : ""}${c.no_show ? ` · ${c.no_show} no-show` : ""}</div>
        <div class="meta">${limitWords(c)}</div></div>
      <span class="chev">›</span></button>`).join("")}</div>
    <h2>Add a client</h2>
    <form data-form="client" class="card">
      <label class="field"><span>Name</span><input name="name" required autocomplete="off" placeholder="Dana"></label>
      <label class="row" style="padding:0;min-height:44px;border:0">
        <input type="checkbox" name="mirror" checked> <span class="main">Give them your working hours to start with</span></label>
      <button class="btn block" type="submit">Add</button>
    </form>`;
}

function clientSheet(id) {
  const c = clientById(id);
  if (!c) return `<h3>Gone</h3>`;
  const weekly = ((state.weekly || {})[id] || []);
  const hours = weekly.length ? weekly.map((r) => `${WEEKDAYS[r.weekday]} ${r.from}–${r.to}`).join("<br>") : "None set";
  const choice = (kind, current, options) => `<div class="segmented">${options.map(([n, label]) => `
    <button type="button" data-act="limit" data-client="${esc(id)}" data-kind="${kind}" data-n="${n}"
      aria-pressed="${(current ?? "") === n}">${label}</button>`).join("")}</div>`;
  return `<h3>${esc(c.name)}</h3>${factsHtml([
    ["Booked", c.booked], ["Waiting", c.open_requests], ["Attended", c.completed],
    ["No-show", c.no_show], ["Cancelled", c.cancelled], ["Moved by you", c.moved_by_us], ["Free", hours],
  ])}
  <h2>Billing</h2>
  <div class="segmented">${[["", "Practice default"], ["after_session", "After each session"], ["off", "Not billed"]]
    .map(([m, label]) => `<button type="button" data-act="billing-mode" data-client="${esc(id)}" data-mode="${m}"
      aria-pressed="${(c.billing_mode || "") === m}">${label}</button>`).join("")}</div>
  ${chargesOf(id, "due").length ? `<p class="hint">Owes ${sgd(chargesOf(id, "due").reduce((t, x) => t + x.amount, 0))}.</p>` : ""}
  <h2>Sessions allowed</h2>
  <p class="hint" style="margin-top:0">The scheduler never books or moves ${esc(c.name)} past these.</p>
  <div class="field"><span>A day</span>${choice("per_day", c.per_day, [[1, "1"], [2, "2"], [3, "3"], ["", "No limit"]])}</div>
  <div class="field"><span>A week</span>${choice("per_week", c.per_week, [[1, "1"], [2, "2"], [3, "3"], ["", "No limit"]])}</div>
  ${(() => {
    const series = (state.series || []).filter((x) => x.client_id === id && x.status !== "stopped");
    return series.length ? `<h2>Repeating</h2><div class="list">${series.map((x) => `
      <div class="row"><div class="main"><div class="title">${esc(x.service)}</div>
        <div class="meta">${seriesLine(x)}</div><div class="meta">${seriesMeta(x)}</div></div>
        ${x.status === "active" ? `<button class="btn small danger" data-act="series-stop" data-id="${x.id}">Stop</button>` : ""}
      </div>`).join("")}</div>` : "";
  })()}
  <div class="stack">
    <button class="btn" data-act="series-open" data-id="${esc(id)}">Set up a repeating booking</button>
    <button class="btn secondary" data-act="view-as" data-who="${esc(id)}">See ${esc(c.name)}'s view</button>
    <button class="btn ghost" data-act="close">Close</button></div>`;
}

// The provider setting one up for a client — how weekly therapy usually
// starts: agreed in the room, entered by the clinician.
function seriesSheet(clientId) {
  const services = bookable();
  const key = `series-${clientId}`;
  const p = ui.pickers[key] || {};
  if (!p.service || !services.some((x) => x.id === p.service)) {
    p.service = (services.slice().sort((a, b) => a.duration - b.duration)[0] || {}).id;
  }
  if (!p.every) p.every = 1;
  const service = serviceById(p.service);
  if (!service) return `<h3>Nothing to book</h3><p>No services are listed.</p>`;
  const body = picker(key, clientId, service.duration);
  Object.assign(ui.pickers[key], {service: p.service, every: p.every});
  const start = pickedStart(key);
  return `<h3>Repeating booking for ${esc(nameOf(clientId))}</h3>
    <h2>What</h2><div class="choices">${services.map((x) => `
      <button class="choice" data-act="series-service" data-client="${esc(clientId)}" data-id="${esc(x.id)}"
        aria-pressed="${x.id === p.service}"><span><b>${esc(x.name)}</b><br>
        <span class="meta">${x.duration} minutes</span></span></button>`).join("")}</div>
    <h2>First session</h2>${body}
    <h2>How often</h2>
    <div class="segmented">${FREQUENCIES.filter(([n]) => n).map(([n, label]) => `
      <button type="button" data-act="series-every" data-client="${esc(clientId)}" data-n="${n}"
        aria-pressed="${p.every === n}">${label}</button>`).join("")}</div>
    ${untilField("series-until")}
    <div class="stack">
      <button class="btn block" data-act="series-submit" data-client="${esc(clientId)}" ${start ? "" : "disabled"}>
        ${start ? `Start ${everyWords(p.every)} from ${fmtDay(start)}` : "Pick the first session"}</button>
      <button class="btn ghost" data-act="close">Cancel</button>
    </div>
    <p class="hint">Times shown are when ${esc(nameOf(clientId))} is free. Each session is still
      offered to them to accept as it comes up.</p>`;
}

function servicesView() {
  return `${backLink()}<h1>Services</h1><div class="list">${(state.services || []).map((s) => `
    <div class="row ${s.active ? "" : "dim"}"><div class="main"><div class="title">${esc(s.name)}</div>
      <div class="meta">${s.duration} min · ${money(s.price)}${s.client_bookable ? "" : " · only you can book it"}</div></div>
      <button class="btn small ${s.active ? "danger" : "secondary"}" data-act="service-toggle" data-id="${esc(s.id)}"
        data-active="${s.active ? 0 : 1}">${s.active ? "Withdraw" : "Re-list"}</button></div>`).join("")}</div>
    <p class="hint">Withdrawn services stay readable, so bookings and invoices made against them still work.</p>
    <h2>Add a service</h2>
    <form data-form="service" class="card">
      <label class="field"><span>Name</span><input name="name" required autocomplete="off"></label>
      <div class="field-row">
        <label class="field"><span>Minutes</span><input name="duration" type="number" inputmode="numeric" value="60" min="5" step="5"></label>
        <label class="field"><span>Price</span><input name="price" type="number" inputmode="decimal" value="80" min="0" step="1"></label>
      </div>
      <button class="btn block" type="submit">Add</button>
    </form>`;
}

function settingsView() {
  const s = state.scheduler || {};
  const toggle = (key, on, title, meta) => `<label class="row">
    <div class="main"><div class="title">${title}</div><div class="meta">${meta}</div></div>
    <input type="checkbox" data-act="setting-toggle" data-key="${key}" ${on ? "checked" : ""}></label>`;
  return `${backLink()}<h1>Scheduling</h1>
    <h2>Planning window</h2><div class="card">
      <div class="row" style="padding:0;border:0">
        <div class="main"><div class="title">Plan ${s.horizon_days} days ahead</div>
          <div class="meta">Further takes longer to work out.</div></div>
        <button class="btn small secondary" data-act="horizon" data-delta="-1" aria-label="Fewer days">−</button>
        <button class="btn small secondary" data-act="horizon" data-delta="1" aria-label="More days">+</button>
      </div></div>
    <div class="list">
      ${toggle("scope_to_horizon", s.scope_to_horizon !== false, "Only plan requests inside the window",
               "Someone asking for next month is not booked into this week.")}
      ${toggle("auto_run", s.auto_run, "Make schedules automatically",
               "Weekly, and when a request is getting urgent. Off: only when you ask.")}
      ${toggle("rehouse_orphans_on_run", s.rehouse_orphans_on_run, "Find new times for stranded bookings",
               "When a schedule is made, rather than waiting for you to decide.")}
    </div>`;
}

// -- calendar sync ---------------------------------------------------

const calendarPerson = () => (isProvider() ? "provider" : me);

// A subscribed calendar fetches from wherever it runs. Google fetches from its
// own servers, so an address only this Wi-Fi can reach will never work there.
const reachableFromInternet = () => !/^(localhost|127\.|10\.|192\.168\.|172\.(1[6-9]|2\d|3[01])\.|\[?::1)/
  .test(location.hostname);

function calendarBody() {
  const token = (state.feeds || {})[calendarPerson()];
  if (!token) {
    return `<p>Get a private link that keeps your calendar app in step with
      ${isProvider() ? "the diary" : "your appointments"}: new bookings appear,
      moves move, cancellations disappear.</p>
      <div class="stack"><button class="btn block" data-act="calendar-make">Create my calendar link</button></div>`;
  }
  const path = `${location.host}/calendar/${token}.ics`;
  const https = `${location.protocol}//${path}`;
  return `
    <div class="stack" style="margin-top:0">
      <a class="btn block" href="webcal://${path}">Add to Apple Calendar</a>
    </div>
    <label class="field" style="margin-top:14px"><span>Your private link</span>
      <input readonly value="${esc(https)}" data-role="feed-link"></label>
    <button class="btn secondary block" data-act="calendar-copy">Copy link</button>
    <h2>Google Calendar</h2>
    <p class="hint" style="margin-top:0">On a computer: Google Calendar → <b>Other calendars +</b> →
      <b>From URL</b> → paste the link. It then appears on your phone too.
      Google checks for changes every few hours, not instantly.</p>
    ${reachableFromInternet() ? "" : `<p class="because">This address only works on this Wi-Fi, so Google
      cannot read it yet. It will once the app has a public address.</p>`}
    <h2>Outlook</h2>
    <p class="hint" style="margin-top:0">Add calendar → <b>Subscribe from web</b> → paste the link.</p>
    <h2>Privacy</h2>
    <p class="hint" style="margin-top:0">${isProvider()
      ? "Your diary shows client names and services, never notes."
      : "Each entry says only \"Appointment\" and the practice name — nothing about what it is for."}
      Anyone with the link can see these, so keep it to yourself.</p>
    <button class="btn danger block" data-act="calendar-reset">Replace the link</button>
    <p class="hint">If it was shared by mistake. The old link stops working at once.</p>`;
}

function calendarSheet() {
  return `<h3>Put these in your calendar</h3>${calendarBody()}
    <div class="stack"><button class="btn ghost" data-act="close">Done</button></div>`;
}

function calendarView() {
  return `${backLink()}<h1>Calendar sync</h1>
    <form data-form="practice" class="card">
      <label class="field"><span>Practice name</span>
        <input name="name" value="${esc(state.practice_name || "")}" autocomplete="off"></label>
      <p class="hint" style="margin-top:-4px">The only thing clients' calendars say about an appointment.</p>
      <button class="btn secondary block" type="submit">Save name</button>
    </form>
    <h2>Your diary</h2><div class="card">${calendarBody()}</div>`;
}

// -- payments (provider) ---------------------------------------------

function paymentsView() {
  const fees = chargesOf(null, "proposed"), owed = chargesOf(null, "due");
  const paid = chargesOf(null, "paid").slice().reverse().slice(0, 20);
  const total = (list) => sgd(list.reduce((t, c) => t + c.amount, 0));
  const who = (c) => `<div class="title">${esc(nameOf(c.client_id))} · ${sgd(c.amount)}</div>
    <div class="meta">${esc(c.description)}</div>`;

  let html = `${backLink()}<h1>Payments</h1>
    <p class="lead">${(state.billing || {}).processor === "stripe"
      ? "Stripe, test mode." : "Simulated payments — no real money moves."}</p>`;

  html += `<h2>Fees to decide</h2>`;
  html += fees.length ? `<div class="list">${fees.map((c) => `<div class="row" style="flex-wrap:wrap">
      <div class="main">${who(c)}<div class="because">${esc(c.reason)}</div></div>
      <div class="pair" style="width:100%;margin-top:6px">
        <button class="btn small" data-act="fee-confirm" data-id="${c.id}">Charge it</button>
        <button class="btn small secondary" data-act="charge-waive" data-id="${c.id}">Waive</button>
      </div></div>`).join("")}</div>
      <p class="hint">Proposed by your rules, never charged by them. The client sees nothing until you choose.</p>`
    : `<p class="hint">None.</p>`;

  html += `<h2>Owed · ${total(owed)}</h2>`;
  html += owed.length ? `<div class="list">${owed.map((c) => `<div class="row">
      <div class="main">${who(c)}</div>
      <button class="btn small secondary" data-act="record-open" data-id="${c.id}">Record</button></div>`).join("")}</div>`
    : `<p class="hint">Nothing owed.</p>`;

  html += `<h2>Recently paid</h2>`;
  html += paid.length ? `<div class="list">${paid.map((c) => {
      const p = paymentOf(c);
      return `<div class="row"><div class="main">${who(c)}
        <div class="meta">${p ? `${fmtDay(p.created)} · ${esc(methodWords(p.method))}` : ""}</div></div>
        <span class="tag ok">paid</span></div>`;
    }).join("")}</div>` : `<p class="hint">Nothing yet.</p>`;
  return html;
}

function recordSheet(id) {
  const c = (state.charges || []).find((x) => x.id === id);
  if (!c) return `<h3>Gone</h3>`;
  const methods = (state.billing || {}).recorded_methods || [];
  return `<h3>${esc(nameOf(c.client_id))} paid ${sgd(c.amount)}</h3>
    <p class="hint" style="margin:0 0 8px">${esc(c.description)}. How?</p>
    <div class="stack">${methods.map((m) => `
      <button class="btn secondary block" data-act="record" data-id="${c.id}" data-method="${m}">${esc(methodWords(m))}</button>`).join("")}
      <button class="btn danger block" data-act="charge-waive" data-id="${c.id}">Waive it instead</button>
      <button class="btn ghost" data-act="close">Cancel</button></div>`;
}

function billingView() {
  const b = state.billing || {};
  const modes = b.modes || {};
  return `${backLink()}<h1>Billing settings</h1>
    <form data-form="billing" class="card">
      <label class="field"><span>Clients pay</span>
        <select name="default_mode">${Object.entries(modes).map(([k, v]) =>
          `<option value="${k}" ${k === b.default_mode ? "selected" : ""}>${esc(v)}</option>`).join("")}</select></label>
      <p class="hint" style="margin-top:-4px">Paying at booking, packages and monthly invoices are coming next.
        Any client can be set differently on their card.</p>
      <label class="field"><span>A cancellation is late if it is within (hours)</span>
        <input name="late_cancel_window_hours" type="number" inputmode="numeric" min="0" max="336"
          value="${b.late_cancel_window_hours}"></label>
      <div class="field-row">
        <label class="field"><span>Late-cancellation fee</span>
          <input name="late_cancel_fee" value="${esc(b.late_cancel_fee)}" autocomplete="off"></label>
        <label class="field"><span>No-show fee</span>
          <input name="no_show_fee" value="${esc(b.no_show_fee)}" autocomplete="off"></label>
      </div>
      <p class="hint" style="margin-top:-4px">"100%" or "50%" of the session price, or an amount like "40.00".
        "0" for none. Fees are always proposed to you first, never charged on their own.</p>
      <label class="row" style="padding:0;min-height:44px;border:0">
        <span class="main"><span class="title">GST-registered</span><br>
          <span class="meta">Off: prices are final and GST is never mentioned.</span></span>
        <input type="checkbox" name="gst_registered" ${b.gst_registered ? "checked" : ""}></label>
      <div class="field-row" style="margin-top:8px">
        <label class="field"><span>GST registration no.</span>
          <input name="gst_number" value="${esc(b.gst_number)}" autocomplete="off"></label>
        <label class="field"><span>Rate (%)</span>
          <input name="gst_rate_percent" type="number" inputmode="decimal" step="0.1" min="0" max="30"
            value="${b.gst_rate_percent}"></label>
      </div>
      <p class="hint" style="margin-top:-4px">Prices include GST, as Singapore requires for prices shown to
        clients; receipts show the GST part.</p>
      <button class="btn block" type="submit">Save</button>
    </form>
    <h2>Payment processor</h2>
    <div class="card"><b>${esc(b.processor_label || "")}</b>
      <p class="hint" style="margin:6px 0 0">To use Stripe's test mode, start the server with a Stripe
        <b>test</b> secret key in <code>STRIPE_SECRET_KEY</code>. Live keys are refused until sign-in exists.</p></div>`;
}

function activityView() {
  return `${backLink()}<h1>Activity</h1><div class="list"><div class="log">${
    (state.log || []).slice().reverse().map(esc).join("\n")}</div></div>`;
}

// -- the sheet ----------------------------------------------------------

let sheetRenderer = null;
let sheetResolve = null;

function openSheet(renderer) {
  sheetRenderer = renderer;
  $("sheet").innerHTML = `<div class="grabber"></div>${renderer()}`;
  $("sheet").hidden = false;
  $("scrim").hidden = false;
  $("sheet").scrollTop = 0;
  restoreScrolls($("sheet"), {});      // visible now, so the strip can be measured
}

function closeSheet(answer) {
  sheetRenderer = null;
  $("sheet").hidden = true;
  $("scrim").hidden = true;
  $("sheet").innerHTML = "";
  if (sheetResolve) { const r = sheetResolve; sheetResolve = null; r(answer); }
}

function redrawSheet() {
  if (!sheetRenderer) return;
  const kept = keepForms($("sheet"));
  const scrolls = keepScrolls($("sheet"));
  $("sheet").innerHTML = `<div class="grabber"></div>${sheetRenderer()}`;
  restoreForms($("sheet"), kept);
  restoreScrolls($("sheet"), scrolls);
}

// A yes/no question, as a sheet rather than confirm(): a native dialog on a
// phone gives no room to say what the choice actually does.
function ask(title, body, yes, danger) {
  return new Promise((resolve) => {
    openSheet(() => `<h3>${title}</h3><p>${body}</p><div class="stack">
      <button class="btn ${danger ? "danger" : ""} block" data-act="answer" data-yes="1">${yes}</button>
      <button class="btn ghost block" data-act="answer" data-yes="0">Go back</button></div>`);
    sheetResolve = resolve;
  });
}

let toastTimer = null;
function toast(message) {
  const el = $("toast");
  if (!el) return;
  el.textContent = message;
  el.classList.add("show");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.remove("show"), 2600);
}

// -- actions ------------------------------------------------------------

function go(next) {
  tab = next;
  try { history.replaceState({}, "", `?as=${encodeURIComponent(me)}#${tab}`); } catch { /* sandbox */ }
  closeSheet();
  render(true);
  window.scrollTo(0, 0);
}

// Choices belong to the person who made them; nobody inherits a half-picked
// booking from whoever was on the screen before.
function resetChoices() {
  chat.mine = null; chat.summary = []; chat.thread = {}; chatText = "";
  ui.pickers = {}; ui.bookService = null; ui.bookEvery = 0; ui.diaryDay = null;
  ui.runOver = null; ui.runSeen = null; ui.picked = {};
}

function switchTo(who) {
  me = who;
  remember(who);
  resetChoices();
  go("home");
}

async function done(promise, message) {
  const result = await promise;
  if (result !== null && message) toast(message);
  await refresh(true);
  return result;
}

const ACTIONS = {
  tab: (d) => go(d.tab),
  close: () => closeSheet(),
  answer: (d) => closeSheet(d.yes === "1"),
  "view-as": (d) => switchTo(d.who),
  "more-open": (d) => { if (d.day) ui.away.day = d.day; go(`more/${d.page}`); },

  // pickers
  "pick-day": (d) => { const p = ui.pickers[d.picker]; p.day = d.day; p.time = null; render(true); },
  "pick-time": (d) => { ui.pickers[d.picker].time = Number(d.time); render(true); },

  // client
  respond: async (d) => {
    if (d.answer === "refuse" && !await ask("Keep your current time?",
        "The clinic will stop asking you to move this appointment.", "Keep my slot")) return;
    const words = {accept: "Thanks — the clinic will confirm", decline: "Got it — they may suggest another time",
                   refuse: "You're keeping your slot"};
    await done(api(`/api/approvals/${d.id}`, {answer: d.answer}), words[d.answer]);
  },
  withdraw: async (d) => {
    if (!await ask("Withdraw this request?", "You will not be offered a time for it.", "Withdraw", true)) return;
    await done(api(`/api/requests/${d.id}/withdraw`), "Request withdrawn");
  },
  "book-service": (d) => { ui.bookService = d.id; render(true); },
  "book-every": (d) => { ui.bookEvery = Number(d.n); render(true); },
  "book-submit": async () => {
    const start = pickedStart("book");
    if (!start) return;
    let ok;
    if (ui.bookEvery) {
      const until = document.querySelector('[data-form="book-until"] [name="until"]');
      ok = await done(api("/api/series", {client_id: me, service_id: ui.bookService, first_start: start,
                                          every_weeks: ui.bookEvery, until: (until && until.value) || null}),
                      `Set up ${everyWords(ui.bookEvery)} — no need to ask again`);
    } else {
      ok = await done(api("/api/requests", {client_id: me, service_id: ui.bookService, preferred_start: start}),
                      "Request sent — you'll be offered a time");
    }
    if (ok) { delete ui.pickers.book; ui.bookEvery = 0; go("home"); }
  },
  "series-stop": async (d) => {
    const x = (state.series || []).find((y) => y.id === Number(d.id));
    if (!await ask("Stop this repeating booking?",
        `${esc(x ? x.service : "")}, ${x ? seriesLine(x) : ""}. Sessions already booked stay booked;
         ones not yet booked are withdrawn.`, "Stop it", true)) return;
    await done(api(`/api/series/${d.id}/stop`), "Stopped");
  },
  "series-open": (d) => openSheet(() => seriesSheet(d.id)),
  "calendar-open": () => openSheet(calendarSheet),
  "install-tip-dismiss": () => {
    try { localStorage.setItem("install-tip", "no"); } catch { /* private mode */ }
    render(true);
  },
  pay: async (d) => {
    const out = await api(`/api/charges/${d.id}/checkout`, {return_to: `/phone?as=${encodeURIComponent(me)}`});
    if (out && out.url) location.href = out.url;          // the processor's own page
  },
  "fee-confirm": async (d) => {
    const c = (state.charges || []).find((x) => x.id === Number(d.id));
    if (!await ask(`Charge ${esc(nameOf(c.client_id))} ${sgd(c.amount)}?`,
        `${esc(c.description)} — ${esc(c.reason)}. They will be asked to pay it.`, "Charge it")) return;
    await done(api(`/api/charges/${d.id}/confirm`), "Charged — the client will see it");
  },
  "charge-waive": async (d) => {
    await done(api(`/api/charges/${d.id}/waive`), "Waived");
    closeSheet();
  },
  "record-open": (d) => openSheet(() => recordSheet(Number(d.id))),
  record: async (d) => {
    const ok = await done(api(`/api/charges/${d.id}/record`, {method: d.method}), "Recorded as paid");
    if (ok) closeSheet();
  },
  "billing-mode": async (d) => {
    await done(api(`/api/clients/${encodeURIComponent(d.client)}/billing-mode`, {mode: d.mode || null}), "Saved");
  },
  "calendar-make": async () => {
    await done(api(`/api/calendar/${encodeURIComponent(calendarPerson())}`, {}), "Your calendar link is ready");
  },
  "calendar-copy": async () => {
    const field = document.querySelector('[data-role="feed-link"]');
    if (!field) return;
    try {
      await navigator.clipboard.writeText(field.value);
      toast("Copied");
    } catch {
      // No clipboard over plain http on a phone: select it for a long-press copy.
      field.focus(); field.select();
      toast("Selected — press and hold to copy");
    }
  },
  "calendar-reset": async () => {
    if (!await ask("Replace your calendar link?",
        "The old link stops working at once, and any calendar using it stops updating until you add the new one.",
        "Replace it", true)) return;
    await done(api(`/api/calendar/${encodeURIComponent(calendarPerson())}/reset`, {}), "New link made");
  },
  limit: async (d) => {
    const c = clientById(d.client);
    const next = {per_day: c.per_day ?? null, per_week: c.per_week ?? null};
    next[d.kind] = d.n === "" ? null : Number(d.n);
    await done(api(`/api/clients/${encodeURIComponent(d.client)}/limits`, next),
               `${c.name}: ${limitWords(next)}`);
  },
  "series-service": (d) => { ui.pickers[`series-${d.client}`].service = d.id; redrawSheet(); },
  "series-every": (d) => { ui.pickers[`series-${d.client}`].every = Number(d.n); redrawSheet(); },
  "series-submit": async (d) => {
    const key = `series-${d.client}`, start = pickedStart(key), p = ui.pickers[key];
    if (!start) return;
    const until = document.querySelector('[data-form="series-until"] [name="until"]');
    const ok = await done(api("/api/series", {client_id: d.client, service_id: p.service, first_start: start,
                                              every_weeks: p.every, until: (until && until.value) || null}),
                          `Set up ${everyWords(p.every)} for ${nameOf(d.client)}`);
    if (ok) { delete ui.pickers[key]; closeSheet(); }
  },
  booking: (d) => openSheet(() => bookingSheet(Number(d.id))),
  "move-open": (d) => openSheet(() => moveSheet(Number(d.id))),
  "move-release": (d) => {
    const key = `move-${d.id}`;
    ui.pickers[key] = {...(ui.pickers[key] || {}), release: d.on === "1"};
    redrawSheet();
  },
  "move-submit": async (d) => {
    const key = `move-${d.id}`, start = pickedStart(key);
    if (!start) return;
    const ok = await done(api(`/api/appointments/${d.id}/reschedule-request`,
      {preferred_start: start, release_slot: !!ui.pickers[key].release}), "Asked to move");
    if (ok) { delete ui.pickers[key]; closeSheet(); }
  },
  "cancel-booking": async (d) => {
    const a = appointmentById(Number(d.id));
    if (!await ask("Cancel this appointment?", `${esc(a.service)}, ${fmtWhen(a.start)}. This cannot be undone.`,
                   "Cancel appointment", true)) return;
    await done(api(`/api/appointments/${d.id}/cancel`, {by_provider: false}), "Appointment cancelled");
  },

  // hours
  "range-add": (d) => openSheet(() => rangeSheet(Number(d.weekday))),
  "range-remove": async (d) => {
    const ranges = ((state.weekly || {})[whose()] || []).filter((_, i) => i !== Number(d.index));
    await done(api("/api/availability", {client_id: whose(), ranges}), "Hours updated");
  },
  "exception-add": (d) => { ui.exceptionAvailable = false; openSheet(() => exceptionSheet(d.day)); },
  "exception-kind": (d) => { ui.exceptionAvailable = d.on === "1"; redrawSheet(); },
  "exception-clear": async (d) => {
    await done(api("/api/exceptions", {client_id: whose(), date: d.date, from_time: d.from, to_time: d.to,
                                       available: d.kind === "add", clear: true}), "Back to usual");
  },

  // provider
  stranded: () => openSheet(strandedSheet),
  "stranded-fix": async (d) => {
    if (d.how === "cancel" && !await ask("Cancel every stranded booking?",
        "Nobody is offered another time.", "Cancel them", true)) return;
    await done(api(`/api/orphans/${d.how}`, {}), d.how === "cancel" ? "Cancelled" : "Queued to find new times");
    closeSheet();
  },
  "diary-day": (d) => { ui.diaryDay = d.day; render(true); },
  "diary-open": (d) => { ui.diaryDay = d.day; go("diary"); },
  slot: (d) => openSheet(() => slotSheet(Number(d.id))),
  offer: (d) => openSheet(() => offerSheet(Number(d.id))),
  attendance: async (d) => {
    await done(api(`/api/appointments/${d.id}/attendance`, {attended: d.attended === "1"}), "Recorded");
    closeSheet();
  },
  "provider-cancel": async (d) => {
    const a = appointmentById(Number(d.id));
    if (!await ask(`Cancel ${esc(nameOf(a.client_id))}'s appointment?`,
        `${fmtWhen(a.start)}. They are not offered another time.`, "Cancel appointment", true)) return;
    await done(api(`/api/appointments/${d.id}/cancel`, {by_provider: true}), "Cancelled");
  },

  "pick-request": (d, el) => {
    if (el.checked) ui.runOver.add(Number(d.id)); else ui.runOver.delete(Number(d.id));
    render(true);
  },
  "pick-window": () => {
    const end = planningEnd();
    ui.runOver = new Set((state.requests || []).filter((r) => openRequest(r) && r.preferred.slice(0, 10) < end).map((r) => r.id));
    render(true);
  },
  "pick-all": () => { ui.runOver = new Set((state.requests || []).filter(openRequest).map((r) => r.id)); render(true); },
  moves: (d) => { ui.moves = Number(d.n); render(true); },
  run: async () => {
    const ids = [...ui.runOver];
    ui.runOver = null;
    const outcome = await api("/api/solve", {request_ids: ids, alpha: ui.alpha, max_displacements: ui.moves});
    await refresh(true);
    if (!outcome) return;
    if (outcome.ran === false) { toast(`Nothing to do: ${outcome.reason}`); return; }
    if (!outcome.plan_id) { toast("Nothing could be fitted in"); return; }
    for (const w of outcome.warnings || []) toast(w);
    go("plans");
  },
  "place-open": (d, el, event) => { event.preventDefault(); openSheet(() => placeSheet(Number(d.id))); },
  "place-submit": async (d) => {
    const start = pickedStart(`place-${d.id}`);
    if (!start) return;
    const ok = await done(api("/api/appointments/manual", {request_id: Number(d.id), start}), "Booked and locked");
    if (ok) closeSheet();
  },
  tick: (d, el) => {
    (ui.picked[d.plan] = ui.picked[d.plan] || {})[d.key] = el.checked;
    render(true);
  },
  approve: async (d) => {
    const plan = planById(Number(d.id));
    const items = [...ticked(plan).on];
    const ok = await done(api(`/api/plans/${d.id}/approve`, {items}), "Sent to the clients");
    if (ok) delete ui.picked[d.id];
  },
  "lock-rerun": async (d) => {
    const plan = planById(Number(d.id));
    const ok = await api(`/api/plans/${d.id}/approve`, {items: [...ticked(plan).on]});
    if (!ok) return;
    delete ui.picked[d.id];
    await done(api("/api/solve", {alpha: ui.alpha ?? state.settings.alpha,
                                  max_displacements: ui.moves ?? state.settings.max_displacements}), "Re-planned the rest");
  },
  discard: async (d) => { await done(api(`/api/plans/${d.id}/discard`), "Discarded"); },
  settle: async (d) => {
    if (d.how === "reoptimise" && !await ask("Drop the whole plan?",
        "Agreed answers are discarded too — nobody has been booked — and the requests are planned again.",
        "Drop and re-plan", true)) return;
    const words = {agreed: "In the diary", agreed_only: "Confirmed; the rest are back in Requests",
                   reoptimise: "Dropped and re-planned"};
    await done(api(`/api/plans/${d.id}/settle`, {how: d.how}), words[d.how]);
  },

  "away-mode": (d) => { ui.away.mode = d.mode; render(true); },
  "away-action": (d) => { ui.away.action = d.action; render(true); },
  "client-open": (d) => openSheet(() => clientSheet(d.id)),
  "service-toggle": async (d) => {
    await done(api(`/api/services/${encodeURIComponent(d.id)}/active?active=${d.active === "1"}`),
               d.active === "1" ? "Re-listed" : "Withdrawn");
  },
  "setting-toggle": async (d, el) => {
    await done(api("/api/settings", {[d.key]: el.checked}), "Saved");
  },
  horizon: async (d) => {
    const days = Math.max(1, Math.min(60, ((state.scheduler || {}).horizon_days || 7) + Number(d.delta)));
    await done(api("/api/settings", {horizon_days: days}));
  },
  reset: async () => {
    const select = document.querySelector('[data-role="scenario"]');
    const which = select ? select.value : "simple";
    const named = select ? select.selectedOptions[0].textContent.toLowerCase() : which;
    if (!await ask("Start over?", `Everything in this session is thrown away and the ${esc(named)} is loaded.`,
                   "Start over", true)) return;
    ui.pickers = {}; ui.runOver = null; ui.picked = {}; ui.alpha = null; ui.moves = null; ui.diaryDay = null;
    await done(api(`/api/reset?scenario=${encodeURIComponent(which)}`), "Started over");
    go("home");
  },
};

const FORMS = {
  message: async (form) => {
    const text = form.elements.text.value.trim();
    if (!text) return;
    const to = isProvider() ? {client_id: openThread()} : {};
    try {
      await command("message.send", {...to, text});
      form.reset();
      if (document.activeElement) document.activeElement.blur();
      await refresh(true);
    } catch (e) { toast(e.message); }
  },
  "chat-settings": async (form) => {
    const f = form.elements;
    await done(api("/api/settings", {chat_enabled: f.enabled.checked,
      chat_reply_expectation: f.reply.value, chat_emergency_note: f.emergency.value}), "Saved");
  },
  billing: async (form) => {
    const f = form.elements;
    await done(api("/api/billing", {
      default_mode: f.default_mode.value,
      late_cancel_window_hours: Number(f.late_cancel_window_hours.value),
      late_cancel_fee: f.late_cancel_fee.value.trim(),
      no_show_fee: f.no_show_fee.value.trim(),
      gst_registered: f.gst_registered.checked,
      gst_number: f.gst_number.value.trim(),
      gst_rate_percent: Number(f.gst_rate_percent.value),
    }), "Billing settings saved");
  },
  practice: async (form) => {
    await done(api("/api/settings", {practice_name: form.elements.name.value}), "Saved");
  },
  // Fields only, read by the button beside them; Enter in one does nothing.
  "book-until": () => {},
  "series-until": () => {},
  range: async (form) => {
    const from = form.elements.from.value, to = form.elements.to.value;
    if (toMin(to) <= toMin(from)) { toast("The end has to be after the start"); return; }
    const weekday = Number(form.dataset.weekday);
    // Merge with anything it touches, so the pills stay readable.
    const all = [...((state.weekly || {})[whose()] || []), {weekday, from, to}];
    const same = all.filter((r) => r.weekday === weekday).sort((a, b) => a.from.localeCompare(b.from));
    const merged = [];
    for (const r of same) {
      const last = merged[merged.length - 1];
      if (last && toMin(r.from) <= toMin(last.to)) last.to = toMin(r.to) > toMin(last.to) ? r.to : last.to;
      else merged.push({...r});
    }
    const ranges = [...all.filter((r) => r.weekday !== weekday), ...merged];
    const ok = await done(api("/api/availability", {client_id: whose(), ranges}), "Hours updated");
    if (ok) closeSheet();
  },
  exception: async (form) => {
    const f = form.elements;
    if (toMin(f.to.value) <= toMin(f.from.value)) { toast("The end has to be after the start"); return; }
    const ok = await done(api("/api/exceptions", {client_id: whose(), date: f.date.value,
      from_time: f.from.value, to_time: f.to.value, available: ui.exceptionAvailable}), "Saved for that date");
    if (ok) closeSheet();
  },
  away: async (form) => {
    const f = form.elements, a = ui.away;
    let start, end;
    if (a.mode === "days") {
      if (f.to_date.value < f.from_date.value) { toast("The end has to be after the start"); return; }
      start = `${f.from_date.value}T00:00`;
      end = `${addDays(f.to_date.value, 1)}T00:00`;
    } else {
      if (toMin(f.to.value) <= toMin(f.from.value)) { toast("The end has to be after the start"); return; }
      start = `${f.from_date.value}T${f.from.value}`;
      end = `${f.from_date.value}T${f.to.value}`;
    }
    if (a.action === "cancel" && !await ask("Cancel everyone in that time?",
        "Nobody is offered another time.", "Mark away and cancel", true)) return;
    const outcome = await api("/api/provider/away", {start, end, action: a.action});
    await refresh(true);
    if (!outcome) return;
    const affected = outcome.affected || 0;
    toast(affected ? `Marked away · ${plural(affected, "booking")} ${
      {flag: "flagged", reschedule: "being moved", cancel: "cancelled"}[a.action]}` : "Marked away · nothing was booked");
    ui.away.day = null;
    go("home");
  },
  client: async (form) => {
    const name = form.elements.name.value.trim();
    if (!name) return;
    const ok = await done(api("/api/clients", {
      id: name.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, ""), name,
      mirror_provider: form.elements.mirror.checked,
    }), `Added ${name}`);
    if (ok) form.reset();
  },
  service: async (form) => {
    const f = form.elements, name = f.name.value.trim();
    const ok = await done(api("/api/services", {
      id: name.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, ""), name,
      duration_minutes: Number(f.duration.value),
      price_minor_units: Math.round(Number(f.price.value) * 100),
    }), `Added ${name}`);
    if (ok) form.reset();
  },
};

function dispatch(event) {
  const el = event.target.closest("[data-act]");
  if (!el) return;
  const act = ACTIONS[el.dataset.act];
  if (!act) return;
  // Checkboxes and ranges report through "change"/"input"; a click on them
  // would otherwise fire the action twice.
  const isValueControl = el.tagName === "INPUT" && (el.type === "checkbox" || el.type === "range");
  if (event.type === "click" && isValueControl) return;
  if (event.type !== "click" && !isValueControl) return;
  act(el.dataset, el, event);
}

document.addEventListener("click", dispatch);
document.addEventListener("change", dispatch);
document.addEventListener("input", (event) => {
  // The slider updates its own readout without a redraw, so dragging it stays smooth.
  const el = event.target;
  if (el.dataset && el.dataset.act === "alpha") ui.alpha = Number(el.value);
});
document.addEventListener("submit", (event) => {
  const form = event.target.closest("[data-form]");
  if (!form) return;
  // Never a real submission: that would reload the page and lose everything.
  event.preventDefault();
  if (FORMS[form.dataset.form]) FORMS[form.dataset.form](form);
});

if ($("scrim")) $("scrim").addEventListener("click", () => closeSheet(false));
if ($("role")) $("role").addEventListener("change", (e) => switchTo(e.target.value));
window.addEventListener("hashchange", () => {
  const next = (location.hash || "").slice(1) || "home";
  if (next !== tab) { tab = next; render(true); }
});

// For tests: render any view against whatever state was last fetched.
window.Phone = {
  views: VIEWS,
  actions: Object.keys(ACTIONS),
  forms: Object.keys(FORMS),
  // "diary@2026-10-13" opens the diary on that day.
  show: (who, where) => {
    const [view, day] = where.split("@");
    if (who !== me) resetChoices();
    me = who; tab = view;
    if (day) ui.diaryDay = day;
    render(true);
    return $("view").innerHTML;
  },
  tabs: () => $("tabs").innerHTML,
  get state() { return state; },
};

// Back from a payment page. The redirect says nothing about whether money
// moved — anyone can type an address — so the server asks the processor.
async function returnedFromCheckout() {
  const paid = params.get("paid"), cancelled = params.get("cancelled");
  if (!paid) return;
  try { history.replaceState({}, "", `?as=${encodeURIComponent(me)}#bookings`); } catch { /* sandbox */ }
  tab = "bookings";
  if (cancelled) { toast("Payment cancelled — nothing was charged"); return; }
  const out = await api(`/api/payments/${encodeURIComponent(paid)}/check`, {});
  if (!out) return;
  toast(out.status === "succeeded" ? "Paid — thank you"
    : out.status === "pending" ? "Still being confirmed — check back in a moment"
    : "The payment did not go through. Nothing was charged.");
}

returnedFromCheckout().then(() => refresh(true));
setInterval(() => refresh(false), 2500);
