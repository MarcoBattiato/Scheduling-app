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

from .state import Client, World


def save(world: World, path: Path) -> None:
    payload = {
        "clients": [{"id": c.id, "name": c.name} for c in world.clients.values()],
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
             "origin": a.origin.value, "supersedes": a.supersedes}
            for a in world.store._appointments
        ],
        "services": [
            {"id": s.id, "name": s.name, "duration_minutes": s.duration_minutes,
             "price_minor_units": s.price_minor_units, "active": s.active,
             "client_bookable": s.client_bookable, "description": s.description}
            for s in world.catalogue.services(include_inactive=True)
        ],
        "log": world.log,
    }
    path.write_text(json.dumps(payload, indent=2))


def load(path: Path) -> World:
    payload: Dict[str, Any] = json.loads(path.read_text())
    world = World()

    for entry in payload.get("clients", []):
        world.clients[entry["id"]] = Client(id=entry["id"], name=entry["name"])

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
        )
        for a in payload.get("appointments", [])
    ]
    world.log = payload.get("log", [])

    # Restart id issuing above everything restored, so a reloaded session
    # cannot mint an id that already exists.
    highest = max(
        [0]
        + [r.id for r in world.store._rules]
        + [e.id for e in world.store._exceptions]
        + [a.id for a in world.store._appointments]
    )
    world.store._ids = itertools.count(highest + 1)
    world._ids = itertools.count(highest + 1000)
    return world
