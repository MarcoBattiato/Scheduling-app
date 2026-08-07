"""A standalone HTML view of a scenario and what the solver made of it.

Shows every object involved, in the order they matter: what the provider had
available, what was already booked, what that leaves free, where each request
wanted to go, where it actually landed, and which leftover gaps are dead.

Self-contained — inline CSS, no scripts, no network — so the output file can be
opened straight from disk or mailed to someone.
"""
from __future__ import annotations

import html
from datetime import date, datetime, time, timedelta
from typing import Dict, Optional, Sequence

from .models import CostConfig, PlacementResult
from .visualize import Gap, gaps_left

def _span(item):
    inner = getattr(item, "range", item)
    return inner.start, inner.end


def _axis_hours(items: Sequence) -> tuple:
    """The clock range every day's chart is drawn against, as whole hours."""
    bounds = [_span(i) for i in items if i is not None]
    if not bounds:
        return 9, 17
    low = min(s.hour for s, _ in bounds)
    high = max(e.hour + (1 if e.minute or e.second else 0) for _, e in bounds)
    return low, max(high, low + 1)


def page(title: str, sections: Sequence[str]) -> str:
    """Wrap one or more `section()` fragments into a complete document.

    Several sections in one page is the useful case: the same scenario solved
    at different `alpha` values, so the trade-off is visible side by side
    rather than by flipping between files.
    """
    return _PAGE.format(title=html.escape(title), body="\n".join(s for s in sections if s))


def to_html(provider_free: Sequence, result: Optional[PlacementResult] = None, **kw) -> str:
    """Convenience for the single-solve case."""
    title = kw.pop("title", "Placement scenario")
    return page(title, [section(provider_free, result, heading=title, **kw)])


def section(
    provider_free: Sequence,
    result: Optional[PlacementResult] = None,
    *,
    heading: str = "Placement scenario",
    config: Optional[CostConfig] = None,
    provider_availability: Sequence = (),
    booked: Sequence = (),
    requests: Sequence = (),
    client_availability: Optional[Dict[str, Sequence]] = None,
    movable: Sequence = (),
) -> str:
    config = config or CostConfig()
    placements = list(result.placements) if result else []
    displacements = list(result.displacements) if result else []
    gaps = gaps_left(provider_free, placements, config,
                     movable=movable, displacements=displacements)
    client_availability = client_availability or {}

    days = sorted(
        {_span(s)[0].date() for s in provider_availability}
        | {_span(s)[0].date() for s in provider_free}
        | {_span(b)[0].date() for b in booked}
        | {p.range.start.date() for p in placements}
        | {w.start.date() for r in requests for w in r.desired}
        | {d.was.start.date() for d in displacements}
        | {d.now.start.date() for d in displacements}
    )

    # One clock range for every day, so an hour is the same width wherever it
    # is drawn. Scaling each day to its own contents makes equal appointments
    # look unequal — the same booking appearing narrower after being moved to
    # a longer day reads as the booking having been shortened.
    hours = _axis_hours(
        list(provider_availability) + list(provider_free) + list(booked)
        + list(movable) + [p.range for p in placements]
        + [d.was for d in displacements] + [d.now for d in displacements]
    )

    body = [
        f"<h1>{html.escape(heading)}</h1>",
        _meta(config),
        _summary(result),
    ]
    if not days:
        body.append("<p class='empty'>Nothing to show — no availability, no requests.</p>")
    for day in days:
        body.append(
            _day(
                day, provider_availability, booked, provider_free, placements,
                gaps, requests, client_availability, displacements, movable, hours,
            )
        )
    body.append(_rebooking_note(displacements))
    body.append(_unplaced_note(result, requests))
    return "<article class='run'>" + "\n".join(p for p in body if p) + "</article>"


def _meta(config: CostConfig) -> str:
    services = "/".join(str(d) for d in config.service_durations)
    return (
        "<div class='meta'>"
        f"<span><b>alpha</b> {config.alpha:g}</span>"
        f"<span><b>grid</b> {config.grid_minutes}m</span>"
        f"<span><b>services</b> {services}m</span>"
        f"<span class='hint'>alpha 0 = earliest-first · 1 = packing-first</span>"
        "</div>"
    )


