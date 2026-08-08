"""HTTP layer: a thin translation of JSON into `World` method calls.

Deliberately holds no logic of its own — anything interesting belongs in
`state.World`, so the mock's behaviour can be tested without a browser.
"""
from __future__ import annotations

import hashlib
import os
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import List, Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from calendar_store import Party

from . import persistence
from .state import PROVIDER, World

STATIC = Path(__file__).parent / "static"
def default_snapshot() -> Path:
    """Beside the package, not beside the shell.

    A relative default meant the session landed somewhere different depending
    on where you launched from, so a restart could silently start empty.
    """
    return Path(__file__).resolve().parents[2] / "mock_ui_session.json"


SNAPSHOT = Path(os.environ.get("MOCK_UI_SNAPSHOT", default_snapshot()))

app = FastAPI(title="Scheduling mock UI")
world = persistence.load_or_new(SNAPSHOT)


# -- payloads -------------------------------------------------------


class ClientIn(BaseModel):
    id: str
    name: str = ""
    mirror_provider: bool = True


class WeeklyIn(BaseModel):
    client_id: str
    ranges: List[dict] = []


class RequestIn(BaseModel):
    client_id: str
    service_id: str
    preferred_start: str        # a wish; availability decides what is possible


class ExceptionIn(BaseModel):
    client_id: str
    date: str
    from_time: str
    to_time: str
    available: bool = True
    clear: bool = False


class ServiceIn(BaseModel):
    id: str
    name: str
    duration_minutes: int
    price_minor_units: int
    client_bookable: bool = True


class MoveIn(BaseModel):
    start: str
    end: str


class ApprovalIn(BaseModel):
    # "accept" | "decline" | "refuse" — see World.respond_to_approval. Declining
    # and refusing are different answers, and only the client knows which.
    answer: str


class AwayIn(BaseModel):
    """The provider cannot work a stretch of time."""
    start: str
    end: str
    # "reschedule" keeps each client's booking until there is somewhere to move
    # it; "cancel" ends them outright.
    action: str = "reschedule"


class ManualPlacementIn(BaseModel):
    request_id: int
    start: str
    # Locked by default: the point of placing by hand is to decide something
    # before optimising, and a decision should not come back as a candidate.
    lock: bool = True


class SettleIn(BaseModel):
    # "agreed" | "agreed_only" | "reoptimise" — see World.settle_plan
    how: str


class RescheduleRequestIn(BaseModel):
    preferred_start: str
    # False keeps the old slot until a replacement is confirmed; True gives it
    # up now. The client's risk to take, so the client is asked.
    release_slot: bool = False


class CancelIn(BaseModel):
    by_provider: bool = False


class AttendanceIn(BaseModel):
    attended: bool


class SolveIn(BaseModel):
    """Try the optimiser at settings other than the saved ones. Omitted
    fields fall back to the provider's defaults."""
    alpha: Optional[float] = None
    max_displacements: Optional[int] = None
    allow_chains: Optional[bool] = None
    # Which requests this run is about. None falls back to the standing rule
    # (SchedulingPolicy.scope_to_horizon).
    request_ids: Optional[List[int]] = None


class ApproveIn(BaseModel):
    items: Optional[List[str]] = None     # None = the whole plan


class SettingsIn(BaseModel):
    alpha: Optional[float] = None
    max_displacements: Optional[int] = None
    auto_run: Optional[bool] = None
    urgency_hours: Optional[int] = None
    retry_after_minutes: Optional[int] = None
    horizon_days: Optional[int] = None
    scope_to_horizon: Optional[bool] = None


# -- routes ---------------------------------------------------------


@app.get("/api/state")
def get_state():
    # Every poll is also a chance for the provider's schedule policy to fire.
    # Cheap, deterministic, and avoids a background thread for something a
    # person is watching anyway.
    world.tick()
    return world.snapshot()


@app.post("/api/clients")
def add_client(payload: ClientIn):
    try:
        client = world.add_client(payload.id, payload.name, payload.mirror_provider)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None
    return {"client": vars(client)}


@app.post("/api/availability")
def set_availability(payload: WeeklyIn):
    world.set_weekly_availability(payload.client_id, payload.ranges)
    return {"ok": True}


