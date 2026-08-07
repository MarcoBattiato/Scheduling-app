// A week-column calendar: days across, time down, several weeks stacked.
//
// Everything is drawn from a plain description — availability to tint, blocks
// to place, ghosts for where something used to be, arrows between them — so the
// same component renders the real schedule and a proposed one without knowing
// the difference between them.

const MIN_PER_DAY = (h) => h * 60;

function ymd(d) {
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
}

function mondayOf(d) {
  const out = new Date(d);
  out.setHours(0, 0, 0, 0);
  out.setDate(out.getDate() - ((out.getDay() + 6) % 7));
  return out;
}

function minutesInto(iso) {
  const d = new Date(iso);
  return d.getHours() * 60 + d.getMinutes();
}

/**
 * @param {HTMLElement} el      where to draw
 * @param {object} opts
 *   weeks        how many weeks to show
 *   start        Date inside the first week
 *   dayStart/dayEnd  hours bounding each day column
 *   availability [{start,end}]  tinted background
 *   blocks       [{id,start,end,label,sub,cls,data}]
 *   ghosts       [{id,start,end,label,cls}]  where something currently sits
 *   arrows       [{from,to}]  ids, drawn as a curve with a head
 *   onSelect     (dateStr, "HH:MM", "HH:MM", additive) => void
 */
function renderCalendar(el, opts) {
  const {
    weeks = 2, start = new Date(), dayStart = 8, dayEnd = 20,
    availability = [], blocks = [], ghosts = [], arrows = [], onSelect = null,
  } = opts;

  const first = mondayOf(start);
  const span = MIN_PER_DAY(dayEnd) - MIN_PER_DAY(dayStart);
  const pct = (mins) => ((mins - MIN_PER_DAY(dayStart)) / span) * 100;

  const days = [];
  for (let w = 0; w < weeks; w++) {
    for (let d = 0; d < 7; d++) {
      const day = new Date(first);
      day.setDate(first.getDate() + w * 7 + d);
      days.push(day);
    }
  }

  const byDay = (items) => {
    const map = {};
    for (const item of items) {
      (map[item.start.slice(0, 10)] = map[item.start.slice(0, 10)] || []).push(item);
    }
    return map;
  };
  const avail = byDay(availability), blocked = byDay(blocks), was = byDay(ghosts);

  const hours = [];
  for (let h = dayStart; h <= dayEnd; h++) hours.push(h);

  let html = `<div class="cal" style="--rows:${dayEnd - dayStart}">`;
  for (let w = 0; w < weeks; w++) {
    const week = days.slice(w * 7, w * 7 + 7);
    html += `<div class="cal-week">
      <div class="cal-corner">${week[0].toLocaleDateString([], {month: "short"})}</div>
      ${week.map((d) => `<div class="cal-dayhead${isToday(d) ? " today" : ""}">
          ${d.toLocaleDateString([], {weekday: "short"})}
          <span>${d.getDate()}</span></div>`).join("")}
      <div class="cal-times">${hours.map((h) =>
        `<div class="cal-hour"><span>${String(h).padStart(2, "0")}:00</span></div>`).join("")}</div>
      ${week.map((d) => {
        const key = ymd(d);
        return `<div class="cal-day" data-date="${key}">
          ${(avail[key] || []).map((a) => `<div class="cal-avail" style="${band(a)}"></div>`).join("")}
          ${(was[key] || []).map((g) => `<div class="cal-block ghost ${g.cls || ""}"
                id="cb-${g.id}" style="${band(g)}"><b>${esc(g.label || "")}</b></div>`).join("")}
          ${(blocked[key] || []).map((b) => `<div class="cal-block ${b.cls || ""}"
                id="cb-${b.id}" data-hover='${attr(b.data)}' style="${band(b)}">
                <b>${esc(b.label || "")}</b>${b.sub ? `<i>${esc(b.sub)}</i>` : ""}</div>`).join("")}
        </div>`;
      }).join("")}
    </div>`;
  }
  html += `<svg class="cal-arrows"></svg></div>`;
  el.innerHTML = html;

  function band(item) {
    const top = pct(minutesInto(item.start));
    const height = pct(minutesInto(item.end)) - top;
    return `top:${top}%;height:${Math.max(height, 1.6)}%`;
  }

  drawArrows(el, arrows);
  if (onSelect) wireSelection(el, dayStart, dayEnd, onSelect);
  return el;
}