def _summary(result: Optional[PlacementResult]) -> str:
    if result is None:
        return ""
    total = len(result.placements) + len(result.unplaced)
    ok = len(result.placements)
    state = "good" if ok == total else "warn"
    rebooked = (
        f"<span class='pill move'>rebooked {len(result.displacements)} "
        f"already-agreed booking{'s' if len(result.displacements) != 1 else ''}</span>"
        if result.displacements else ""
    )
    return (
        "<div class='summary'>"
        f"<span class='pill {state}'>placed {ok}/{total}</span>"
        f"{rebooked}"
        f"<span class='pill'>fragmentation {result.fragmentation_minutes}m</span>"
        f"<span class='pill'>off-preference {result.preference_gap_minutes}m</span>"
        "</div>"
    )


def _rebooking_note(displacements: Sequence) -> str:
    """Spelled out separately because these are the disruptive part of the
    plan — each one is a client who has to be asked, and may say no.
    """
    if not displacements:
        return ""
    rows = "".join(
        f"<li><b>{html.escape(d.client_id)}</b> "
        f"({html.escape(d.appointment_id)}) &mdash; "
        f"{d.was.start:%a %d %b %H:%M} &rarr; {d.now.start:%a %d %b %H:%M} "
        f"<span class='hint'>moves {d.shift_minutes} min</span></li>"
        for d in displacements
    )
    return (
        "<section class='rebooked'><h2>Already-agreed bookings this plan wants "
        f"to move ({len(displacements)})</h2><ul>{rows}</ul>"
        "<p class='hint'>Proposals, not facts — each needs the client's "
        "agreement, and any of them may be refused.</p></section>"
    )


def _unplaced_note(result: Optional[PlacementResult], requests: Sequence) -> str:
    if not result or not result.unplaced:
        return ""
    by_id = {r.id: r for r in requests}
    rows = []
    for request_id in result.unplaced:
        request = by_id.get(request_id)
        detail = (
            f"{html.escape(request.client_id)}, {request.duration_minutes}m"
            if request else ""
        )
        rows.append(f"<li><b>{html.escape(request_id)}</b> {detail}</li>")
    return (
        "<section class='unplaced'><h2>Not placed</h2><ul>"
        + "".join(rows)
        + "</ul><p class='hint'>A partial solution is a valid outcome — these "
        "simply had no feasible slot at this alpha.</p></section>"
    )