@app.post("/api/exceptions")
def set_exception(payload: ExceptionIn):
    """One date only — the week that is not normal."""
    try:
        on_date = date.fromisoformat(payload.date)
        start = time.fromisoformat(payload.from_time)
        end = time.fromisoformat(payload.to_time)
        if payload.clear:
            world.clear_exception(payload.client_id, on_date, start, end,
                                  payload.available)
        else:
            world.set_exception(payload.client_id, on_date, start, end,
                                payload.available)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None
    return {"ok": True}


@app.post("/api/requests")
def submit_request(payload: RequestIn):
    try:
        request = world.submit_request(
            payload.client_id, payload.service_id, payload.preferred_start
        )
    except KeyError as exc:
        raise HTTPException(400, str(exc.args[0])) from None
    # Deliberately does NOT run the scheduler. A request arriving is not a
    # reason to re-plan the week — see policy.py.
    return {"request_id": request.id}


@app.post("/api/services")
def add_service(payload: ServiceIn):
    try:
        service = world.catalogue.add_service(
            payload.id, payload.name, payload.duration_minutes,
            payload.price_minor_units, client_bookable=payload.client_bookable,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None
    world._note(f"added service {service.name}")
    return {"ok": True}


@app.post("/api/services/{service_id}/active")
def set_service_active(service_id: str, active: bool = True):
    try:
        service = (world.catalogue.reactivate_service(service_id) if active
                   else world.catalogue.deactivate_service(service_id))
    except KeyError:
        raise HTTPException(404, "no such service") from None
    world._note(
        f"{'re-listed' if active else 'discontinued'} {service.name}"
    )
    return {"ok": True}


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
    return {"ok": True}


@app.post("/api/appointments/{appointment_id}/reschedule-request")
def request_reschedule(appointment_id: int, payload: RescheduleRequestIn):
    """A client asking to be moved without naming where — see
    World.request_reschedule. Distinct from /move, which is a client who has
    already found a slot for themselves."""
    try:
        request = world.request_reschedule(
            appointment_id, payload.preferred_start, payload.release_slot)
    except KeyError:
        raise HTTPException(404, "no such appointment") from None
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from None
    return {"request_id": request.id}


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
    try:
        world.respond_to_approval(approval_id, payload.answer)
    except ValueError as bad:
        raise HTTPException(400, str(bad)) from None
    return {"ok": True}


@app.post("/api/solve")
def solve(payload: SolveIn = SolveIn()):
    """Run the scheduler now, on the provider's say-so.

    Several drafts may coexist under different settings — nothing is reserved
    until one is approved — so this is how the provider compares rather than
    being told.
    """
    try:
        return world.propose(
            reason="provider", alpha=payload.alpha,
            request_ids=payload.request_ids,
            max_displacements=payload.max_displacements,
            allow_chains=payload.allow_chains,
        )
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from None


@app.post("/api/provider/away")
def provider_away(payload: AwayIn):
    """See World.provider_away — marks the time off *and* deals with what is
    already booked in it, which nothing else notices."""
    try:
        return world.provider_away(
            datetime.fromisoformat(payload.start),
            datetime.fromisoformat(payload.end),
            payload.action,
        )
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from None


@app.post("/api/appointments/manual")
def place_manually(payload: ManualPlacementIn):
    """The provider booking a waiting request themselves, before optimising."""
    if payload.request_id not in world.requests:
        raise HTTPException(404, "no such request")
    try:
        booked = world.place_manually(
            payload.request_id, datetime.fromisoformat(payload.start), payload.lock)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from None
    return {"appointment_id": booked.id}


@app.post("/api/plans/{plan_id}/approve")
def approve_plan(plan_id: int, payload: ApproveIn = ApproveIn()):
    if plan_id not in world.plans:
        raise HTTPException(404, "no such plan")
    try:
        return world.provider_approve(plan_id, payload.items)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from None


@app.post("/api/plans/{plan_id}/settle")
def settle_plan(plan_id: int, payload: SettleIn):
    """What the provider does with the answers: see World.settle_plan."""
    if plan_id not in world.plans:
        raise HTTPException(404, "no such plan")
    try:
        return world.settle_plan(plan_id, payload.how)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from None


@app.post("/api/plans/{plan_id}/discard")
def discard_plan(plan_id: int):
    if plan_id not in world.plans:
        raise HTTPException(404, "no such plan")
    world.discard_plan(plan_id)
    return {"ok": True}


@app.post("/api/plans/{plan_id}/reject")
def reject_plan(plan_id: int):
    if plan_id not in world.plans:
        raise HTTPException(404, "no such plan")
    world.provider_reject(plan_id)
    # Rejection is itself a trigger: try again without the arrangement the
    # provider did not want.
    return world.propose(reason="provider")


@app.post("/api/settings")
def settings(payload: SettingsIn):
    if payload.alpha is not None:
        world.alpha = max(0.0, min(1.0, payload.alpha))
    if payload.max_displacements is not None:
        world.max_displacements = max(0, payload.max_displacements)
    if payload.auto_run is not None:
        world.policy.auto_run = payload.auto_run
    if payload.urgency_hours is not None:
        world.policy.urgency_hours = max(0, payload.urgency_hours)
    if payload.retry_after_minutes is not None:
        world.policy.retry_after_minutes = max(1, payload.retry_after_minutes)
    if payload.horizon_days is not None:
        world.policy.horizon_days = max(1, payload.horizon_days)
    if payload.scope_to_horizon is not None:
        world.policy.scope_to_horizon = payload.scope_to_horizon
    return {"ok": True}


@app.post("/api/snapshot/save")
def snapshot_save():
    """Explicit save. Redundant with the autosave above, kept so there is a
    way to confirm where the session actually lives."""
    persistence.save(world, SNAPSHOT)
    return {"saved": str(SNAPSHOT)}


@app.post("/api/reset")
def reset(seed: bool = True):
    """Start again — including on disk.

    Leaving the saved file behind would mean a reset that undoes itself at the
    next restart, which is the opposite of what the button says.
    """
    global world
    world = World()
    if seed:
        seed_world(world)
    SNAPSHOT.unlink(missing_ok=True)
    return {"ok": True}


# -- a world worth opening on the first run --------------------------


def seed_world(target: World) -> None:
    """Enough of a calendar that the first page load shows something.

    Weekdays 09:00-17:00 for the provider, three clients with different
    availability, and one booking each so history starts accumulating rather
    than requiring setup before anything can be tried.
    """
    target.catalogue.add_service("session-60", "Standard session", 60, 8000)
    target.catalogue.add_service("session-90", "Extended session", 90, 11000)
    target.catalogue.add_service("break", "Break", 60, 0, client_bookable=False)

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
            client_id, "session-60", start, start + timedelta(minutes=60)
        )
    target._note("seeded a starting calendar")


