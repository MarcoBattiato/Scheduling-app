"""The shared world: one store, one engine, one set of pending negotiations.

Everything the browser can do goes through `World`. It is deliberately the only
place that knows both packages, so the HTTP layer above stays a thin translation
of JSON to method calls.
"""
from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Dict, List, Optional, Sequence

from calendar_store import (
    Appointment,
    AvailabilityStore,
    Origin,
    Party,
    ServiceCatalogue,
    TimeSegment,
)
from .policy import SchedulingPolicy, due

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
    service_id: str
    duration_minutes: int
    windows: List[TimeRange]
    # awaiting_client — out with the client; not replanned while they think
    # pending  — waiting, and may trigger a run of its own
    # on_hold  — already tried and not placed; still reconsidered by runs that
    #            happen anyway, but no longer demands one, which is what stops
    #            an unplaceable request whose date is approaching from firing
    #            the urgency trigger on every tick
    # placed / withdrawn — done with
    status: str = "pending"
    note: str = ""


@dataclass
class Approval:
    """Something the scheduler wants a client to agree to.

    Two kinds, deliberately handled by one object because the client
    experience is the same question: "is this time all right?" A `booking` is
    a new appointment they asked for; a `reschedule` is one they already have,
    being moved to make room for someone else.

    Owned by this mock, not by the engine — see SPEC.md §4.
    """
    id: int
    plan_id: int
    client_id: str
    kind: str = "booking"            # booking | reschedule
    now_start: Optional[datetime] = None
    now_end: Optional[datetime] = None
    request_id: Optional[int] = None      # booking
    appointment_id: Optional[int] = None  # reschedule
    was_start: Optional[datetime] = None
    was_end: Optional[datetime] = None
    status: str = "pending"          # pending | accepted | declined
    applied: bool = False
    # Appointment ids that must move before this can happen. Reported by the
    # engine rather than inferred here: guessing from overlap is only right
    # while chains are forbidden.
    depends_on: tuple = ()


@dataclass
class Plan:
    """A solver result, held until the provider and then the clients agree.

    Nothing in a plan touches the calendar until the individual approvals come
    back, and they are applied one by one rather than all together.
    """
    id: int
    placements: List[dict] = field(default_factory=list)
    displacements: List[dict] = field(default_factory=list)
    status: str = "draft"            # draft | awaiting_clients | applied | rejected
    reason: str = "manual"           # why the scheduler ran
    detail: str = ""
    # What the optimiser was asked for, and what it achieved. Several drafts
    # can sit side by side under different settings so the provider can see
    # the trade-off rather than being told about it.
    params: dict = field(default_factory=dict)
    metrics: dict = field(default_factory=dict)
    created_at: datetime = field(default_factory=datetime.now)

    def item_key(self, kind: str, ident: int) -> str:
        return f"{'p' if kind == 'booking' else 'm'}:{ident}"


