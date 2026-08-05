"""HTTP layer: a thin translation of JSON into `World` method calls.

Deliberately holds no logic of its own — anything interesting belongs in
`state.World`, so the mock's behaviour can be tested without a browser.
"""
from __future__ import annotations

import os
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import List, Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from calendar_store import Party

from . import persistence
from .state import PROVIDER, World

STATIC = Path(__file__).parent / "static"
SNAPSHOT = Path(os.environ.get("MOCK_UI_SNAPSHOT", "mock_ui_session.json"))

app = FastAPI(title="Scheduling mock UI")
world = persistence.load(SNAPSHOT) if SNAPSHOT.exists() else World()


# -- payloads -------------------------------------------------------


class ClientIn(BaseModel):
    id: str
    name: str = ""


class WeeklyIn(BaseModel):
    client_id: str
    ranges: List[dict] = []


class RequestIn(BaseModel):
    client_id: str
    duration_minutes: int
    windows: List[dict]


class MoveIn(BaseModel):
    start: str
    end: str


class ApprovalIn(BaseModel):
    accept: bool


class CancelIn(BaseModel):
    by_provider: bool = False


class AttendanceIn(BaseModel):
    attended: bool


class SettingsIn(BaseModel):
    alpha: Optional[float] = None
    max_displacements: Optional[int] = None


# -- routes ---------------------------------------------------------


@app.get("/api/state")
def get_state():
    return world.snapshot()


@app.post("/api/clients")
def add_client(payload: ClientIn):
    return {"client": vars(world.add_client(payload.id, payload.name))}


@app.post("/api/availability")
def set_availability(payload: WeeklyIn):
    world.set_weekly_availability(payload.client_id, payload.ranges)
    return {"ok": True}


@app.post("/api/requests")
def submit_request(payload: RequestIn):
    if payload.duration_minutes <= 0:
        raise HTTPException(400, "duration must be positive")
    if not payload.windows:
        raise HTTPException(400, "a request needs at least one window")
    request = world.submit_request(
        payload.client_id, payload.duration_minutes, payload.windows
    )
    outcome = world.solve()
    return {"request_id": request.id, "outcome": outcome}


@app.post("/api/requests/{request_id}/withdraw")
def withdraw_request(request_id: int):
    if request_id not in world.requests:
        raise HTTPException(404, "no such request")
    world.withdraw_request(request_id)
    return {"ok": True}


@app.post("/api/appointments/{appointment_id}/cancel")
def cancel_appointment(appointment_id: int, payload: CancelIn = CancelIn()):
    try:
        world.cancel_appointment(
            appointment_id,
            by=Party.PROVIDER if payload.by_provider else Party.CLIENT,
        )
    except KeyError:
        raise HTTPException(404, "no such appointment") from None
    world.solve()
    return {"ok": True}


@app.post("/api/appointments/{appointment_id}/attendance")
def mark_attendance(appointment_id: int, payload: AttendanceIn):
    try:
        world.mark_attendance(appointment_id, payload.attended)
    except KeyError:
        raise HTTPException(404, "no such appointment") from None
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from None
    return {"ok": True}


@app.post("/api/appointments/{appointment_id}/move")
def move_appointment(appointment_id: int, payload: MoveIn):
    try:
        world.move_appointment(
            appointment_id,
            datetime.fromisoformat(payload.start),
            datetime.fromisoformat(payload.end),
        )
    except KeyError:
        raise HTTPException(404, "no such appointment") from None
    return {"ok": True}


@app.post("/api/approvals/{approval_id}")
def respond(approval_id: int, payload: ApprovalIn):
    if approval_id not in world.approvals:
        raise HTTPException(404, "no such approval")
    world.respond_to_approval(approval_id, payload.accept)
    return {"ok": True}


@app.post("/api/solve")
def solve():
    return world.solve()


@app.post("/api/settings")
def settings(payload: SettingsIn):
    if payload.alpha is not None:
        world.alpha = max(0.0, min(1.0, payload.alpha))
    if payload.max_displacements is not None:
        world.max_displacements = max(0, payload.max_displacements)
    return {"ok": True}


@app.post("/api/snapshot/save")
def snapshot_save():
    persistence.save(world, SNAPSHOT)
    return {"saved": str(SNAPSHOT)}


@app.post("/api/reset")
def reset(seed: bool = True):
    global world
    world = World()
    if seed:
        seed_world(world)
    return {"ok": True}


# -- a world worth opening on the first run --------------------------


def seed_world(target: World) -> None:
    """Enough of a calendar that the first page load shows something.

    Weekdays 09:00-17:00 for the provider, three clients with different
    availability, and one booking each so history starts accumulating rather
    than requiring setup before anything can be tried.
    """
    target.add_client("alice", "Alice")
    target.add_client("bob", "Bob")
    target.add_client("carol", "Carol")

    weekdays = [{"weekday": d, "from": "09:00", "to": "17:00"} for d in range(5)]
    target.set_weekly_availability(PROVIDER, weekdays)
    target.set_weekly_availability("alice", [
        {"weekday": d, "from": "09:00", "to": "13:00"} for d in range(5)
    ])
    target.set_weekly_availability("bob", [
        {"weekday": d, "from": "13:00", "to": "17:00"} for d in (0, 2, 4)
    ])
    target.set_weekly_availability("carol", [
        {"weekday": d, "from": "10:00", "to": "16:00"} for d in (1, 3)
    ])

    monday = date.today() + timedelta(days=(7 - date.today().weekday()) % 7 or 7)
    for client_id, day_offset, hour in (
        ("alice", 0, 10), ("bob", 0, 14), ("carol", 1, 11),
    ):
        start = datetime.combine(monday + timedelta(days=day_offset), time(hour))
        target.store.book_appointment(
            client_id, "session", start, start + timedelta(minutes=60)
        )
    target._note("seeded a starting calendar")


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


app.mount("/static", StaticFiles(directory=STATIC), name="static")

if not world.clients:
    seed_world(world)