def _day(
    day: date,
    provider_availability: Sequence,
    booked: Sequence,
    provider_free: Sequence,
    placements: Sequence,
    gaps: Sequence[Gap],
    requests: Sequence,
    client_availability: Dict[str, Sequence],
    displacements: Sequence = (),
    movable: Sequence = (),
    hours: tuple = (9, 17),
) -> str:
    def on_day(items):
        return [i for i in items if _span(i)[0].date() == day]

    availability = on_day(provider_availability)
    booked_today = on_day(booked)
    free_today = on_day(provider_free)
    placed_today = sorted(on_day(placements), key=lambda p: p.range.start)
    gaps_today = [g for g in gaps if g.start.date() == day]
    windows_today = [
        (r, [w for w in r.desired if w.start.date() <= day <= w.end.date()])
        for r in requests
    ]
    windows_today = [(r, ws) for r, ws in windows_today if ws]

    vacating = [d for d in displacements if d.was.start.date() == day]
    arriving = [d for d in displacements if d.now.start.date() == day]
    stayed = {d.appointment_id for d in displacements}

    everything = (
        [_span(i) for i in availability + booked_today + free_today]
        + [(p.range.start, p.range.end) for p in placed_today]
        + [(d.now.start, d.now.end) for d in arriving]
        + [_span(m) for m in on_day(movable)]
    )
    if not everything:
        return ""
    start_hour, end_hour = hours
    low = datetime.combine(day, time(start_hour))
    high = datetime.combine(day, time.min) + timedelta(hours=end_hour)
    total = (high - low).total_seconds() or 1

    def bar(start, end, css, label="", tip=""):
        start, end = max(start, low), min(end, high)
        if end <= start:
            return ""
        left = (start - low).total_seconds() / total * 100
        width = (end - start).total_seconds() / total * 100
        tip = tip or f"{start:%H:%M}–{end:%H:%M}"
        return (
            f"<div class='bar {css}' style='left:{left:.4f}%;width:{width:.4f}%' "
            f"title='{html.escape(tip)}'>{html.escape(label)}</div>"
        )

    lanes = [_ruler(low, high, total)]

    lanes.append(_lane("Availability", "".join(
        bar(*_span(s), css="avail") for s in availability)))
    if booked_today:
        lanes.append(_lane("Already booked", "".join(
            bar(*_span(b), css="booked",
                label=getattr(b, "client_id", ""),
                tip=f"{getattr(b, 'client_id', 'booked')} "
                    f"{_span(b)[0]:%H:%M}–{_span(b)[1]:%H:%M}")
            for b in booked_today)))
    if movable:
        lanes.append(_lane("Movable bookings", "".join(
            bar(*_span(m), css="held" if m.id not in stayed else "vacated",
                label=m.client_id,
                tip=f"{m.client_id} — "
                    + ("stays put" if m.id not in stayed else "asked to move away"))
            for m in on_day(movable))))

    lanes.append(_lane("Free", "".join(
        bar(*_span(s), css="free") for s in free_today)))

    if vacating or arriving:
        lanes.append(_lane("Rebooked", "".join(
            [bar(d.was.start, d.was.end, css="vacated", label="↳ freed",
                 tip=f"{d.client_id} moves out of "
                     f"{d.was.start:%H:%M}–{d.was.end:%H:%M}")
             for d in vacating]
            + [bar(d.now.start, d.now.end, css="arrived", label=f"{d.client_id} ↴",
                   tip=f"{d.client_id} moves in from {d.was.start:%a %H:%M}")
               for d in arriving])))

    lanes.append(_lane("Placed", "".join(
        bar(p.range.start, p.range.end, css="placed",
            label=f"{p.request_id} · {p.client_id}",
            tip=f"{p.request_id} · {p.client_id} · "
                f"{p.range.start:%H:%M}–{p.range.end:%H:%M}")
        for p in placed_today)))

    if gaps_today:
        lanes.append(_lane("Gaps left", "".join(
            bar(g.start, g.end,
                css="gap-ok" if g.usable else "gap-bad",
                label=f"{g.minutes}m" if g.minutes >= 45 else "",
                tip=f"{g.start:%H:%M}–{g.end:%H:%M} · {g.minutes}m · "
                    + ("reusable" if g.usable else f"{g.wasted_minutes}m wasted"))
            for g in gaps_today)))

    if windows_today:
        lanes.append("<div class='divider'>requested windows</div>")
        placed_ids = {p.request_id for p in placements}
        for request, windows in windows_today:
            css = "want" if request.id in placed_ids else "want-unplaced"
            name = f"{request.id} · {request.client_id} · {request.duration_minutes}m"
            lanes.append(_lane(name, "".join(
                bar(w.start, w.end, css=css,
                    tip=f"{name} wants {w.start:%a %H:%M}–{w.end:%a %H:%M}")
                for w in windows)))
            # Keyed by client, not by request — several requests can belong to
            # the same client.
            same_day = [
                s for s in client_availability.get(request.client_id, ())
                if _span(s)[0].date() == day
            ]
            if same_day:
                lanes.append(_lane("↳ their availability", "".join(
                    bar(*_span(s), css="client-avail",
                        tip=f"{request.client_id} available "
                            f"{_span(s)[0]:%H:%M}–{_span(s)[1]:%H:%M} "
                            "(shown only — not enforced by this pass)")
                    for s in same_day)))

    return (
        f"<section class='day'><h2>{day:%A %-d %B %Y}</h2>"
        f"<div class='chart'>{''.join(lanes)}</div></section>"
    )


def _lane(name: str, bars: str) -> str:
    return (
        "<div class='lane'>"
        f"<div class='lane-name'>{html.escape(name)}</div>"
        f"<div class='track'>{bars}</div>"
        "</div>"
    )


def _ruler(low: datetime, high: datetime, total: float) -> str:
    ticks = []
    cursor = low
    while cursor <= high:
        left = (cursor - low).total_seconds() / total * 100
        ticks.append(
            f"<div class='tick' style='left:{left:.4f}%'>"
            f"<span>{cursor:%H:%M}</span></div>"
        )
        cursor += timedelta(hours=1)
    return (
        "<div class='lane ruler'><div class='lane-name'></div>"
        f"<div class='track'>{''.join(ticks)}</div></div>"
    )