class World:
    def __init__(self) -> None:
        self.store = AvailabilityStore()
        self.catalogue = ServiceCatalogue()
        self.clients: Dict[str, Client] = {}
        self.requests: Dict[int, Request] = {}
        self.approvals: Dict[int, Approval] = {}
        self.plans: Dict[int, Plan] = {}
        self.log: List[str] = []
        self.alpha = 0.5
        self.max_displacements = 1
        self.bounds = RescheduleBounds(max_days_earlier=2, max_days_later=4)
        self.policy = SchedulingPolicy()
        self.allow_chains = False
        self.last_run: Optional[datetime] = None
        # Appointments a client refused to move this session. Cruder than the
        # engine's per-date blocking (SPEC.md §7 of the mock).
        self.immovable: set = set()
        self._ids = itertools.count(1)

    # -- people ------------------------------------------------------

    def add_client(
        self, client_id: str, name: str = "", mirror_provider: bool = False
    ) -> Client:
        """Register a client.

        `mirror_provider` gives them the provider's own weekly pattern, which
        makes them immediately bookable. A client with no availability can be
        booked (a request says what they want regardless) but can never be
        offered a *rescheduling*, so a calendar populated with such clients
        would quietly never exercise displacement at all.
        """
        if client_id in self.clients:
            raise ValueError(f"there is already a client called {client_id!r}")
        if not client_id.strip():
            raise ValueError("a client needs an id")

        client = Client(id=client_id, name=name or client_id.title())
        self.clients[client_id] = client
        self._note(f"client {client_id} joined")

        if mirror_provider:
            self.set_weekly_availability(client_id, [
                {"weekday": r.weekday,
                 "from": r.start_time.strftime("%H:%M"),
                 "to": r.end_time.strftime("%H:%M")}
                for r in self.store.rules_for(PROVIDER)
            ])
        return client

    def clear_exception(self, client_id: str, on_date: date,
                        start: time, end: time, was_available: bool) -> None:
        """Undo a single-date override by applying its opposite.

        calendar_store stores only the net deviation from the weekly pattern,
        so an add and a matching remove cancel out rather than piling up.
        """
        self.set_exception(client_id, on_date, start, end, not was_available)

    # -- availability ------------------------------------------------

    def set_exception(
        self, client_id: str, on_date: date, start: time, end: time, available: bool
    ) -> None:
        """Override the weekly pattern on one date only.

        Recurring rules say what a client's week normally looks like; this is
        for the week that is not normal — a Tuesday off, an extra Saturday.
        """
        if available:
            self.store.add_exception_availability(client_id, on_date, start, end)
        else:
            self.store.remove_exception_availability(client_id, on_date, start, end)
        self._note(
            f"{client_id} is {'available' if available else 'away'} "
            f"{on_date:%a %d %b} {start:%H:%M}-{end:%H:%M}"
        )

    def client_summary(self, client_id: str) -> dict:
        """Enough about a client to be worth showing on hover."""
        history = self.store.appointment_history(
            client_id, datetime(2000, 1, 1), datetime(2100, 1, 1)
        )
        counts = {}
        for appointment in history:
            counts[appointment.status.value] = counts.get(appointment.status.value, 0) + 1
        return {
            "booked": counts.get("booked", 0),
            "completed": counts.get("completed", 0),
            "no_show": counts.get("no_show", 0),
            "cancelled": (counts.get("cancelled_by_client", 0)
                          + counts.get("cancelled_by_provider", 0)),
            "moved_by_us": sum(1 for a in history if a.origin is Origin.DISPLACED),
            "open_requests": sum(1 for r in self.requests.values()
                                 if r.client_id == client_id
                                 and r.status in ("pending", "on_hold", "awaiting_client")),
        }

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
        self, client_id: str, service_id: str, windows: Sequence[dict]
    ) -> Request:
        """Duration comes from the catalogue, not the caller: how long a
        service takes is a property of the service, not of the asking."""
        service = self.catalogue.get_service(service_id)
        request = Request(
            id=next(self._ids),
            client_id=client_id,
            service_id=service_id,
            duration_minutes=service.duration_minutes,
            windows=[
                TimeRange(_parse_dt(w["from"]), _parse_dt(w["to"])) for w in windows
            ],
        )
        self.requests[request.id] = request
        self._note(f"{client_id} asked for {service.name}")
        return request

    def withdraw_request(self, request_id: int) -> None:
        request = self.requests[request_id]
        request.status = "withdrawn"
        self._note(f"{request.client_id} withdrew request {request_id}")

    def cancel_appointment(
        self, appointment_id: int, *, by: Party = Party.CLIENT
    ) -> Appointment:
        appointment = self.store.cancel_appointment(appointment_id, by=by)
        self._note(
            f"{by.value} cancelled {appointment.client_id}'s "
            f"{appointment.range.start:%a %d %b %H:%M}"
        )
        return appointment

    def mark_attendance(self, appointment_id: int, attended: bool) -> Appointment:
        """The provider recording whether the client turned up."""
        marked = self.store.mark_attendance(appointment_id, attended=attended)
        self._note(
            f"{marked.client_id} {'attended' if attended else 'did not show for'} "
            f"{marked.range.start:%a %d %b %H:%M}"
        )
        return marked

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

    def tick(self, now: Optional[datetime] = None) -> Optional[dict]:
        """Run the scheduler if the provider's policy says it is due.

        Called on every state poll, which is how "run at 08:00 on Monday"
        happens without a background thread. The decision itself is in
        policy.due(), so it can be tested without waiting for a clock.
        """
        now = now or datetime.now()
        trigger = due(
            self.policy, now, self.last_run,
            [min(w.start for w in r.windows)
             for r in self.requests.values() if r.status == "pending"],
            any(r.status in ("pending", "on_hold") for r in self.requests.values()),
        )
        if trigger is None:
            return None
        return self.propose(now=now, reason=trigger.reason, detail=trigger.detail)

    def pending_holds(self) -> List[TimeRange]:
        """Time already promised to somebody, though not yet confirmed.

        A re-run must plan around these or it would offer the same slot twice.
        Only the *destination* needs blocking here: where a booking is being
        moved from is still occupied by that booking — it does not move until
        the client agrees — so `free_time` already excludes it. What the other
        end needs instead is that the appointment stops being offered as
        movable, or the same client would be asked twice about it.
        """
        return [
            TimeRange(a.now_start, a.now_end)
            for a in self.approvals.values() if a.status == "pending"
        ]

    def _asked_to_move(self) -> set:
        return {a.appointment_id for a in self.approvals.values()
                if a.status == "pending" and a.kind == "reschedule"}

    def propose(
        self,
        now: Optional[datetime] = None,
        reason: str = "manual",
        detail: str = "",
        horizon_days: int = 21,
        alpha: Optional[float] = None,
        max_displacements: Optional[int] = None,
        allow_chains: Optional[bool] = None,
    ) -> dict:
        """Run the engine and put the answer in front of the provider.

        Writes nothing to the calendar. The result is a *proposal*: the
        provider approves it, then each affected client is asked, and only
        what comes back agreed is actually booked.

        Parked (ON_HOLD) requests are reconsidered here — being parked stops a
        request demanding runs of its own, it does not exclude it from runs
        that happen anyway.

        Several drafts may sit side by side, each run under different settings:
        nothing is reserved until one is approved, so proposing costs nothing.
        Only one plan may be *in flight* with the clients at a time, which is
        what stops two plans quietly promising the same slot.
        """
        now = now or datetime.now()
        self.last_run = now

        waiting = [r for r in self.requests.values()
                   if r.status in ("pending", "on_hold")]
        if not waiting:
            return {"ran": False, "reason": "nothing waiting"}
        alpha = self.alpha if alpha is None else alpha
        max_moves = (self.max_displacements if max_displacements is None
                     else max_displacements)
        chains = self.allow_chains if allow_chains is None else allow_chains

        window_start = now.date()
        window_end = window_start + timedelta(days=horizon_days)
        # Gap usability is measured against what can still be sold, so a
        # withdrawn service must stop making gaps of its length look useful.
        config = CostConfig(
            alpha=alpha,
            service_durations=self.catalogue.bookable_durations() or (60,),
        )

        availability = self.availability_segments(PROVIDER, window_start, window_end)
        live = self._live_appointments(window_start, window_end)
        # Anything already promised is treated as taken. This is the whole of
        # "lock and re-optimise": the engine is a pure function of the world it
        # is described, so planning around a commitment is a matter of
        # describing it, not of a reservation mechanism.
        provider_free = free_time(availability, list(live) + self.pending_holds())

        asked_to_move = self._asked_to_move()
        movable = []
        for appointment in live:
            if appointment.locked or appointment.id in self.immovable:
                continue
            if appointment.id in asked_to_move:
                continue        # already being asked about; do not ask twice
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

        result = solve_placements(
            [BookingRequest(str(r.id), r.client_id, r.duration_minutes, r.windows)
             for r in waiting],
            provider_free, config,
            movable=movable, max_displacements=max_moves,
            allow_chains=chains,
        )

        # Anything the engine could not place is parked, so its approaching
        # date stops demanding a fresh run on every tick.
        placed_ids = {int(p.request_id) for p in result.placements}
        for request in waiting:
            if request.id not in placed_ids:
                request.status = "on_hold"

        if not result.placements and not result.displacements:
            self._note(f"scheduler ran ({reason}) — nothing could be placed")
            return {"ran": True, "reason": reason, "detail": detail,
                    "placements": 0, "displacements": 0, "on_hold": len(waiting)}

        plan = Plan(
            id=next(self._ids),
            status="draft",
            reason=reason,
            detail=detail,
            params={"alpha": alpha, "max_displacements": max_moves,
                    "allow_chains": chains},
            metrics={
                "placed": len(result.placements),
                "unplaced": len(result.unplaced),
                "displacements": len(result.displacements),
                "fragmentation_minutes": result.fragmentation_minutes,
                "earliness_minutes": result.earliness_minutes,
                "shift_minutes": result.shift_minutes,
            },
            placements=[
                {"request_id": int(p.request_id), "client_id": p.client_id,
                 "service_id": self.requests[int(p.request_id)].service_id,
                 "start": p.range.start, "end": p.range.end,
                 "depends_on": [int(a) for a in p.depends_on]}
                for p in result.placements
            ],
            displacements=[
                {"appointment_id": int(d.appointment_id), "client_id": d.client_id,
                 "was_start": d.was.start, "was_end": d.was.end,
                 "now_start": d.now.start, "now_end": d.now.end,
                 "depends_on": [int(a) for a in d.depends_on]}
                for d in result.displacements
            ],
        )
        self.plans[plan.id] = plan
        self._note(
            f"scheduler ran ({reason}): {len(plan.placements)} to book, "
            f"{len(plan.displacements)} to move — awaiting the provider"
        )
        return {"ran": True, "reason": reason, "detail": detail,
                "plan_id": plan.id, "placements": len(plan.placements),
                "displacements": len(plan.displacements),
                "on_hold": len(waiting) - len(placed_ids)}

    # -- the provider's decision -------------------------------------

    def provider_approve(
        self, plan_id: int, items: Optional[Sequence[str]] = None
    ) -> dict:
        """Send some or all of a proposal on to the clients it affects.

        `items` are keys from the plan ("p:<request_id>", "m:<appointment_id>");
        None means all of it. A part that depends on a move is pulled in with
        it automatically — approving a booking while leaving the move that
        frees its slot behind would promise something that cannot happen.

        Still nothing written: the provider agreeing means the plan is worth
        asking about, not that it has happened. Approving one draft discards
        the rest, which were computed against a calendar this one is about to
        change.
        """
        plan = self.plans[plan_id]
        if plan.status != "draft":
            raise ValueError(f"plan {plan_id} is {plan.status}, not awaiting approval")

        chosen = self._expand(plan, items)
        plan.status = "awaiting_clients"

        for placement in plan.placements:
            if plan.item_key("booking", placement["request_id"]) in chosen:
                self._ask(plan, "booking", placement["client_id"],
                          request_id=placement["request_id"],
                          now_start=placement["start"], now_end=placement["end"],
                          depends_on=tuple(placement["depends_on"]))
                # Out with the client — not replanned while they are thinking,
                # or a re-run would offer the same person a second slot.
                self.requests[placement["request_id"]].status = "awaiting_client"
            else:
                self.requests[placement["request_id"]].status = "on_hold"
        for displacement in plan.displacements:
            if plan.item_key("reschedule", displacement["appointment_id"]) in chosen:
                self._ask(plan, "reschedule", displacement["client_id"],
                          appointment_id=displacement["appointment_id"],
                          was_start=displacement["was_start"],
                          was_end=displacement["was_end"],
                          now_start=displacement["now_start"],
                          now_end=displacement["now_end"],
                          depends_on=tuple(displacement["depends_on"]))

        dropped = 0
        for other in self.plans.values():
            if other.id != plan_id and other.status == "draft":
                other.status = "rejected"
                dropped += 1

        asked = len(self.pending_approvals(plan_id))
        self._note(
            f"provider approved {len(chosen)} of {len(plan.placements) + len(plan.displacements)} "
            f"item(s) in plan {plan_id}; asking {asked} client(s)"
            + (f", discarded {dropped} other draft(s)" if dropped else "")
        )
        return {"asked": asked, "approved": sorted(chosen), "discarded": dropped}

    def _expand(self, plan: Plan, items: Optional[Sequence[str]]) -> set:
        """Everything the provider picked, plus whatever it rests on.

        Dependencies come from the engine, so this follows real links rather
        than guessing from overlapping times, and it follows them transitively
        — with chains a move can itself be waiting on another.
        """
        everything = (
            {plan.item_key("booking", p["request_id"]) for p in plan.placements}
            | {plan.item_key("reschedule", d["appointment_id"])
               for d in plan.displacements}
        )
        if items is None:
            return everything

        unknown = set(items) - everything
        if unknown:
            raise ValueError(f"plan {plan.id} has no item(s) {sorted(unknown)}")

        needs = {}
        for placement in plan.placements:
            needs[plan.item_key("booking", placement["request_id"])] = [
                plan.item_key("reschedule", a) for a in placement["depends_on"]
            ]
        for displacement in plan.displacements:
            needs[plan.item_key("reschedule", displacement["appointment_id"])] = [
                plan.item_key("reschedule", a) for a in displacement["depends_on"]
            ]

        chosen, queue = set(), list(items)
        while queue:
            key = queue.pop()
            if key in chosen:
                continue
            chosen.add(key)
            queue.extend(needs.get(key, []))
        return chosen

    def discard_plan(self, plan_id: int) -> None:
        self.plans[plan_id].status = "rejected"
        self._note(f"discarded draft {plan_id}")

    def provider_reject(self, plan_id: int) -> None:
        plan = self.plans[plan_id]
        plan.status = "rejected"
        for request in self.requests.values():
            if request.id in {p["request_id"] for p in plan.placements}:
                request.status = "on_hold"
        self._note(f"provider rejected plan {plan_id}")

    def _ask(self, plan: Plan, kind: str, client_id: str, **fields) -> None:
        approval = Approval(
            id=next(self._ids), plan_id=plan.id, kind=kind, client_id=client_id,
            **fields,
        )
        self.approvals[approval.id] = approval

    def pending_approvals(self, plan_id: Optional[int] = None) -> List[Approval]:
        return [a for a in self.approvals.values()
                if a.status == "pending" and (plan_id is None or a.plan_id == plan_id)]

    # -- the client's decision ---------------------------------------

    def respond_to_approval(self, approval_id: int, accept: bool) -> None:
        """Apply, or refuse, one client's part of the plan.

        Each answer stands on its own (engine SPEC.md §7.4): one client
        declining does not undo what another has already agreed to. The single
        exception is a booking that only exists because somebody was going to
        move — if that move is refused, the slot was never free.
        """
        approval = self.approvals[approval_id]
        if approval.status != "pending":
            return
        approval.status = "accepted" if accept else "declined"
        plan = self.plans.get(approval.plan_id)

        if accept:
            self._apply_approval(approval)
        else:
            self._record_refusal(approval)

        if plan and not self.pending_approvals(plan.id):
            plan.status = "applied"
            self._note(f"plan {plan.id} settled")

    def _apply_approval(self, approval: Approval) -> None:
        blocker = self._unmet_dependency(approval)
        if blocker is not None:
            if blocker.status == "declined":
                self._strand(approval, blocker)
            return                      # still waiting, or never happening

        if approval.kind == "reschedule":
            # Agreed to, but not chosen: the client said yes to moving, they
            # did not pick the slot. Recording it as CLIENT would teach the
            # anchoring model that being pushed around is a preference.
            self.store.reschedule_appointment(
                approval.appointment_id, approval.now_start, approval.now_end,
                origin=Origin.DISPLACED,
            )
            approval.applied = True
            self._note(f"{approval.client_id} moved to "
                       f"{approval.now_start:%a %d %b %H:%M}")
            self._apply_dependent_bookings(approval)
        else:
            self._book(approval)

    def _apply_dependent_bookings(self, moved: Approval) -> None:
        """Do whatever was waiting on this slot being vacated.

        Includes other *moves*: with chains permitted one displaced booking can
        be waiting on another. The engine reports these dependencies, so this
        does not have to guess.
        """
        for other in self.approvals.values():
            if (other.status == "accepted" and not other.applied
                    and other.plan_id == moved.plan_id
                    and moved.appointment_id in other.depends_on):
                if other.kind == "booking":
                    self._book(other)
                else:
                    self._apply_approval(other)

    def _book(self, approval: Approval) -> None:
        request = self.requests[approval.request_id]
        self.store.book_appointment(
            approval.client_id, request.service_id,
            approval.now_start, approval.now_end,
        )
        request.status = "placed"
        approval.applied = True
        self._note(f"booked {approval.client_id} at "
                   f"{approval.now_start:%a %d %b %H:%M}")

    def _unmet_dependency(self, approval: Approval) -> Optional[Approval]:
        """A move this one needs that has not happened yet."""
        for other in self.approvals.values():
            if (other.appointment_id in approval.depends_on
                    and other.plan_id == approval.plan_id
                    and not other.applied):
                return other
        return None

    def _strand(self, approval: Approval, blocker: Approval) -> None:
        """Something that can never happen now, because what it needed was
        refused."""
        if approval.kind == "booking":
            request = self.requests.get(approval.request_id)
            if request:
                request.status = "on_hold"
        self._note(f"{approval.client_id}'s slot fell through — "
                   f"{blocker.client_id} would not move")

    def _cascade_refusal(self, refused: Approval) -> None:
        """Anything that had already agreed but was waiting on this."""
        for other in self.approvals.values():
            if (other.status == "accepted" and not other.applied
                    and other.plan_id == refused.plan_id
                    and refused.appointment_id in other.depends_on):
                self._strand(other, refused)

    def _record_refusal(self, approval: Approval) -> None:
        """A refused slot becomes a hole in that client's availability.

        Only for that date (engine SPEC.md §9): a client saying no to next
        Tuesday at three says nothing about Tuesdays in general.
        """
        self.store.remove_exception_availability(
            approval.client_id, approval.now_start.date(),
            approval.now_start.time(), approval.now_end.time(),
        )
        self._note(f"{approval.client_id} declined {approval.now_start:%a %d %b %H:%M}; "
                   f"blocked that slot for them")

        if approval.kind == "reschedule":
            # Blunter than the single-date block a refused booking gets, and
            # deliberately so: a client already holding an appointment has more
            # standing to keep it than one merely being offered a slot.
            self.immovable.add(approval.appointment_id)
            self._cascade_refusal(approval)
        else:
            request = self.requests.get(approval.request_id)
            if request:
                request.status = "on_hold"

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
                    "service_id": a.service_type_id,
                    "start": a.range.start.isoformat(), "end": a.range.end.isoformat(),
                    "status": a.status.value, "origin": a.origin.value,
                    "locked": a.locked, "supersedes": a.supersedes,
                    "service": self._service_name(a.service_type_id),
                    "price": self._service_price(a.service_type_id),
                    "notes": a.notes,
                })

        return {
            "today": start.isoformat(),
            "horizon": end.isoformat(),
            "clients": [
                {"id": c.id, "name": c.name, **self.client_summary(c.id)}
                for c in self.clients.values()
            ],
            "services": [
                {"id": s.id, "name": s.name, "duration": s.duration_minutes,
                 "price": s.price_minor_units, "active": s.active,
                 "client_bookable": s.client_bookable}
                for s in self.catalogue.services(include_inactive=True)
            ],
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
            "exceptions": {
                client_id: [
                    {"date": e.date.isoformat(),
                     "from": e.start_time.strftime("%H:%M"),
                     "to": e.end_time.strftime("%H:%M"),
                     "kind": e.kind.value}
                    for e in self.store.exceptions_for(client_id)
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
                {"id": r.id, "client_id": r.client_id, "service_id": r.service_id,
                 "service": self._service_name(r.service_id),
                 "duration": r.duration_minutes, "status": r.status,
                 "windows": [{"from": w.start.isoformat(), "to": w.end.isoformat()}
                             for w in r.windows]}
                for r in self.requests.values()
            ],
            "approvals": [
                {"id": a.id, "plan_id": a.plan_id, "kind": a.kind,
                 "client_id": a.client_id, "status": a.status,
                 "appointment_id": a.appointment_id,
                 "was": a.was_start.isoformat() if a.was_start else None,
                 "was_end": a.was_end.isoformat() if a.was_end else None,
                 "now": a.now_start.isoformat(), "now_end": a.now_end.isoformat()}
                for a in self.approvals.values()
            ],
            "plans": [
                {"id": p.id, "status": p.status, "reason": p.reason,
                 "detail": p.detail, "params": p.params, "metrics": p.metrics,
                 "placements": [
                     {"key": p.item_key("booking", x["request_id"]),
                      "request_id": x["request_id"],
                      "client_id": x["client_id"], "service_id": x["service_id"],
                      "service": self._service_name(x["service_id"]),
                      "start": x["start"].isoformat(), "end": x["end"].isoformat(),
                      "depends_on": [p.item_key("reschedule", a)
                                     for a in x["depends_on"]]}
                     for x in p.placements],
                 "displacements": [
                     {"key": p.item_key("reschedule", x["appointment_id"]),
                      "appointment_id": x["appointment_id"],
                      "client_id": x["client_id"],
                      "was": x["was_start"].isoformat(),
                      "was_end": x["was_end"].isoformat(),
                      "now": x["now_start"].isoformat(),
                      "now_end": x["now_end"].isoformat(),
                      "depends_on": [p.item_key("reschedule", a)
                                     for a in x["depends_on"]]}
                     for x in p.displacements]}
                for p in self.plans.values()
                if p.status in ("draft", "awaiting_clients")
            ],
            "scheduler": {
                "auto_run": self.policy.auto_run,
                "weekly_runs": [[d, t.strftime("%H:%M")]
                                for d, t in self.policy.weekly_runs],
                "urgency_hours": self.policy.urgency_hours,
                "retry_after_minutes": self.policy.retry_after_minutes,
                "last_run": self.last_run.isoformat() if self.last_run else None,
            },
            "log": self.log[-40:],
        }

    def _service_name(self, service_id: str) -> str:
        try:
            return self.catalogue.get_service(service_id).name
        except KeyError:
            return service_id

    def _service_price(self, service_id: str) -> int:
        try:
            return self.catalogue.get_service(service_id).price_minor_units
        except KeyError:
            return 0

    def _note(self, message: str) -> None:
        self.log.append(f"{datetime.now():%H:%M:%S}  {message}")


def _parse_time(text: str) -> time:
    hour, minute = text.split(":")[:2]
    return time(int(hour), int(minute))


def _parse_dt(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", ""))
