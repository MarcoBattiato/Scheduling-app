"""The shared world: one store, one engine, one set of pending negotiations.

Everything the browser can do goes through `World`. It is deliberately the only
place that knows both packages, so the HTTP layer above stays a thin translation
of JSON to method calls.
"""
from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Dict, List, Sequence

from calendar_store import Appointment, AvailabilityStore, Origin, TimeSegment
from scheduling_engine import (
    BookingRequest,
    CostConfig,
    MovableAppointment,
    RescheduleBounds,
    TimeRange,
    free_time,
    reschedule_windows,
    solve_placements,
)

PROVIDER = "provider-self"
_EPOCH = date(2000, 1, 1)


@dataclass
class Client:
    id: str
    name: str


@dataclass
class Request:
    """A client asking for a booking. Lives until placed or withdrawn."""
    id: int
    client_id: str
    duration_minutes: int
    windows: List[TimeRange]
    status: str = "pending"          # pending | placed | withdrawn
    note: str = ""


@dataclass
class Approval:
    """A rescheduling the scheduler wants, waiting on the client's answer.

    Owned by this mock, not by the engine — see SPEC.md §4.
    """
    id: int
    plan_id: int
    client_id: str
    appointment_id: int
    was: TimeRange
    now: TimeRange
    status: str = "pending"          # pending | accepted | declined


@dataclass
class Plan:
    """A solver result held back until every displacement it needs is agreed."""
    id: int
    placements: List[dict] = field(default_factory=list)
    status: str = "pending"          # pending | applied | abandoned
    created_at: datetime = field(default_factory=datetime.now)


