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
    # The slot they would like. A wish, not a constraint — where they *may* be
    # booked is their availability, resolved afresh at every solve.
    preferred_start: datetime
    # awaiting_client — out with the client; not replanned while they think
    # pending  — waiting, and may trigger a run of its own
    # on_hold  — already tried and not placed; still reconsidered by runs that
    #            happen anyway, but no longer demands one, which is what stops
    #            an unplaceable request whose date is approaching from firing
    #            the urgency trigger on every tick
    # placed / withdrawn — done with
    status: str = "pending"
    note: str = ""
    # Set when a booking is being moved but its slot is held until there is
    # somewhere to go. The old appointment is cancelled at the moment the
    # replacement is booked, and not before.
    replaces_appointment_id: Optional[int] = None
    # Who wanted this. A client asking is CLIENT; the clinic rehousing somebody
    # because the provider is away is DISPLACED, even though the client will be
    # asked and will say yes. Recording that as a preference is precisely the
    # failure `origin` exists to prevent — see calendar_store's CLAUDE.md.
    origin: Origin = Origin.CLIENT


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
    # pending | accepted | declined | refused | withdrawn
    #   declined  — not that slot, but still open to another
    #   refused   — not moving at all; only a reschedule can be refused
    #   withdrawn — taken back by us, because what it rested on fell through
    status: str = "pending"
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
    # draft | awaiting_clients | answered | applied | rejected
    #   answered — every client has replied; nothing is written until the
    #              provider says what to do with the answers
    status: str = "draft"
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
        self, client_id: str, service_id: str, preferred_start: str
    ) -> Request:
        """A client asks for a service at a time they would like.

        Duration comes from the catalogue — how long something takes is a
        property of the service, not of the asking. *Where* they may be booked
        comes from their availability, read afresh at each solve, so a stated
        wish never narrows what is possible for them.
        """
        service = self.catalogue.get_service(service_id)
        request = Request(
            id=next(self._ids),
            client_id=client_id,
            service_id=service_id,
            duration_minutes=service.duration_minutes,
            preferred_start=_parse_dt(preferred_start),
        )
        self.requests[request.id] = request
        self._note(f"{client_id} asked for {service.name} "
                   f"around {request.preferred_start:%a %d %b %H:%M}")
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

    # -- the provider's own diary ------------------------------------

    AWAY_ACTIONS = ("flag", "reschedule", "cancel")

    def provider_away(
        self, start: datetime, end: datetime, action: str = "flag"
    ) -> dict:
        """The provider cannot work a stretch of time.

        Marks it unavailable. What happens to the appointments already in it is
        a **separate decision**, and by default this does not make it: they are
        left in place and flagged (`orphaned_appointments`), because a mistyped
        date should not put half the week's clients through a rescheduling
        nobody wanted.

        `action` runs the follow-up in the same breath when the provider is
        sure — "I am ill tomorrow, cancel everything" is one decision, not two.
        It is exactly equivalent to marking the time off and then calling
        `rehouse_orphans` or `cancel_orphans` on what it stranded.
        """
        if action not in self.AWAY_ACTIONS:
            raise ValueError(f"action must be one of {self.AWAY_ACTIONS}, not {action!r}")
        if end <= start:
            raise ValueError("that is not a stretch of time")

        for day in range((end.date() - start.date()).days + 1):
            on = start.date() + timedelta(days=day)
            self.set_exception(
                PROVIDER, on,
                start.time() if on == start.date() else time.min,
                end.time() if on == end.date() else time.max,
                available=False,
            )

        stranded = [a.id for a in self.orphaned_appointments()
                    if a.range.start < end and start < a.range.end]
        self._note(f"provider away {start:%a %d %b %H:%M} – {end:%a %d %b %H:%M}"
                   + (f": {len(stranded)} appointment(s) now stranded"
                      if stranded else ""))

        if action == "cancel":
            return {"affected": len(stranded), "rehoused": [],
                    "cancelled": self.cancel_orphans(stranded)}
        if action == "reschedule":
            return {"affected": len(stranded), "cancelled": 0,
                    "rehoused": self.rehouse_orphans(stranded)}
        return {"affected": len(stranded), "rehoused": [], "cancelled": 0,
                "flagged": stranded}

    # -- bookings the provider no longer has time for --------------------

    def orphaned_appointments(self, horizon_days: int = 28) -> List[Appointment]:
        """Live bookings sitting in time the provider is not available for.

        A pure query, deliberately. Changing availability — by the weekly grid,
        by dragging on the calendar, or by declaring a stretch off — never
        reschedules anything on its own. It only makes this list longer, and
        the provider decides what to do about it.

        Only the *provider's* availability counts. A client narrowing theirs
        says nothing about a booking they have already committed to; a provider
        who cannot be there means the hour cannot happen.
        """
        start = date.today()
        end = start + timedelta(days=horizon_days)
        free = self.availability_segments(PROVIDER, start, end)
        return [
            a for a in self._live_appointments(start, end)
            if not any(s.start <= a.range.start and a.range.end <= s.end
                       for s in free)
        ]

    def rehouse_orphans(self, appointment_ids: Optional[Sequence[int]] = None) -> List[int]:
        """Raise a request on each stranded client's behalf.

        **Their booking is kept** until there is somewhere to move it to,
        exactly as when a client asks to move themselves: being told "you have
        been cancelled, we will be in touch" is a worse outcome than an hour
        that is still notionally yours. It then goes through the queue like any
        request, so they are asked rather than told.

        `Origin.DISPLACED` from the moment it is booked. They will say yes, but
        they did not choose it.
        """
        wanted = None if appointment_ids is None else {int(i) for i in appointment_ids}
        being_dealt_with = self._asked_to_move()
        raised = []
        for appointment in self.orphaned_appointments():
            if wanted is not None and appointment.id not in wanted:
                continue
            if appointment.id in being_dealt_with:
                continue        # somebody is already moving this one
            request = self.submit_request(
                appointment.client_id, appointment.service_type_id,
                (appointment.preferred_start or appointment.range.start).isoformat(),
            )
            request.replaces_appointment_id = appointment.id
            request.origin = Origin.DISPLACED
            request.note = "the provider is not available then"
            raised.append(request.id)
        if raised:
            self._note(f"{len(raised)} stranded booking(s) queued to be rehoused")
        return raised

    def cancel_orphans(self, appointment_ids: Optional[Sequence[int]] = None) -> int:
        """End them outright instead. Nobody is offered another slot."""
        wanted = None if appointment_ids is None else {int(i) for i in appointment_ids}
        cancelled = 0
        for appointment in self.orphaned_appointments():
            if wanted is None or appointment.id in wanted:
                self.cancel_appointment(appointment.id, by=Party.PROVIDER)
                cancelled += 1
        if cancelled:
            self._note(f"{cancelled} stranded booking(s) cancelled")
        return cancelled

    def place_manually(
        self, request_id: int, start: datetime, lock: bool = True
    ) -> Appointment:
        """The provider booking a waiting request themselves.

        The point is to decide something *before* optimising, so the run plans
        around it rather than proposing it. Locked by default for that reason:
        a decision the provider has already made should not come back as a
        candidate for displacement.

        Refuses a clash with anything live or already promised — that is a bug,
        not a judgement call. Outside the provider's own hours is allowed and
        merely noted: they are the authority on their own time.
        """
        request = self.requests[request_id]
        if request.status not in ("pending", "on_hold"):
            raise ValueError(f"request {request_id} is {request.status}, not waiting")

        service = self.catalogue.get_service(request.service_id)
        end = start + timedelta(minutes=service.duration_minutes)
        clash = [
            a for a in self._live_appointments(start.date(),
                                               end.date() + timedelta(days=1))
            if a.range.start < end and start < a.range.end
        ]
        if clash:
            raise ValueError(
                f"that overlaps {clash[0].client_id}'s "
                f"{clash[0].range.start:%a %d %b %H:%M}")
        held = [h for h in self.pending_holds()
                if h.start < end and start < h.end]
        if held:
            raise ValueError("that time is already promised to somebody")

        booked = self.store.book_appointment(
            request.client_id, request.service_id, start, end,
            locked=lock, preferred_start=request.preferred_start,
        )
        request.status = "placed"
        free = self.availability_segments(PROVIDER, start.date(),
                                          end.date() + timedelta(days=1))
        outside = not any(s.start <= start and end <= s.end for s in free)
        self._note(
            f"provider booked {request.client_id} at {start:%a %d %b %H:%M}"
            + (" (locked)" if lock else "")
            + (" — outside the usual hours" if outside else "")
        )
        return booked

    def request_reschedule(
        self, appointment_id: int, preferred_start: str, release_slot: bool
    ) -> Request:
        """A client asking to be moved, without naming where.

        Distinct from `move_appointment`, which is a client who has found a
        slot themselves. Here they say only "not this time, ideally around
        then" and the scheduler looks — so it goes through the queue like any
        other request, and comes back to them as an offer they can accept.

        `release_slot` is the whole of the decision they have to make:

        - **True** — give the hour up now. It frees immediately for everybody
          else, and they take the risk of the replacement not being found.
        - **False** — keep it until there is somewhere to go. Nobody else can
          have it meanwhile, and the replacement must be found *elsewhere*,
          but they cannot end up with nothing.

        Either way the new booking is `Origin.CLIENT`: they asked to move, so
        wherever they land is a choice of theirs, not one imposed on them.
        """
        appointment = self.store.get_appointment(appointment_id)
        if not appointment.occupies_slot:
            raise ValueError("that appointment is not live")
        if any(r.replaces_appointment_id == appointment_id
               and r.status in ("pending", "on_hold", "awaiting_client")
               for r in self.requests.values()):
            raise ValueError("that booking is already waiting to be moved")
        # We are already asking them about this one. Letting both run would
        # leave the replacement trying to cancel an appointment the accepted
        # move had meanwhile superseded — so they answer that first.
        if any(a.appointment_id == appointment_id and a.kind == "reschedule"
               and self._outstanding(a) for a in self.approvals.values()):
            raise ValueError(
                "we have already asked about moving that one — answer that first")

        request = self.submit_request(
            appointment.client_id, appointment.service_type_id, preferred_start
        )
        if release_slot:
            self.cancel_appointment(appointment_id, by=Party.CLIENT)
            self._note(f"{appointment.client_id} gave up "
                       f"{appointment.range.start:%a %d %b %H:%M} to be rebooked")
        else:
            request.replaces_appointment_id = appointment_id
            self._note(f"{appointment.client_id} wants to move "
                       f"{appointment.range.start:%a %d %b %H:%M}, "
                       f"keeping it until there is somewhere to go")
        return request

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
            [r.preferred_start
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

        An accepted answer counts too, until it is applied. The client has
        agreed to that slot; handing it to somebody else while the provider
        decides what to do with the plan would be worse than never asking.
        """
        return [
            TimeRange(a.now_start, a.now_end)
            for a in self.approvals.values() if self._outstanding(a)
        ]

    @staticmethod
    def _outstanding(approval: Approval) -> bool:
        """Spoken for: either still with the client, or agreed but not written."""
        return approval.status == "pending" or (
            approval.status == "accepted" and not approval.applied)

    def _asked_to_move(self) -> set:
        """Appointments nobody should be asked about twice.

        Includes those whose client is already moving them of their own accord
        and has not yet been given a replacement — they are on their way out,
        and displacing them would be asking about a booking twice over.
        """
        return {a.appointment_id for a in self.approvals.values()
                if self._outstanding(a) and a.kind == "reschedule"} | {
            r.replaces_appointment_id for r in self.requests.values()
            if r.replaces_appointment_id is not None
            and r.status in ("pending", "on_hold", "awaiting_client")}

    def _in_scope(
        self,
        open_requests: List[Request],
        request_ids: Optional[Sequence[int]],
        window_end: date,
    ) -> tuple:
        """Split the queue into what this run is about and what it is not.

        Naming requests explicitly says "this run is these"; naming none falls
        back to the provider's standing rule. Either way the ones left out are
        returned rather than dropped, so the caller can say how many there
        were — a plan that looks light because half the queue was not in it
        should not read as a quiet week.
        """
        if request_ids is not None:
            chosen = {int(i) for i in request_ids}
            unknown = chosen - {r.id for r in open_requests}
            if unknown:
                raise ValueError(
                    f"request(s) {sorted(unknown)} are not waiting to be placed")
            return ([r for r in open_requests if r.id in chosen],
                    [r for r in open_requests if r.id not in chosen])

        if not self.policy.scope_to_horizon:
            return open_requests, []
        # Inclusive of the last day: a wish for the final afternoon of the
        # window is inside it. Anything earlier is in scope too — an overdue
        # request is the most in-scope thing there is.
        return ([r for r in open_requests if r.preferred_start.date() < window_end],
                [r for r in open_requests if r.preferred_start.date() >= window_end])

    def propose(
        self,
        now: Optional[datetime] = None,
        reason: str = "manual",
        detail: str = "",
        horizon_days: Optional[int] = None,   # None = the provider's setting
        request_ids: Optional[Sequence[int]] = None,
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

        `request_ids` runs over a subset — the provider choosing what this run
        is about. With nothing named, the queue is filtered by
        `SchedulingPolicy.scope_to_horizon`: only requests wishing for a time
        inside the window are considered, which is what makes "plan next week
        on Monday" mean what it says. Without it, someone who asked for a date
        a month out would be booked into next week, because the horizon has
        cropped their availability to it and the nearest feasible slot is then
        the only slot there is.

        A request left out is left *alone*: it keeps its status rather than
        being parked, because it was never tried.
        """
        now = now or datetime.now()
        horizon_days = horizon_days or self.policy.horizon_days
        self.last_run = now

        # Bookings the provider no longer has time for. Never rescheduled as a
        # side effect of the availability changing — either the provider has
        # asked for that standing, or this only reports them.
        stranded = self.orphaned_appointments()
        warnings = []
        if stranded and self.policy.rehouse_orphans_on_run:
            raised = self.rehouse_orphans()
            if raised:
                warnings.append(
                    f"{len(raised)} booking(s) were in time you are not "
                    f"available for; they have been queued to be rehoused")
        elif stranded:
            warnings.append(
                f"{len(stranded)} booking(s) are in time you are not available "
                f"for, and were left alone")

        window_start = now.date()
        window_end = window_start + timedelta(days=horizon_days)

        open_requests = [r for r in self.requests.values()
                         if r.status in ("pending", "on_hold")]
        waiting, skipped = self._in_scope(open_requests, request_ids, window_end)
        if not waiting:
            return {"ran": False,
                    "reason": "nothing in this window" if skipped else "nothing waiting",
                    "out_of_scope": len(skipped), "warnings": warnings}
        alpha = self.alpha if alpha is None else alpha
        max_moves = (self.max_displacements if max_displacements is None
                     else max_displacements)
        chains = self.allow_chains if allow_chains is None else allow_chains

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
                    preferred_start=appointment.preferred_start,
                ))

        # Where a client may be booked is their availability — the same
        # constraint that governs where one of their bookings may be moved to.
        # Their stated wish is only a cost, so naming a slot cannot make them
        # unplaceable elsewhere, and cannot turn a narrow ask into a claim on
        # somebody else's hour.
        result = solve_placements(
            [BookingRequest(
                str(r.id), r.client_id, r.duration_minutes,
                [TimeRange(s.start, s.end) for s in self.availability_segments(
                    r.client_id, window_start, window_end)],
                preferred_start=r.preferred_start,
             )
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
                    "placements": 0, "displacements": 0, "on_hold": len(waiting),
                    "out_of_scope": len(skipped), "warnings": warnings}

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
                "preference_gap_minutes": result.preference_gap_minutes,
                "shift_minutes": result.shift_minutes,
                # Not a failure: these were never put to the solver. Shown so
                # a light-looking plan is not mistaken for a quiet week.
                "out_of_scope": len(skipped),
                "warnings": warnings,
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
                "on_hold": len(waiting) - len(placed_ids),
                "out_of_scope": len(skipped), "warnings": warnings}

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

    # -- settling a plan the clients have answered -------------------

    SETTLEMENTS = ("agreed", "agreed_only", "reoptimise")

    def settle_plan(self, plan_id: int, how: str) -> dict:
        """What the provider does once the answers start coming back.

        Accepting is an answer, not an action, so a plan out with the clients
        accumulates agreement without changing anything. This is where it turns
        into a calendar — and the provider decides when, because a half-applied
        rearrangement can be worse than none at all.

        - *agreed* — write down what has been agreed and keep waiting for the
          rest. Anything still outstanding stays outstanding.
        - *agreed_only* — write down what has been agreed and stop waiting.
          Outstanding asks are withdrawn; those requests go back in the queue.
        - *reoptimise* — none of it. Everything is withdrawn, agreed or not,
          and the scheduler runs again over the calendar as it now stands.

        Applying respects the engine's dependencies: a booking whose slot was
        to be vacated by somebody who has not answered stays where it is, and
        is picked up by a later call once they do.
        """
        if how not in self.SETTLEMENTS:
            raise ValueError(f"how must be one of {self.SETTLEMENTS}, not {how!r}")
        plan = self.plans[plan_id]
        if plan.status not in ("awaiting_clients", "answered"):
            raise ValueError(f"plan {plan_id} is {plan.status}, not out with clients")

        if how == "reoptimise":
            dropped = self._withdraw_outstanding(plan, "the provider rejected the plan")
            plan.status = "rejected"
            self._note(f"plan {plan_id} rejected, {dropped} ask(s) withdrawn; re-running")
            run = self.propose(reason="reoptimise",
                               detail=f"after rejecting plan {plan_id}")
            return {"applied": 0, "withdrawn": dropped, "rerun": run}

        applied = self._apply_agreed(plan)
        dropped = 0
        if how == "agreed_only":
            dropped = self._withdraw_outstanding(plan, "the provider stopped waiting")

        still_waiting = self.pending_approvals(plan.id)
        plan.status = "awaiting_clients" if still_waiting else "applied"
        self._note(
            f"plan {plan_id}: applied {applied} agreed change(s)"
            + (f", withdrew {dropped} unanswered" if dropped else "")
            + (f", still waiting on {len(still_waiting)}" if still_waiting else "")
        )
        return {"applied": applied, "withdrawn": dropped,
                "waiting": len(still_waiting)}

    def _apply_agreed(self, plan: Plan) -> int:
        """Write down everything agreed that is not blocked, in any order.

        Repeated until nothing more moves rather than sorted first: applying
        one move can unblock another, chains included, and letting the loop
        discover that is simpler than computing an order and being wrong about
        it.
        """
        applied = 0
        progress = True
        while progress:
            progress = False
            for approval in list(self.approvals.values()):
                if (approval.plan_id != plan.id or approval.applied
                        or approval.status != "accepted"):
                    continue
                self._apply_approval(approval)
                if approval.applied:
                    applied += 1
                    progress = True
        return applied

    def _withdraw_outstanding(self, plan: Plan, why: str) -> int:
        """Take back everything still in the air, and put the work back.

        A withdrawn ask must not leave its request in limbo: it goes back to
        `on_hold`, which is reconsidered by runs that happen anyway.
        """
        dropped = 0
        for approval in self.approvals.values():
            if approval.plan_id != plan.id or approval.applied:
                continue
            if approval.status not in ("pending", "accepted"):
                continue
            approval.status = "withdrawn"
            dropped += 1
            if approval.kind == "booking":
                request = self.requests.get(approval.request_id)
                if request and request.status not in ("placed", "withdrawn"):
                    request.status = "on_hold"
        if dropped:
            self._note(f"withdrew {dropped} outstanding ask(s) — {why}")
        return dropped

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

    ANSWERS = ("accept", "decline", "refuse")

    def respond_to_approval(self, approval_id: int, answer: str) -> None:
        """Apply, decline, or refuse one client's part of the plan.

        Each answer stands on its own (engine SPEC.md §7.4): one client
        declining does not undo what another has already agreed to. The single
        exception is a booking that only exists because somebody was going to
        move — if that move is refused, the slot was never free.

        Being asked to move has three honest answers, not two, and the
        difference is worth a great deal to the scheduler:

        - *accept* — apply it.
        - *decline* — "not that time." Blocks the offered slot on that date and
          nothing more. The appointment stays movable, so the next run can look
          for somewhere else.
        - *refuse* — "not at all." Pins the appointment; it is never offered up
          again.

        Collapsing the middle answer into the last one was the old behaviour,
        and it made the calendar seize up: one "not Tuesday at three" removed an
        appointment from consideration permanently. Only the client can tell
        those apart, so only the client is asked.

        A *booking* offer has no third answer — there is nothing to refuse to
        move — so it accepts only the first two.
        """
        if answer not in self.ANSWERS:
            raise ValueError(f"answer must be one of {self.ANSWERS}, not {answer!r}")
        approval = self.approvals[approval_id]
        if approval.status != "pending":
            return
        if answer == "refuse" and approval.kind != "reschedule":
            raise ValueError("only a reschedule can be refused; an offer is declined")
        approval.status = {"accept": "accepted", "decline": "declined",
                           "refuse": "refused"}[answer]
        plan = self.plans.get(approval.plan_id)

        if answer == "accept":
            # Deliberately *not* applied here. An accepted move is a change the
            # client is willing to make, not one that has happened: the
            # provider still has to decide what to do with the answers as a
            # whole, and half a rearrangement is often worse than none.
            self._note(f"{approval.client_id} accepted "
                       f"{approval.now_start:%a %d %b %H:%M} — not applied yet")
        else:
            self._record_refusal(approval, pin=answer == "refuse")

        if plan and plan.status == "awaiting_clients" \
                and not self.pending_approvals(plan.id):
            plan.status = "answered"
            self._note(f"plan {plan.id}: everyone has answered")

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
            origin=request.origin,
            preferred_start=request.preferred_start,
        )
        request.status = "placed"
        approval.applied = True
        self._note(f"booked {approval.client_id} at "
                   f"{approval.now_start:%a %d %b %H:%M}")

        # The old slot goes only now — the point of not releasing it was that
        # they would never be left with neither. Who cancelled it follows who
        # wanted the move: a client rehoused because the provider is away did
        # not give their hour up.
        if request.replaces_appointment_id is not None:
            old = self.store.get_appointment(request.replaces_appointment_id)
            if old.occupies_slot:
                self.cancel_appointment(
                    request.replaces_appointment_id,
                    by=Party.PROVIDER if request.origin is Origin.DISPLACED
                    else Party.CLIENT,
                )

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
        """Everything that rested on this move, whether or not it had answered.

        The pending ones matter as much as the agreed ones. A slot that was
        only going to be free because somebody was going to vacate it is not a
        real question any more, and leaving it out with the client is worse
        than useless: it asks them to confirm something that cannot happen, and
        until they answer, the plan never settles and the request is never
        replanned.
        """
        for other in self.approvals.values():
            if (other.applied or other.plan_id != refused.plan_id
                    or refused.appointment_id not in other.depends_on):
                continue
            if other.status == "pending":
                other.status = "withdrawn"
                self._note(f"took back {other.client_id}'s offer — it needed "
                           f"{refused.client_id} to move")
            elif other.status != "accepted":
                continue
            self._strand(other, refused)

    def _record_refusal(self, approval: Approval, pin: bool) -> None:
        """A rejected slot becomes a hole in that client's availability.

        Only for that date (engine SPEC.md §9): a client saying no to next
        Tuesday at three says nothing about Tuesdays in general. That single
        statement is the whole of a *decline* — everything else about the
        client is left alone, so the next run is free to find them somewhere
        better.

        `pin` is the extra thing a *refusal* says: not this appointment, ever.
        It is what the client chose, not something inferred from their having
        said no once.
        """
        self.store.remove_exception_availability(
            approval.client_id, approval.now_start.date(),
            approval.now_start.time(), approval.now_end.time(),
        )
        self._note(f"{approval.client_id} declined {approval.now_start:%a %d %b %H:%M}; "
                   f"blocked that slot for them")

        if approval.kind == "reschedule":
            if pin:
                self.immovable.add(approval.appointment_id)
                self._note(f"{approval.client_id} will not move at all; "
                           f"that appointment is now fixed")
            # Either way this move is not happening *now*, so whatever was
            # waiting on the slot falls through. A decline only leaves the door
            # open for a future run, not for this plan.
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

    def snapshot(self, horizon_days: int = 28) -> dict:
        """What every view needs. Deliberately wider than the planning horizon:
        the calendar can show four weeks, and it should not go blank just
        because the scheduler only plans one week ahead."""
        start = date.today()
        end = start + timedelta(days=horizon_days)
        window = (datetime.combine(start, time.min), datetime.combine(end, time.min))

        stranded = {a.id for a in self.orphaned_appointments(horizon_days)}

        # What is hanging over each booking, so a client can see at a glance
        # whether their slot is settled — the question they actually have.
        hanging = {}
        for approval in self.approvals.values():
            if approval.kind != "reschedule" or not self._outstanding(approval):
                continue
            hanging[approval.appointment_id] = {
                "state": "agreed" if approval.status == "accepted" else "asked",
                "now": approval.now_start.isoformat(),
                "now_end": approval.now_end.isoformat(),
            }
        for request in self.requests.values():
            if (request.replaces_appointment_id is not None
                    and request.status in ("pending", "on_hold", "awaiting_client")):
                hanging.setdefault(request.replaces_appointment_id,
                                   {"state": "moving", "now": None, "now_end": None})

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
                    "preferred": (a.preferred_start.isoformat()
                                  if a.preferred_start else None),
                    "price": self._service_price(a.service_type_id),
                    "notes": a.notes,
                    # asked  — we have proposed moving them, no answer yet
                    # agreed — they said yes; not written until the provider
                    #          settles the plan
                    # moving — they asked to move it themselves and are
                    #          holding the slot until there is somewhere to go
                    "pending": hanging.get(a.id),
                    # In time the provider is not available for. Flagged only —
                    # changing availability never reschedules anything by
                    # itself; see World.orphaned_appointments.
                    "orphaned": a.id in stranded,
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
                 "preferred": r.preferred_start.isoformat(),
                 "replaces": r.replaces_appointment_id,
                 # Why this exists. A request the clinic raised on somebody's
                 # behalf should not be indistinguishable from one they asked
                 # for — in the queue least of all.
                 "origin": r.origin.value, "note": r.note}
                for r in self.requests.values()
            ],
            "approvals": [
                {"id": a.id, "plan_id": a.plan_id, "kind": a.kind,
                 "client_id": a.client_id, "status": a.status,
                 # Agreed is not the same as done: an accepted answer waits
                 # for the provider to settle the plan.
                 "applied": a.applied,
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
                "horizon_days": self.policy.horizon_days,
                "scope_to_horizon": self.policy.scope_to_horizon,
                "rehouse_orphans_on_run": self.policy.rehouse_orphans_on_run,
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
