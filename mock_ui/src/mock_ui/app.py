"""HTTP layer: a thin translation of JSON into `World` method calls.

Deliberately holds no logic of its own — anything interesting belongs in
`state.World`, so the mock's behaviour can be tested without a browser.
"""
from __future__ import annotations

import hashlib
import mimetypes
import os
from datetime import date, datetime, time
from pathlib import Path
from typing import List, Optional

import html as html_lib

from fastapi import FastAPI, Form, HTTPException, Query, Request as HttpRequest
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from calendar_store import Party
from scheduling_contracts import Principal, Role, Session

from . import assistant as assistant_mod, billing, calendar_sync, control, persistence, scenarios
from .state import World

STATIC = Path(__file__).parent / "static"
# Not in every platform's table, and a manifest served as octet-stream is
# ignored, so "Add to Home Screen" would open the desktop page.
mimetypes.add_type("application/manifest+json", ".webmanifest")
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


class BillingIn(BaseModel):
    default_mode: Optional[str] = None
    late_cancel_window_hours: Optional[int] = None
    late_cancel_fee: Optional[str] = None
    no_show_fee: Optional[str] = None
    gst_registered: Optional[bool] = None
    gst_number: Optional[str] = None
    gst_rate_percent: Optional[float] = None


class BillingModeIn(BaseModel):
    mode: Optional[str] = None          # None = the practice default


class CheckoutIn(BaseModel):
    # A path on this app to come back to, e.g. "/phone?as=dan". Only a path:
    # an absolute address here would let a link send a paying client anywhere.
    return_to: str = "/phone"


class RecordIn(BaseModel):
    method: str = "cash"


class LimitsIn(BaseModel):
    # None means no limit.
    per_day: Optional[int] = 1
    per_week: Optional[int] = None


class SeriesIn(BaseModel):
    client_id: str
    service_id: str
    first_start: str            # the first session; the rest follow at the same time
    every_weeks: int = 1
    until: Optional[str] = None  # last date a session may fall on


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
    # "flag" (the default) marks the time off and leaves the bookings in it
    # alone for the provider to decide about; "reschedule" and "cancel" make
    # that decision in the same breath.
    action: str = "flag"


class OrphansIn(BaseModel):
    # Which stranded bookings to act on. None means all of them.
    appointment_ids: Optional[List[int]] = None


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
    rehouse_orphans_on_run: Optional[bool] = None
    practice_name: Optional[str] = None
    chat_enabled: Optional[bool] = None
    assistant_enabled: Optional[bool] = None
    chat_reply_expectation: Optional[str] = None
    chat_emergency_note: Optional[str] = None


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


# -- the gate (control.py) ---------------------------------------------------

_gate = None


def gate() -> control.Gate:
    """The gate over the current world; a reset replaces both."""
    global _gate
    if _gate is None or _gate.world is not world:
        _gate = control.Gate(world)
    return _gate


def session_for(person: str) -> Session:
    """Who is asking. **A stand-in for signing in**: "as" is a claim, exactly
    like "view as" on the screens, until there is a login to take it from.
    Everything downstream already treats the session as the only source of
    identity, so replacing this one function is what sign-in will be."""
    if person == "provider":
        return Session(Principal(Role.PROVIDER))
    if person in world.clients:
        return Session(Principal(Role.CLIENT, person))
    raise HTTPException(404, "no such person")


def _gate_error(exc: control.GateError):
    raise HTTPException(exc.status, str(exc)) from None


class CommandIn(BaseModel):
    args: dict = {}


@app.post("/api/commands/{name}")
def run_command(name: str, payload: CommandIn, as_: str = Query("provider", alias="as")):
    """Any command, through the gate, as the person themselves. The screens'
    way onto the gate — the clinician chat is the first feature to use it,
    so its permissions are the contract's from the start."""
    session = session_for(as_)
    try:
        outcome = gate().execute(session, control.build_command(name, payload.args, session))
    except control.GateError as exc:
        _gate_error(exc)
    return {"status": outcome.status, "result": outcome.result,
            "token": outcome.token, "summary": outcome.summary}


# -- the assistant (assistant.py) -----------------------------------------------

_assistant = None


def assistant() -> assistant_mod.Assistant:
    global _assistant
    if _assistant is None or _assistant.world is not world:
        _assistant = assistant_mod.Assistant(world, gate())
    elif _assistant.gate is not gate():
        _assistant.gate = gate()
    return _assistant


class AssistantIn(BaseModel):
    conversation_id: Optional[str] = None
    text: str


class OptIn(BaseModel):
    on: bool


@app.get("/api/assistant/status")
def assistant_status(as_: str = Query("provider", alias="as")):
    return assistant().status(session_for(as_).principal)


