"""Save and restore a session, so accumulated history survives a restart.

**This reaches past calendar_store's public API on purpose.** Real persistence
belongs inside that package and does not exist yet, and rebuilding state
through the public API would issue fresh appointment ids — which would break
the `supersedes` chain that makes the history readable. Everything that knows
about calendar_store's internals is in this one file, so it is obvious what to
delete when the store learns to persist itself.
"""
from __future__ import annotations

import itertools
import json
from datetime import date, datetime, time
from pathlib import Path
from typing import Any, Dict

from calendar_store import (
    Appointment,
    AppointmentStatus,
    AvailabilityException,
    ClientAvailabilityRule,
    Kind,
    Origin,
    TimeSegment,
)

from .billing import BillingSettings, Charge, Payment
from .state import Approval, AssistantSettings, ChatSettings, Client, Message, Plan, Request, Series, World


def _dt(value):
    return value.isoformat() if value else None


def _back(value):
    return datetime.fromisoformat(value) if value else None


def save(world: World, path: Path) -> None:
    payload = {
        "clients": [{"id": c.id, "name": c.name, "per_day": c.per_day,
                     "per_week": c.per_week, "billing_mode": c.billing_mode}
                    for c in world.clients.values()],
        "settings": {
            "alpha": world.alpha,
            "max_displacements": world.max_displacements,
        },
        "rules": [
            {"id": r.id, "client_id": r.client_id, "weekday": r.weekday,
             "start_time": r.start_time.isoformat(), "end_time": r.end_time.isoformat(),
             "effective_from": r.effective_from.isoformat(),
             "effective_until": r.effective_until.isoformat() if r.effective_until else None}
            for r in world.store._rules
        ],
        "exceptions": [
            {"id": e.id, "client_id": e.client_id, "date": e.date.isoformat(),
             "start_time": e.start_time.isoformat(), "end_time": e.end_time.isoformat(),
             "kind": e.kind.value}
            for e in world.store._exceptions
        ],
        "appointments": [
            {"id": a.id, "client_id": a.client_id, "service_type_id": a.service_type_id,
             "start": a.range.start.isoformat(), "end": a.range.end.isoformat(),
             "locked": a.locked, "notes": a.notes, "status": a.status.value,
             "origin": a.origin.value, "supersedes": a.supersedes,
             "preferred_start": _dt(a.preferred_start)}
            for a in world.store._appointments
        ],
        "services": [
            {"id": s.id, "name": s.name, "duration_minutes": s.duration_minutes,
             "price_minor_units": s.price_minor_units, "active": s.active,
             "client_bookable": s.client_bookable, "description": s.description}
            for s in world.catalogue.services(include_inactive=True)
        ],
        # The workflow, not just the calendar. Without these a restored session
        # has the bookings but an empty queue: no pending requests, no draft to
        # approve, nobody waiting to answer — which is most of what there is to
        # play with.
        "requests": [
            {"id": r.id, "client_id": r.client_id, "service_id": r.service_id,
             "duration_minutes": r.duration_minutes, "status": r.status,
             "replaces_appointment_id": r.replaces_appointment_id,
             "origin": r.origin.value, "note": r.note,
             "preferred_start": _dt(r.preferred_start),
             "series_id": r.series_id,
             "lapsed_reason": r.lapsed_reason, "seen_by": sorted(r.seen_by)}
            for r in world.requests.values()
        ],
        "series": [
            {"id": x.id, "client_id": x.client_id, "service_id": x.service_id,
             "first_start": _dt(x.first_start), "every_weeks": x.every_weeks,
             "until": x.until.isoformat() if x.until else None,
             "status": x.status, "raised": sorted(d.isoformat() for d in x.raised)}
            for x in world.series.values()
        ],
        "plans": [
            {"id": p.id, "status": p.status, "reason": p.reason, "detail": p.detail,
             "params": p.params, "metrics": p.metrics,
             "placements": [{**x, "start": _dt(x["start"]), "end": _dt(x["end"])}
                            for x in p.placements],
             "displacements": [
                 {**x, "was_start": _dt(x["was_start"]), "was_end": _dt(x["was_end"]),
                  "now_start": _dt(x["now_start"]), "now_end": _dt(x["now_end"])}
                 for x in p.displacements]}
            for p in world.plans.values()
        ],
        "approvals": [
            {"id": a.id, "plan_id": a.plan_id, "client_id": a.client_id,
             "kind": a.kind, "status": a.status, "applied": a.applied,
             "request_id": a.request_id, "appointment_id": a.appointment_id,
             "depends_on": list(a.depends_on),
             "was_start": _dt(a.was_start), "was_end": _dt(a.was_end),
             "now_start": _dt(a.now_start), "now_end": _dt(a.now_end)}
            for a in world.approvals.values()
        ],
        "immovable": sorted(world.immovable),
        "scheduler": {
            "auto_run": world.policy.auto_run,
            "urgency_hours": world.policy.urgency_hours,
            "retry_after_minutes": world.policy.retry_after_minutes,
            "horizon_days": world.policy.horizon_days,
            "scope_to_horizon": world.policy.scope_to_horizon,
            "rehouse_orphans_on_run": world.policy.rehouse_orphans_on_run,
            "last_run": _dt(world.last_run),
        },
        "practice_name": world.practice_name,
        "billing": vars(world.billing),
        "charges": [{**vars(c), "created": _dt(c.created)} for c in world.charges.values()],
        "payments": [{**vars(p), "created": _dt(p.created)} for p in world.payments.values()],
        # A simulated checkout's outcome lives in the processor; without it a
        # payment in progress across a restart would read as expired.
        "simulated_outcomes": getattr(world.processor, "outcomes", {}),
        "feed_tokens": world.feed_tokens,
        "cancel_notices": sorted(world.cancel_notices),
        "audit": world.audit,
        "messages": [{**vars(m), "at": _dt(m.at), "read_at": _dt(m.read_at)}
                     for m in world.messages],
        "chat": vars(world.chat),
        "assistant": {"enabled": world.assistant.enabled,
                      "opted_in": sorted(world.assistant.opted_in)},
        "log": world.log,
    }
    path.write_text(json.dumps(payload, indent=2))