class World:
    def __init__(self) -> None:
        self.store = AvailabilityStore()
        self.clients: Dict[str, Client] = {}
        self.requests: Dict[int, Request] = {}
        self.approvals: Dict[int, Approval] = {}
        self.plans: Dict[int, Plan] = {}
        self.log: List[str] = []
        self.alpha = 0.5
        self.max_displacements = 1
        self.bounds = RescheduleBounds(max_days_earlier=2, max_days_later=4)
        self.service_durations = (60, 90)
        # Appointments a client refused to move this session. Cruder than the
        # engine's per-date blocking (SPEC.md §7 of the mock).
        self.immovable: set = set()
        self._ids = itertools.count(1)

    # -- people ------------------------------------------------------

    def add_client(self, client_id: str, name: str = "") -> Client:
        client = Client(id=client_id, name=name or client_id.title())
        self.clients[client_id] = client
        self._note(f"client {client_id} joined")
        return client

    # -- availability ------------------------------------------------

    def set_weekly_availability(
        self, client_id: str, ranges: Sequence[dict]
    ) -> None:
        """Replace a client's whole weekly pattern.

        The grid in the browser is the source of truth, so this clears every
        weekday first rather than trying to diff — simpler, and the store
        normalises the result anyway.
        """
        for weekday in range(7):
            self.store.remove_recurring_availability(
                client_id, weekday, time.min, time.max, effective_from=_EPOCH,
            )
        for entry in ranges:
            self.store.add_recurring_availability(
                client_id,
                int(entry["weekday"]),
                _parse_time(entry["from"]),
                _parse_time(entry["to"]),
                effective_from=_EPOCH,
            )
        self._note(f"{client_id} updated availability")

    def availability_segments(
        self, client_id: str, start: date, end: date
    ) -> List[TimeSegment]:
        return self.store.get_availability_segments(client_id, start, end)

    # -- bookings ----------------------------------------------------

    def submit_request(
        self, client_id: str, duration_minutes: int, windows: Sequence[dict]
    ) -> Request:
        request = Request(
            id=next(self._ids),
            client_id=client_id,
            duration_minutes=int(duration_minutes),
            windows=[
                TimeRange(_parse_dt(w["from"]), _parse_dt(w["to"])) for w in windows
            ],
        )
        self.requests[request.id] = request
        self._note(f"{client_id} asked for {duration_minutes}m")
        return request

    def withdraw_request(self, request_id: int) -> None:
        request = self.requests[request_id]
        request.status = "withdrawn"
        self._note(f"{request.client_id} withdrew request {request_id}")

    def cancel_appointment(self, appointment_id: int) -> Appointment:
        appointment = self.store.cancel_appointment(appointment_id)
        self._note(
            f"{appointment.client_id} cancelled "
            f"{appointment.range.start:%a %d %b %H:%M}"
        )
        return appointment

    def move_appointment(
        self, appointment_id: int, start: datetime, end: datetime
    ) -> Appointment:
        """A client moving their own booking — their choice, so Origin.CLIENT."""
        moved = self.store.reschedule_appointment(
            appointment_id, start, end, origin=Origin.CLIENT
        )
        self._note(f"{moved.client_id} moved their own booking to {start:%a %d %b %H:%M}")
        return moved

    # -- scheduling --------------------------------------------------

    def solve(self, horizon_days: int = 21) -> dict:
        """Run the engine over the pending queue.

        A plan needing nobody moved is applied at once. One needing
        displacements is held until every affected client agrees (SPEC.md §4).
        """
        pending = [r for r in self.requests.values() if r.status == "pending"]
        if not pending:
            return {"placed": 0, "unplaced": 0, "awaiting_approval": 0}

        window_start = date.today()
        window_end = window_start + timedelta(days=horizon_days)
        config = CostConfig(
            alpha=self.alpha, service_durations=self.service_durations
        )

        availability = self.availability_segments(PROVIDER, window_start, window_end)
        live = self._live_appointments(window_start, window_end)
        provider_free = free_time(availability, live)

        movable = []
        for appointment in live:
            if appointment.locked or appointment.id in self.immovable:
                continue
            current = TimeRange(appointment.range.start, appointment.range.end)
            allowed = reschedule_windows(
                current,
                self.availability_segments(
                    appointment.client_id, window_start, window_end
                ),
                self.bounds,
            )
            if allowed:
                movable.append(MovableAppointment(
                    id=str(appointment.id),
                    client_id=appointment.client_id,
                    range=current,
                    allowed=allowed,
                ))

        requests = [
            BookingRequest(str(r.id), r.client_id, r.duration_minutes, r.windows)
            for r in pending
        ]
        result = solve_placements(
            requests, provider_free, config,
            movable=movable, max_displacements=self.max_displacements,
        )

        plan = Plan(id=next(self._ids), placements=[
            {"request_id": int(p.request_id), "client_id": p.client_id,
             "start": p.range.start, "end": p.range.end}
            for p in result.placements
        ])

        if not result.displacements:
            self._apply(plan)
            return {"placed": len(plan.placements),
                    "unplaced": len(result.unplaced), "awaiting_approval": 0}

        self.plans[plan.id] = plan
        for displacement in result.displacements:
            approval = Approval(
                id=next(self._ids),
                plan_id=plan.id,
                client_id=displacement.client_id,
                appointment_id=int(displacement.appointment_id),
                was=displacement.was,
                now=displacement.now,
            )
            self.approvals[approval.id] = approval
            self._note(
                f"asked {displacement.client_id} to move "
                f"{displacement.was.start:%a %H:%M} → {displacement.now.start:%a %H:%M}"
            )
        return {"placed": 0, "unplaced": len(result.unplaced),
                "awaiting_approval": len(result.displacements)}

    def respond_to_approval(self, approval_id: int, accept: bool) -> None:
        approval = self.approvals[approval_id]
        approval.status = "accepted" if accept else "declined"
        plan = self.plans.get(approval.plan_id)
        self._note(
            f"{approval.client_id} {'accepted' if accept else 'declined'} the move"
        )

        if not accept:
            # Crude: the appointment is off the table for the rest of the
            # session, and the plan that depended on it is dropped.
            self.immovable.add(approval.appointment_id)
            if plan and plan.status == "pending":
                plan.status = "abandoned"
                for other in self.approvals.values():
                    if other.plan_id == plan.id and other.status == "pending":
                        other.status = "declined"
            self.solve()
            return

        siblings = [
            a for a in self.approvals.values()
            if a.plan_id == approval.plan_id
        ]
        if plan and plan.status == "pending" and all(
            a.status == "accepted" for a in siblings
        ):
            self._apply(plan, accepted=siblings)

    def _apply(self, plan: Plan, accepted: Sequence[Approval] = ()) -> None:
        for approval in accepted:
            # Accepted, but not chosen: the client agreed to move, they did not
            # pick this slot. Recording it as CLIENT would teach the anchoring
            # model that being pushed around is a preference (SPEC.md §5).
            self.store.reschedule_appointment(
                approval.appointment_id,
                approval.now.start,
                approval.now.end,
                origin=Origin.DISPLACED,
            )
        for placement in plan.placements:
            self.store.book_appointment(
                placement["client_id"], "session",
                placement["start"], placement["end"],
            )
            request = self.requests.get(placement["request_id"])
            if request:
                request.status = "placed"
        plan.status = "applied"
        self._note(f"applied plan {plan.id}: {len(plan.placements)} booked, "
                   f"{len(accepted)} moved")

    # -- reading -----------------------------------------------------

    def _live_appointments(self, start: date, end: date) -> List[Appointment]:
        window = (datetime.combine(start, time.min), datetime.combine(end, time.min))
        seen = []
        for client_id in list(self.clients) + [PROVIDER]:
            seen.extend(self.store.appointments_for(client_id, *window))
        return sorted(seen, key=lambda a: a.range.start)

    def snapshot(self, horizon_days: int = 21) -> dict:
        start = date.today()
        end = start + timedelta(days=horizon_days)
        window = (datetime.combine(start, time.min), datetime.combine(end, time.min))

        history = []
        for client_id in list(self.clients) + [PROVIDER]:
            for a in self.store.appointment_history(client_id, *window):
                history.append({
                    "id": a.id, "client_id": a.client_id,
                    "start": a.range.start.isoformat(), "end": a.range.end.isoformat(),
                    "status": a.status.value, "origin": a.origin.value,
                    "locked": a.locked, "supersedes": a.supersedes,
                })

        return {
            "today": start.isoformat(),
            "horizon": end.isoformat(),
            "clients": [{"id": c.id, "name": c.name} for c in self.clients.values()],
            "settings": {"alpha": self.alpha,
                         "max_displacements": self.max_displacements,
                         "bounds": {"earlier": self.bounds.max_days_earlier,
                                    "later": self.bounds.max_days_later}},
            "appointments": history,
            "availability": {
                client_id: [
                    {"start": s.start.isoformat(), "end": s.end.isoformat()}
                    for s in self.availability_segments(client_id, start, end)
                ]
                for client_id in list(self.clients) + [PROVIDER]
            },
            "weekly": {
                client_id: [
                    {"weekday": r.weekday,
                     "from": r.start_time.strftime("%H:%M"),
                     "to": r.end_time.strftime("%H:%M")}
                    for r in self.store.rules_for(client_id)
                ]
                for client_id in list(self.clients) + [PROVIDER]
            },
            "requests": [
                {"id": r.id, "client_id": r.client_id,
                 "duration": r.duration_minutes, "status": r.status,
                 "windows": [{"from": w.start.isoformat(), "to": w.end.isoformat()}
                             for w in r.windows]}
                for r in self.requests.values()
            ],
            "approvals": [
                {"id": a.id, "client_id": a.client_id,
                 "appointment_id": a.appointment_id, "status": a.status,
                 "was": a.was.start.isoformat(), "was_end": a.was.end.isoformat(),
                 "now": a.now.start.isoformat(), "now_end": a.now.end.isoformat()}
                for a in self.approvals.values()
            ],
            "log": self.log[-40:],
        }

    def _note(self, message: str) -> None:
        self.log.append(f"{datetime.now():%H:%M:%S}  {message}")


def _parse_time(text: str) -> time:
    hour, minute = text.split(":")[:2]
    return time(int(hour), int(minute))


def _parse_dt(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", ""))