@app.post("/api/assistant/opt-in")
def assistant_opt_in(payload: OptIn, as_: str = Query("provider", alias="as")):
    """A client's own choice, after reading what is sent where."""
    principal = session_for(as_).principal
    if principal.role is not Role.CLIENT:
        raise HTTPException(400, "only a client opts in; the practice switches it on")
    (world.assistant.opted_in.add if payload.on else world.assistant.opted_in.discard)(principal.client_id)
    world._note(f"{principal.client_id} turned the assistant {'on' if payload.on else 'off'}")
    return {"opted_in": payload.on}


@app.post("/api/assistant/send")
def assistant_send(payload: AssistantIn, as_: str = Query("provider", alias="as")):
    try:
        return assistant().send(session_for(as_).principal, payload.conversation_id, payload.text)
    except assistant_mod.Unavailable as exc:
        raise HTTPException(503, str(exc)) from None
    except assistant_mod.AssistantError as exc:
        raise HTTPException(502, str(exc)) from None
    except control.GateError as exc:
        _gate_error(exc)


@app.post("/api/assistant/{conversation_id}/handoff")
def assistant_handoff(conversation_id: str, as_: str = Query("provider", alias="as")):
    try:
        return assistant().handoff(session_for(as_).principal, conversation_id)
    except control.GateError as exc:
        _gate_error(exc)


@app.get("/api/drafts")
def list_drafts(as_: str = Query("provider", alias="as")):
    """Changes an assistant has prepared for this person to confirm."""
    session = session_for(as_)
    return {"drafts": [{"token": d.token, "summary": d.summary,
                        "created": d.created.isoformat(timespec="seconds")}
                       for d in gate().pending(session.principal)]}


@app.post("/api/drafts/{token}/confirm")
def confirm_draft(token: str, as_: str = Query("provider", alias="as")):
    try:
        outcome = gate().confirm(session_for(as_), token)
    except control.GateError as exc:
        _gate_error(exc)
    return {"status": outcome.status}


@app.post("/api/drafts/{token}/discard")
def discard_draft(token: str, as_: str = Query("provider", alias="as")):
    try:
        gate().discard(session_for(as_), token)
    except control.GateError as exc:
        _gate_error(exc)
    return {"ok": True}


# -- money --------------------------------------------------------------


def _charge_or_404(charge_id: int):
    if charge_id not in world.charges:
        raise HTTPException(404, "no such charge")
    return world.charges[charge_id]


@app.post("/api/billing")
def configure_billing(payload: BillingIn):
    try:
        settings = world.configure_billing(**payload.model_dump(exclude_none=True))
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None
    return billing.settings_dict(settings)


@app.post("/api/clients/{client_id}/billing-mode")
def set_billing_mode(client_id: str, payload: BillingModeIn):
    if client_id not in world.clients:
        raise HTTPException(404, "no such client")
    try:
        world.set_billing_mode(client_id, payload.mode)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None
    return {"ok": True}


@app.post("/api/charges/{charge_id}/confirm")
def confirm_fee(charge_id: int):
    _charge_or_404(charge_id)
    try:
        world.confirm_fee(charge_id)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from None
    return {"ok": True}


@app.post("/api/charges/{charge_id}/waive")
def waive_charge(charge_id: int):
    _charge_or_404(charge_id)
    try:
        world.waive_charge(charge_id)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from None
    return {"ok": True}


@app.post("/api/charges/{charge_id}/checkout")
def start_checkout(charge_id: int, payload: CheckoutIn, request: HttpRequest):
    _charge_or_404(charge_id)
    if not payload.return_to.startswith("/") or payload.return_to.startswith("//"):
        raise HTTPException(400, "return_to must be a path on this app")
    back = str(request.base_url).rstrip("/") + payload.return_to
    try:
        payment = world.start_checkout(charge_id, back)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from None
    return {"payment_id": payment.id, "url": payment.checkout_url}


@app.post("/api/payments/{payment_id}/check")
def check_payment(payment_id: int):
    if payment_id not in world.payments:
        raise HTTPException(404, "no such payment")
    payment = world.check_payment(payment_id)
    return {"status": payment.status, "method": payment.method,
            "charge_status": world.charges[payment.charge_id].status}


@app.post("/api/charges/{charge_id}/record")
def record_payment(charge_id: int, payload: RecordIn):
    _charge_or_404(charge_id)
    try:
        world.record_payment(charge_id, payload.method)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from None
    return {"ok": True}