def load(path: Path) -> World:
    """Restore a session.

    Raises on anything it cannot read. The format changes as this mock does,
    and a half-restored world is worse than a fresh one — see `load_or_new`.
    """
    payload: Dict[str, Any] = json.loads(path.read_text())
    world = World()

    for entry in payload.get("clients", []):
        world.clients[entry["id"]] = Client(
            id=entry["id"], name=entry["name"],
            # Sessions saved before limits existed get the defaults.
            per_day=entry.get("per_day", 1), per_week=entry.get("per_week"),
            billing_mode=entry.get("billing_mode"))

    settings = payload.get("settings", {})
    world.alpha = settings.get("alpha", world.alpha)
    world.max_displacements = settings.get(
        "max_displacements", world.max_displacements
    )

    for entry in payload.get("services", []):
        world.catalogue.add_service(
            entry["id"], entry["name"], entry["duration_minutes"],
            entry["price_minor_units"],
            client_bookable=entry.get("client_bookable", True),
            description=entry.get("description"),
        )
        if not entry.get("active", True):
            world.catalogue.deactivate_service(entry["id"])

    world.store._rules = [
        ClientAvailabilityRule(
            id=r["id"], client_id=r["client_id"], weekday=r["weekday"],
            start_time=time.fromisoformat(r["start_time"]),
            end_time=time.fromisoformat(r["end_time"]),
            effective_from=date.fromisoformat(r["effective_from"]),
            effective_until=(date.fromisoformat(r["effective_until"])
                             if r["effective_until"] else None),
        )
        for r in payload.get("rules", [])
    ]
    world.store._exceptions = [
        AvailabilityException(
            id=e["id"], client_id=e["client_id"], date=date.fromisoformat(e["date"]),
            start_time=time.fromisoformat(e["start_time"]),
            end_time=time.fromisoformat(e["end_time"]), kind=Kind(e["kind"]),
        )
        for e in payload.get("exceptions", [])
    ]
    world.store._appointments = [
        Appointment(
            id=a["id"], client_id=a["client_id"], service_type_id=a["service_type_id"],
            range=TimeSegment(datetime.fromisoformat(a["start"]),
                              datetime.fromisoformat(a["end"])),
            locked=a["locked"], notes=a["notes"],
            status=AppointmentStatus(a["status"]), origin=Origin(a["origin"]),
            supersedes=a["supersedes"],
            preferred_start=_back(a.get("preferred_start")),
        )
        for a in payload.get("appointments", [])
    ]
    for entry in payload.get("requests", []):
        world.requests[entry["id"]] = Request(
            id=entry["id"], client_id=entry["client_id"],
            service_id=entry["service_id"],
            duration_minutes=entry["duration_minutes"], status=entry["status"],
            replaces_appointment_id=entry.get("replaces_appointment_id"),
            origin=Origin(entry.get("origin", "client")),
            note=entry.get("note", ""),
            preferred_start=_back(entry.get("preferred_start")),
            series_id=entry.get("series_id"),
            lapsed_reason=entry.get("lapsed_reason", ""),
            seen_by=set(entry.get("seen_by", [])),
        )
    for entry in payload.get("series", []):
        world.series[entry["id"]] = Series(
            id=entry["id"], client_id=entry["client_id"],
            service_id=entry["service_id"],
            first_start=_back(entry["first_start"]),
            every_weeks=entry["every_weeks"],
            until=date.fromisoformat(entry["until"]) if entry.get("until") else None,
            status=entry["status"],
            raised={date.fromisoformat(d) for d in entry.get("raised", [])},
        )
    for entry in payload.get("plans", []):
        world.plans[entry["id"]] = Plan(
            id=entry["id"], status=entry["status"], reason=entry["reason"],
            detail=entry["detail"], params=entry["params"], metrics=entry["metrics"],
            placements=[{**x, "start": _back(x["start"]), "end": _back(x["end"])}
                        for x in entry["placements"]],
            displacements=[
                {**x, "was_start": _back(x["was_start"]), "was_end": _back(x["was_end"]),
                 "now_start": _back(x["now_start"]), "now_end": _back(x["now_end"])}
                for x in entry["displacements"]],
        )
    for entry in payload.get("approvals", []):
        world.approvals[entry["id"]] = Approval(
            id=entry["id"], plan_id=entry["plan_id"], client_id=entry["client_id"],
            kind=entry["kind"], status=entry["status"], applied=entry["applied"],
            request_id=entry["request_id"], appointment_id=entry["appointment_id"],
            depends_on=tuple(entry["depends_on"]),
            was_start=_back(entry["was_start"]), was_end=_back(entry["was_end"]),
            now_start=_back(entry["now_start"]), now_end=_back(entry["now_end"]),
        )
    world.immovable = set(payload.get("immovable", []))

    scheduler = payload.get("scheduler", {})
    world.policy.auto_run = scheduler.get("auto_run", world.policy.auto_run)
    world.policy.urgency_hours = scheduler.get(
        "urgency_hours", world.policy.urgency_hours)
    world.policy.retry_after_minutes = scheduler.get(
        "retry_after_minutes", world.policy.retry_after_minutes)
    world.policy.horizon_days = scheduler.get(
        "horizon_days", world.policy.horizon_days)
    world.policy.scope_to_horizon = scheduler.get(
        "scope_to_horizon", world.policy.scope_to_horizon)
    world.policy.rehouse_orphans_on_run = scheduler.get(
        "rehouse_orphans_on_run", world.policy.rehouse_orphans_on_run)
    world.last_run = _back(scheduler.get("last_run"))

    world.log = payload.get("log", [])
    world.practice_name = payload.get("practice_name", world.practice_name)
    world.billing = BillingSettings(**payload.get("billing", {}))
    for entry in payload.get("charges", []):
        world.charges[entry["id"]] = Charge(**{**entry, "created": _back(entry["created"])})
    for entry in payload.get("payments", []):
        world.payments[entry["id"]] = Payment(**{**entry, "created": _back(entry["created"])})
    if hasattr(world.processor, "outcomes"):
        world.processor.outcomes.update(
            {k: tuple(v) for k, v in payload.get("simulated_outcomes", {}).items()})
    # Kept across restarts, or every subscribed calendar would break on each one.
    world.feed_tokens = dict(payload.get("feed_tokens", {}))
    world.cancel_notices = set(payload.get("cancel_notices", []))
    world.audit = list(payload.get("audit", []))
    world.messages = [Message(**{**m, "at": _back(m["at"]), "read_at": _back(m.get("read_at"))})
                      for m in payload.get("messages", [])]
    world.chat = ChatSettings(**payload.get("chat", {}))
    saved = payload.get("assistant", {})
    world.assistant = AssistantSettings(enabled=saved.get("enabled", False),
                                        opted_in=set(saved.get("opted_in", [])))

    # Restart id issuing above everything restored, so a reloaded session
    # cannot mint an id that already exists.
    highest = max(
        [0]
        + [r.id for r in world.store._rules]
        + [e.id for e in world.store._exceptions]
        + [a.id for a in world.store._appointments]
    )
    world.store._ids = itertools.count(highest + 1)
    # The World issues its own ids for requests, plans and approvals, and those
    # are now restored too, so it must start above them as well.
    world._ids = itertools.count(max(
        [highest] + list(world.requests) + list(world.plans) + list(world.approvals)
        + list(world.series) + list(world.charges) + list(world.payments)
        + [m.id for m in world.messages]
    ) + 1)
    return world


def load_or_new(path: Path) -> World:
    """Restore if possible, otherwise start clean rather than refusing to run.

    A saved session written by an older version of this mock would otherwise
    stop the server booting, which is a poor trade for a development tool: the
    session is disposable, the ability to start is not.
    """
    if not path.exists():
        return World()
    try:
        return load(path)
    except Exception as exc:
        stale = path.with_suffix(".unreadable.json")
        path.rename(stale)
        world = World()
        world._note(f"could not read the saved session ({exc}); kept it at {stale.name}")
        return world