_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}</title>
<style>
:root {{
  --bg:#ffffff; --fg:#111827; --muted:#6b7280; --line:#e5e7eb; --panel:#f9fafb;
  --avail:#dbeafe; --free:#bbf7d0; --booked:#9ca3af; --placed:#2563eb;
  --gap-ok:#a7f3d0; --gap-bad:#fca5a5; --want:#e9d5ff; --want-un:#fed7aa;
  --held:#cbd5e1; --vacated:#fde68a; --arrived:#a78bfa;
}}
@media (prefers-color-scheme: dark) {{
  :root {{
    --bg:#0b0f19; --fg:#e5e7eb; --muted:#9ca3af; --line:#1f2937; --panel:#111827;
    --avail:#1e3a5f; --free:#14532d; --booked:#4b5563; --placed:#3b82f6;
    --gap-ok:#065f46; --gap-bad:#7f1d1d; --want:#4c1d95; --want-un:#7c2d12;
    --held:#334155; --vacated:#78350f; --arrived:#5b21b6;
  }}
}}
* {{ box-sizing:border-box; }}
body {{ margin:0; padding:24px; background:var(--bg); color:var(--fg);
  font:14px/1.5 ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif; }}
h1 {{ font-size:20px; margin:0 0 12px; }}
h2 {{ font-size:15px; margin:0 0 10px; font-weight:600; }}
.meta, .summary {{ display:flex; flex-wrap:wrap; gap:10px; margin-bottom:12px;
  align-items:center; color:var(--muted); }}
.meta b {{ color:var(--fg); font-weight:600; }}
.hint {{ font-size:12px; color:var(--muted); }}
.pill {{ background:var(--panel); border:1px solid var(--line); border-radius:999px;
  padding:3px 10px; font-size:12px; color:var(--fg); }}
.pill.good {{ border-color:#16a34a; }}
.pill.warn {{ border-color:#ea580c; }}
.day {{ border:1px solid var(--line); border-radius:10px; padding:14px;
  margin-bottom:16px; background:var(--panel); overflow-x:auto; }}
.chart {{ min-width:640px; }}
.lane {{ display:grid; grid-template-columns:180px 1fr; align-items:center;
  gap:10px; margin-bottom:4px; }}
.lane-name {{ font-size:12px; color:var(--muted); text-align:right;
  white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }}
.track {{ position:relative; height:24px; border-radius:5px;
  background:repeating-linear-gradient(90deg,transparent,transparent
    calc(8.3333% - 1px),var(--line) calc(8.3333% - 1px),var(--line) 8.3333%); }}
.bar {{ position:absolute; top:2px; bottom:2px; border-radius:4px;
  font-size:11px; line-height:20px; padding:0 5px; overflow:hidden;
  white-space:nowrap; color:var(--fg); }}
.bar.avail {{ background:var(--avail); }}
.bar.free {{ background:var(--free); }}
.bar.booked {{ background:var(--booked); color:#fff; }}
.bar.placed {{ background:var(--placed); color:#fff; font-weight:600; }}
.bar.gap-ok {{ background:var(--gap-ok); }}
.bar.gap-bad {{ background:var(--gap-bad); }}
.bar.want {{ background:var(--want); }}
.bar.want-unplaced {{ background:var(--want-un); }}
.bar.client-avail {{ background:transparent; border:1px dashed var(--muted); }}
.bar.held {{ background:var(--held); }}
.bar.vacated {{ background:var(--vacated); border:1px dashed #b45309; }}
.bar.arrived {{ background:var(--arrived); color:#fff; font-weight:600; }}
.pill.move {{ border-color:#7c3aed; }}
.rebooked {{ border:1px solid #7c3aed; border-radius:10px; padding:12px 16px;
  margin-bottom:16px; }}
.rebooked ul {{ margin:6px 0; padding-left:18px; }}
.ruler .track {{ height:16px; background:none; }}
.tick {{ position:absolute; top:0; border-left:1px solid var(--line);
  height:16px; padding-left:3px; }}
.tick span {{ font-size:10px; color:var(--muted); }}
.divider {{ font-size:11px; text-transform:uppercase; letter-spacing:.06em;
  color:var(--muted); margin:12px 0 6px; padding-top:8px;
  border-top:1px solid var(--line); }}
.unplaced {{ border:1px solid #ea580c; border-radius:10px; padding:12px 16px; }}
.unplaced ul {{ margin:6px 0; padding-left:18px; }}
.empty {{ color:var(--muted); }}
.run {{ margin-bottom:28px; }}
.run + .run {{ border-top:2px solid var(--line); padding-top:22px; }}
</style></head><body>
{body}
</body></html>
"""