function isToday(d) {
  const now = new Date();
  return d.toDateString() === now.toDateString();
}

const esc = (s) => String(s ?? "").replace(/[&<>]/g, (c) => ({"&": "&amp;", "<": "&lt;", ">": "&gt;"}[c]));
const attr = (o) => o ? esc(JSON.stringify(o)).replace(/'/g, "&#39;") : "";

// -- arrows -----------------------------------------------------------

function drawArrows(el, arrows) {
  const svg = el.querySelector(".cal-arrows");
  if (!svg || !arrows.length) return;
  const box = el.querySelector(".cal").getBoundingClientRect();
  svg.setAttribute("viewBox", `0 0 ${box.width} ${box.height}`);
  svg.style.width = box.width + "px";
  svg.style.height = box.height + "px";

  const parts = [`<defs><marker id="ah" viewBox="0 0 10 10" refX="9" refY="5"
      markerWidth="6" markerHeight="6" orient="auto-start-reverse">
      <path d="M 0 0 L 10 5 L 0 10 z"/></marker></defs>`];

  for (const {from, to} of arrows) {
    const a = el.querySelector(`#cb-${CSS.escape(from)}`);
    const b = el.querySelector(`#cb-${CSS.escape(to)}`);
    if (!a || !b) continue;
    const ra = a.getBoundingClientRect(), rb = b.getBoundingClientRect();
    const x1 = ra.left + ra.width - box.left, y1 = ra.top + ra.height / 2 - box.top;
    const x2 = rb.left - box.left, y2 = rb.top + rb.height / 2 - box.top;
    // Bow the curve so a move within one day is still legible.
    const lift = Math.max(24, Math.abs(x2 - x1) * 0.25);
    parts.push(`<path class="cal-arrow"
      d="M ${x1} ${y1} C ${x1 + lift} ${y1}, ${x2 - lift} ${y2}, ${x2} ${y2}"
      marker-end="url(#ah)"/>`);
  }
  svg.innerHTML = parts.join("");
}

// -- drag to select a range on one day --------------------------------

function wireSelection(el, dayStart, dayEnd, onSelect) {
  const span = (dayEnd - dayStart) * 60;
  let anchor = null;

  const slotAt = (dayEl, clientY) => {
    const r = dayEl.getBoundingClientRect();
    const mins = dayStart * 60 + ((clientY - r.top) / r.height) * span;
    return Math.max(dayStart * 60, Math.min(dayEnd * 60, Math.round(mins / 30) * 30));
  };
  const label = (mins) =>
    `${String(Math.floor(mins / 60)).padStart(2, "0")}:${String(mins % 60).padStart(2, "0")}`;

  el.addEventListener("mousedown", (e) => {
    const day = e.target.closest(".cal-day");
    if (!day || e.target.closest(".cal-block")) return;
    anchor = {day, at: slotAt(day, e.clientY)};
    e.preventDefault();
  });
  el.addEventListener("mouseup", (e) => {
    if (!anchor) return;
    const day = e.target.closest(".cal-day") || anchor.day;
    if (day !== anchor.day) { anchor = null; return; }
    const other = slotAt(day, e.clientY);
    const from = Math.min(anchor.at, other), to = Math.max(anchor.at, other);
    const chosen = anchor;
    anchor = null;
    if (to > from) onSelect(chosen.day.dataset.date, label(from), label(to), !e.shiftKey);
  });
}

window.renderCalendar = renderCalendar;