def _asset_version() -> str:
    """A hash of the scripts, so their URLs change whenever they do."""
    digest = hashlib.sha256()
    for name in sorted(STATIC.glob("*.js")):
        digest.update(name.read_bytes())
    digest.update((STATIC / "style.css").read_bytes())
    return digest.hexdigest()[:12]


@app.middleware("http")
async def autosave(request, call_next):
    """Keep the session on disk without anyone having to remember to.

    Losing an afternoon of hand-built history to a restart is a poor way to
    learn that saving was manual.
    """
    response = await call_next(request)
    if request.method == "POST" and response.status_code < 400 \
            and not request.url.path.endswith("/reset"):
        try:
            persistence.save(world, SNAPSHOT)
        except Exception as exc:                     # never break a request over it
            world._note(f"could not save the session: {exc}")
    return response


@app.middleware("http")
async def no_stale_assets(request, call_next):
    """Serve the scripts fresh, always.

    A cached copy of one script beside a new copy of another is a silent,
    baffling failure: the page loads, nothing works, and the cause is invisible.
    This is a development tool, so correctness beats a cache hit.
    """
    response = await call_next(request)
    if request.url.path.startswith("/static") or request.url.path == "/":
        response.headers["Cache-Control"] = "no-store, must-revalidate"
    return response


@app.get("/")
def index():
    # Version the script URLs too, so even a cache that ignores the header
    # cannot pair a stale script with a fresh one.
    html = (STATIC / "index.html").read_text()
    version = _asset_version()
    for asset in ("calendar.js", "app.js", "style.css"):
        html = html.replace(f"/static/{asset}", f"/static/{asset}?v={version}")
    return HTMLResponse(html)


app.mount("/static", StaticFiles(directory=STATIC), name="static")

if not world.clients:
    seed_world(world)