_SIMULATED_PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Pay — simulated</title>
<style>
body{{font:16px/1.4 -apple-system,system-ui,sans-serif;margin:0;background:#f6f7f9;color:#16181d}}
main{{max-width:420px;margin:0 auto;padding:24px 16px}}
.banner{{background:#fff4d6;color:#7a5200;border-radius:12px;padding:10px 14px;font-size:14px}}
.card{{background:#fff;border:1px solid #e3e6eb;border-radius:14px;padding:16px;margin:16px 0}}
.amount{{font-size:34px;font-weight:700;margin:4px 0}}
label{{display:flex;gap:10px;align-items:center;padding:12px;border:1.5px solid #e3e6eb;border-radius:12px;margin:8px 0}}
button{{width:100%;min-height:50px;border-radius:12px;border:0;font:inherit;font-weight:600;margin-top:8px}}
.pay{{background:#2f5bea;color:#fff}} .fail{{background:#fff;border:1px solid #e3e6eb;color:#c0322b}}
@media (prefers-color-scheme:dark){{body{{background:#14161a;color:#eceef2}}.card,label,.fail{{background:#1d2026;border-color:#2f343c}}}}
</style></head><body><main>
<div class="banner">Simulated payment page. No real money moves — this stands in for
Stripe until a Stripe test key is set up.</div>
<div class="card"><div>{practice}</div><div class="amount">{amount}</div><div>{description}</div></div>
{body}
</main></body></html>"""


@app.get("/pay/simulated/{reference}", response_class=HTMLResponse)
def simulated_checkout(reference: str):
    payment = world.payment_by_reference(reference)
    if payment is None or payment.processor != "simulated":
        raise HTTPException(404, "no such payment")
    charge = world.charges[payment.charge_id]
    if payment.status != "pending":
        body = f'<p>This payment is already {payment.status}.</p><a href="{html_lib.escape(payment.return_url)}">Back</a>'
    else:
        options = "".join(
            f'<label><input type="radio" name="method" value="{key}" {"checked" if key == "paynow" else ""}>'
            f"{html_lib.escape(label)}</label>"
            for key, label in billing.PAY_METHODS.items() if key not in billing.RECORDED_METHODS)
        body = (f'<form method="post">{options}'
                f'<button class="pay" name="outcome" value="succeeded">Pay</button>'
                f'<button class="fail" name="outcome" value="failed">Decline the payment</button></form>')
    return _SIMULATED_PAGE.format(
        practice=html_lib.escape(world.practice_name),
        amount=billing.money(charge.amount, world.billing.currency),
        description=html_lib.escape(charge.description), body=body)


@app.post("/pay/simulated/{reference}")
def simulated_outcome(reference: str, outcome: str = Form(...), method: str = Form("paynow")):
    payment = world.payment_by_reference(reference)
    if payment is None or payment.processor != "simulated":
        raise HTTPException(404, "no such payment")
    if outcome not in ("succeeded", "failed") or method not in billing.PAY_METHODS:
        raise HTTPException(400, "unknown outcome or method")
    if payment.status == "pending":
        world.processor.complete(reference, outcome, method)
    # Back to the app, which then *asks* what happened rather than trusting
    # the redirect — exactly as it will with Stripe.
    return RedirectResponse(payment.return_url, status_code=303)


@app.get("/calendar/{token}.ics")
def calendar_feed(token: str):
    """A person's appointments as a subscribable calendar — see
    calendar_sync.py. The token in the address is the only credential: a
    calendar app cannot sign in, so an unknown one is simply not found."""
    person = world.person_for_feed(token)
    if person is None:
        raise HTTPException(404, "no such calendar")
    name = (f"{world.practice_name} — diary" if person == "provider"
            else f"Appointments — {world.practice_name}")
    body = calendar_sync.to_ics(calendar_sync.events_for(world, person), name)
    return Response(body, media_type="text/calendar; charset=utf-8",
                    headers={"Cache-Control": "no-cache"})


@app.post("/api/calendar/{person}")
def calendar_link(person: str):
    """The secret part of a person's calendar address, made if need be. A
    POST so the new secret is saved with the session before anyone uses it."""
    try:
        return {"token": world.feed_token(person)}
    except KeyError:
        raise HTTPException(404, "no such person") from None


@app.post("/api/calendar/{person}/reset")
def reset_calendar_link(person: str):
    try:
        return {"token": world.reset_feed_token(person)}
    except KeyError:
        raise HTTPException(404, "no such person") from None


@app.post("/api/clients/{client_id}/limits")
def set_client_limits(client_id: str, payload: LimitsIn):
    """How often this client may be booked — see World.set_client_limits."""
    if client_id not in world.clients:
        raise HTTPException(404, "no such client")
    try:
        client = world.set_client_limits(client_id, payload.per_day, payload.per_week)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None
    return {"per_day": client.per_day, "per_week": client.per_week}


@app.post("/api/series")
def start_series(payload: SeriesIn):
    """A booking that repeats — see World.start_series. Each session is raised
    as an ordinary request when it comes into the planning window; like any
    request, none of them runs the scheduler by arriving."""
    try:
        series = world.start_series(payload.client_id, payload.service_id,
                                    payload.first_start, payload.every_weeks,
                                    payload.until)
    except KeyError as exc:
        raise HTTPException(400, str(exc.args[0])) from None
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None
    raised = sum(1 for r in world.requests.values() if r.series_id == series.id)
    return {"series_id": series.id, "raised": raised}


@app.post("/api/series/{series_id}/stop")
def stop_series(series_id: int):
    if series_id not in world.series:
        raise HTTPException(404, "no such series")
    return {"withdrawn": world.stop_series(series_id)}


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


@app.post("/api/orphans/rehouse")
def rehouse_orphans(payload: OrphansIn = OrphansIn()):
    """Queue stranded bookings to be moved. Explicit: changing availability
    never does this by itself."""
    return {"rehoused": world.rehouse_orphans(payload.appointment_ids)}


@app.post("/api/orphans/cancel")
def cancel_orphans(payload: OrphansIn = OrphansIn()):
    return {"cancelled": world.cancel_orphans(payload.appointment_ids)}


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
    if payload.rehouse_orphans_on_run is not None:
        world.policy.rehouse_orphans_on_run = payload.rehouse_orphans_on_run
    if payload.practice_name is not None:
        name = payload.practice_name.strip()
        if not name:
            raise HTTPException(400, "the practice needs a name")
        world.practice_name = name[:80]
    if payload.assistant_enabled is not None:
        world.assistant.enabled = payload.assistant_enabled
        world._note(f"the practice turned the assistant {'on' if payload.assistant_enabled else 'off'}")
    if payload.chat_enabled is not None:
        world.chat.enabled = payload.chat_enabled
    if payload.chat_reply_expectation is not None:
        world.chat.reply_expectation = payload.chat_reply_expectation.strip()[:200]
    if payload.chat_emergency_note is not None:
        note = payload.chat_emergency_note.strip()
        if not note:
            raise HTTPException(400, "clients must always be told where to get urgent help")
        world.chat.emergency_note = note[:400]
    return {"ok": True}


@app.post("/api/snapshot/save")
def snapshot_save():
    """Explicit save. Redundant with the autosave above, kept so there is a
    way to confirm where the session actually lives."""
    persistence.save(world, SNAPSHOT)
    return {"saved": str(SNAPSHOT)}


@app.post("/api/reset")
def reset(scenario: str = "simple"):
    """Start again on a chosen example calendar — including on disk.

    Leaving the saved file behind would mean a reset that undoes itself at the
    next restart, which is the opposite of what the button says.

    `scenario` is a key from `scenarios.SCENARIOS`, or "empty" for nothing at
    all. Both examples are built so the first run has something to negotiate
    about; see that module for why that has to be deliberate.
    """
    global world
    world = World()
    if scenario != "empty":
        try:
            scenarios.load(world, scenario)
        except ValueError as exc:
            raise HTTPException(404, str(exc)) from None
    SNAPSHOT.unlink(missing_ok=True)
    return {"ok": True, "scenario": scenario}


@app.get("/api/scenarios")
def list_scenarios():
    return {"scenarios": sorted(scenarios.SCENARIOS) + ["empty"]}


# -- a world worth opening on the first run --------------------------


def _asset_version() -> str:
    """A hash of the scripts, so their URLs change whenever they do."""
    digest = hashlib.sha256()
    for name in sorted([*STATIC.glob("*.js"), *STATIC.glob("*.css")]):
        digest.update(name.read_bytes())
    return digest.hexdigest()[:12]


def _page(name: str, assets) -> HTMLResponse:
    # Version the asset URLs too, so even a cache that ignores the header
    # cannot pair a stale script with a fresh one.
    html = (STATIC / name).read_text()
    version = _asset_version()
    for asset in assets:
        html = html.replace(f"/static/{asset}", f"/static/{asset}?v={version}")
    return HTMLResponse(html)


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
    if request.url.path.startswith("/static") or request.url.path in ("/", "/phone"):
        response.headers["Cache-Control"] = "no-store, must-revalidate"
    return response


@app.get("/")
def index():
    return _page("index.html", ("calendar.js", "app.js", "style.css"))


@app.get("/phone")
def phone():
    """The same world, laid out for a phone: tabs, taps and sheets instead of
    a week grid driven by hover and drag. Same API, so either page can be
    open on the same session at once."""
    return _page("phone.html", ("phone.js", "phone.css"))


app.mount("/static", StaticFiles(directory=STATIC), name="static")

if not world.clients:
    scenarios.load(world, "simple")
