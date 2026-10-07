"""The shared world: one store, one engine, one set of pending negotiations.

Everything the browser can do goes through `World`. It is deliberately the only
place that knows both packages, so the HTTP layer above stays a thin translation
of JSON to method calls.
"""
from __future__ import annotations

import itertools
import secrets
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Dict, List, Optional, Sequence

from calendar_store import (
    Appointment,
    AppointmentStatus,
    AvailabilityStore,
    Origin,
    Party,
    ServiceCatalogue,
    TimeSegment,
)
from . import billing
from .billing import BillingSettings, Charge, Payment
from .policy import SchedulingPolicy, due

from scheduling_engine import (
    BookingRequest,
    ClientLimit,
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
    # How many sessions they may have in a day and in a week. Set by the
    # provider per client: once a day by default, since two sessions on one
    # day is almost never what anybody wants, and no weekly cap, since an
    # intensive week is a clinical decision. None means no limit.
    per_day: Optional[int] = 1
    per_week: Optional[int] = None
    # billing.MODES key, or None for the practice default.
    billing_mode: Optional[str] = None


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
    # The repeating booking this session belongs to, if any.
    series_id: Optional[int] = None


def every(weeks: int) -> str:
    return "every week" if weeks == 1 else f"every {weeks} weeks"


@dataclass
class Message:
    """One message in a client's conversation with the practice.

    Clinical: people write about why they are coming. Kept out of the shared
    state the screens poll, out of the activity log, and away from any
    assistant (the commands are FORBIDDEN to one) — read only through the gate.
    """
    id: int
    client_id: str                  # whose conversation
    author: str                     # "client" | "provider" — from the session
    text: str
    at: datetime
    read_at: Optional[datetime] = None   # when the other side read it


@dataclass
class ChatSettings:
    enabled: bool = True
    # Told to clients so nobody waits for a reply that is not coming tonight.
    reply_expectation: str = "Your clinician usually replies within one working day."
    # The practice confirms the numbers and wording.
    emergency_note: str = ("This is not an emergency service. If you are in danger "
                           "or need help now, call 995, or the SOS 24-hour line on 1767.")


@dataclass
class Series:
    """The same service at the same time every few weeks — weekly therapy,
    say — so the client does not have to ask for each session.

    A standing *request*, not a block of appointments. Each session is raised
    as an ordinary request once it comes inside the planning window, and goes
    through the scheduler and the client's agreement like any other. Booking a
    year of Tuesdays up front would hold those hours against everyone else
    without the scheduler ever weighing them, and would leave nothing to do
    when a Tuesday turns out to be impossible.

    The wished-for time is the series' own slot, so where a session lands is
    judged against when this client actually likes to come — which is the
    habitual-slot anchoring of engine SPEC §10.1, stated by the client rather
    than inferred.
    """
    id: int
    client_id: str
    service_id: str
    first_start: datetime          # the first session, and the anchor for the rest
    every_weeks: int = 1
    until: Optional[date] = None   # last date a session may fall on; None = until stopped
    # active   — raising sessions as they come into the window
    # finished — reached `until`; nothing more to raise
    # stopped  — called off
    status: str = "active"
    # Session dates already raised, whatever became of them. A session the
    # client withdrew, or one that could not be placed, is not raised again
    # on the next tick.
    raised: set = field(default_factory=set)

    def sessions(self):
        """Every session start, in order, forever (or until `until`)."""
        start = self.first_start
        while self.until is None or start.date() <= self.until:
            yield start
            start += timedelta(weeks=self.every_weeks)


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
        self.series: Dict[int, Series] = {}
        # Shown in every client's calendar instead of anything about the
        # appointment itself — see calendar_sync.py.
        self.practice_name = "the practice"
        # Person ("provider" or a client id) -> the secret in their calendar
        # feed's address. The address is the only credential a subscribed
        # calendar app can present, so it has to be unguessable.
        self.feed_tokens: Dict[str, str] = {}
        # Every command that went through the gate (control.py): who, through
        # which channel, in which conversation, and what came of it.
        self.audit: List[dict] = []
        # Client ↔ clinician conversations — see Message.
        self.messages: List[Message] = []
        self.chat = ChatSettings()
        # Money — see billing.py.
        self.billing = BillingSettings()
        self.charges: Dict[int, Charge] = {}
        self.payments: Dict[int, Payment] = {}
        self.processor, warning = billing.processor_from_environment()
        if warning:
            self._note(warning)
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
        """The client no longer wants it — including any offer already made.

        Marking only the request left the offer out with them and its slot
        held, and accepting it afterwards booked the appointment and flipped
        the request back to placed: a withdrawal that undid itself. Anybody
        asked to move purely to make room for it is no longer asked either.
        """
        request = self.requests[request_id]
        request.status = "withdrawn"
        self._note(f"{request.client_id} withdrew request {request_id}")
        for approval in self.approvals.values():
            if (approval.kind == "booking" and approval.request_id == request_id
                    and self._outstanding(approval)):
                approval.status = "withdrawn"
                self._note(f"took back the offer of "
                           f"{approval.now_start:%a %d %b %H:%M} — the request was withdrawn")
                self._drop_moves_nobody_needs(approval.plan_id)
                self._settle_if_answered(approval.plan_id)

    def set_client_limits(self, client_id: str, per_day: Optional[int],
                          per_week: Optional[int]) -> Client:
        """How often this client may be booked. Applies to every later run and
        to repeating bookings; nothing already booked is moved because of it."""
        client = self.clients[client_id]
        for name, value in (("per_day", per_day), ("per_week", per_week)):
            if value is not None and value < 1:
                raise ValueError(f"{name} must be at least 1, or no limit")
        client.per_day, client.per_week = per_day, per_week
        describe = lambda v, unit: f"{v} a {unit}" if v else f"any number a {unit}"
        self._note(f"{client_id} may now be booked {describe(per_day, 'day')}, "
                   f"{describe(per_week, 'week')}")
        return client

    def _client_limits(self, clients, window_start: date, window_end: date,
                       movable_ids: set) -> Dict[str, ClientLimit]:
        """Each client's limit, with every session the solver cannot move.

        Gathered over whole weeks, not just the planning window: a weekly
        limit has to count Monday's session when the run happens on Wednesday.
        A booking somebody is being asked to move counts where it is going,
        not where it is, since it will not be in both.
        """
        week_start = window_start - timedelta(days=window_start.weekday())
        week_end = window_end + timedelta(days=7 - window_end.weekday())
        span = (datetime.combine(week_start, time.min), datetime.combine(week_end, time.min))
        leaving = {a.appointment_id for a in self.approvals.values()
                   if a.kind == "reschedule" and self._outstanding(a)}
        limits = {}
        for client_id in clients:
            client = self.clients.get(client_id)
            if client is None or (client.per_day is None and client.per_week is None):
                continue
            fixed = [a.range.start for a in self.store.appointment_history(client_id, *span)
                     if a.status in (AppointmentStatus.BOOKED, AppointmentStatus.COMPLETED,
                                     AppointmentStatus.NO_SHOW)
                     and str(a.id) not in movable_ids and a.id not in leaving]
            fixed += [a.now_start for a in self.approvals.values()
                      if a.client_id == client_id and self._outstanding(a)]
            limits[client_id] = ClientLimit(per_day=client.per_day,
                                            per_week=client.per_week, fixed=fixed)
        return limits

    def _week_is_full(self, client_id: str, moment: datetime) -> bool:
        """Has this client already reached their weekly limit in that week?"""
        client = self.clients.get(client_id)
        if client is None or client.per_week is None:
            return False
        monday = moment.date() - timedelta(days=moment.weekday())
        span = (datetime.combine(monday, time.min),
                datetime.combine(monday + timedelta(days=7), time.min))
        booked = [a for a in self.store.appointment_history(client_id, *span)
                  if a.status in (AppointmentStatus.BOOKED, AppointmentStatus.COMPLETED)]
        waiting = [r for r in self.requests.values()
                   if r.client_id == client_id and span[0] <= r.preferred_start < span[1]
                   and r.status in ("pending", "on_hold", "awaiting_client")]
        return len(booked) + len(waiting) >= client.per_week

    # -- calendar feeds -------------------------------------------------

    def feed_token(self, person: str) -> str:
        """The secret for `person`'s calendar feed, made on first use."""
        if person != "provider" and person not in self.clients:
            raise KeyError(f"no client called {person!r}")
        if person not in self.feed_tokens:
            self.feed_tokens[person] = secrets.token_urlsafe(18)
        return self.feed_tokens[person]

    def reset_feed_token(self, person: str) -> str:
        """A new address; the old one stops working at once. For a link that
        was shared by mistake — every calendar subscribed to it goes quiet
        and has to be given the new one."""
        self.feed_tokens.pop(person, None)
        token = self.feed_token(person)
        self._note(f"{person}'s calendar link was replaced")
        return token

    def person_for_feed(self, token: str) -> Optional[str]:
        for person, known in self.feed_tokens.items():
            if secrets.compare_digest(known, token):
                return person
        return None

    # -- the clinician chat -----------------------------------------------

    MAX_MESSAGE = 4000

    def send_message(self, client_id: str, author: str, text: str) -> Message:
        if client_id not in self.clients:
            raise KeyError(f"no client called {client_id!r}")
        if author not in ("client", "provider"):
            raise ValueError("a message is written by the client or the provider")
        if not self.chat.enabled:
            raise ValueError("messaging is switched off for this practice")
        text = (text or "").strip()
        if not text:
            raise ValueError("an empty message is not sent")
        if len(text) > self.MAX_MESSAGE:
            raise ValueError(f"a message is at most {self.MAX_MESSAGE} characters")
        message = Message(id=next(self._ids), client_id=client_id, author=author,
                          text=text, at=datetime.now())
        self.messages.append(message)
        # That someone wrote, never what: the log is seen far more widely
        # than a conversation should be.
        self._note(f"{'the practice' if author == 'provider' else client_id} wrote "
                   f"in {client_id}'s conversation")
        return message

    def conversation(self, client_id: str) -> List[Message]:
        if client_id not in self.clients:
            raise KeyError(f"no client called {client_id!r}")
        return [m for m in self.messages if m.client_id == client_id]

    def mark_read(self, client_id: str, reader: str) -> int:
        """Everything the other side wrote in this conversation, as read."""
        other = "provider" if reader == "client" else "client"
        now, marked = datetime.now(), 0
        for m in self.conversation(client_id):
            if m.author == other and m.read_at is None:
                m.read_at, marked = now, marked + 1
        return marked

    def unread(self, client_id: str, reader: str) -> int:
        other = "provider" if reader == "client" else "client"
        return sum(1 for m in self.messages
                   if m.client_id == client_id and m.author == other and m.read_at is None)

    # -- repeating bookings --------------------------------------------

    MAX_EVERY_WEEKS = 12

    def start_series(
        self, client_id: str, service_id: str, first_start: str,
        every_weeks: int = 1, until: Optional[str] = None,
    ) -> Series:
        """Book the same thing every `every_weeks` weeks, from `first_start`.

        Sessions are raised as requests straight away for as far as the
        planning window reaches, and the rest as the window moves forward.
        """
        if client_id not in self.clients:
            raise KeyError(f"no client called {client_id!r}")
        service = self.catalogue.get_service(service_id)       # KeyError if unknown
        if not 1 <= int(every_weeks) <= self.MAX_EVERY_WEEKS:
            raise ValueError(f"every_weeks must be 1 to {self.MAX_EVERY_WEEKS}")
        start = _parse_dt(first_start)
        last = date.fromisoformat(until) if until else None
        if last is not None and last < start.date():
            raise ValueError("the last session cannot be before the first")

        series = Series(id=next(self._ids), client_id=client_id,
                        service_id=service_id, first_start=start,
                        every_weeks=int(every_weeks), until=last)
        self.series[series.id] = series
        self._note(f"{client_id} set up {service.name} {every(series.every_weeks)} "
                   f"from {start:%a %d %b %H:%M}"
                   + (f" until {last:%d %b}" if last else ""))
        self.raise_due_sessions()
        return series

    def stop_series(self, series_id: int) -> int:
        """No more sessions. Booked ones stay booked — each is cancelled on its
        own if wanted — and ones still being placed are withdrawn, offers
        included. Returns how many were withdrawn."""
        series = self.series[series_id]
        if series.status == "stopped":
            return 0
        series.status = "stopped"
        open_ones = [r for r in self.requests.values()
                     if r.series_id == series_id
                     and r.status in ("pending", "on_hold", "awaiting_client")]
        for request in open_ones:
            self.withdraw_request(request.id)
        self._note(f"{series.client_id} stopped their {every(series.every_weeks)} "
                   f"{self._service_name(series.service_id)}"
                   + (f"; withdrew {len(open_ones)} not yet booked" if open_ones else ""))
        return len(open_ones)

    def raise_due_sessions(self, now: Optional[datetime] = None) -> List[Request]:
        """Turn every series session now inside the planning window into a
        request. Idempotent: called on every tick, raises each session once.

        The window is the scheduler's own (`horizon_days` from today), so a
        session becomes a request exactly when a run could place it, and never
        sooner — a request raised months early would sit in the queue looking
        like work.
        """
        now = now or datetime.now()
        window_end = now.date() + timedelta(days=self.policy.horizon_days)
        raised = []
        for series in self.series.values():
            if series.status != "active":
                continue
            exhausted = True
            for start in series.sessions():
                if start.date() >= window_end:
                    exhausted = False
                    break
                if start <= now or start.date() in series.raised:
                    continue
                if self._week_is_full(series.client_id, start):
                    # Already as many sessions that week as they may have, so
                    # this one is skipped rather than raised to sit unplaceable
                    # in the queue. Recorded, so it is not tried again.
                    series.raised.add(start.date())
                    self._note(f"skipped {series.client_id}'s "
                               f"{start:%a %d %b} session — the week is already full")
                    continue
                request = self.submit_request(
                    series.client_id, series.service_id, start.isoformat())
                request.series_id = series.id
                request.note = every(series.every_weeks)
                series.raised.add(start.date())
                raised.append(request)
            if exhausted:          # ran past `until` inside the window
                series.status = "finished"
                self._note(f"{series.client_id}'s {every(series.every_weeks)} "
                           f"{self._service_name(series.service_id)} has had its last session")
        return raised

    def _next_session(self, series: Series) -> Optional[datetime]:
        """The next session not yet raised, for showing what happens next."""
        if series.status != "active":
            return None
        now = datetime.now()
        return next((s for s in series.sessions()
                     if s > now and s.date() not in series.raised), None)

    def cancel_appointment(
        self, appointment_id: int, *, by: Party = Party.CLIENT,
        as_move: bool = False, now: Optional[datetime] = None,
    ) -> Appointment:
        """`as_move` marks the old slot of a booking that is moving rather than
        going away. It is cancelled all the same, but it is not a cancellation
        in the sense a fee cares about: nobody is charged for moving."""
        appointment = self.store.cancel_appointment(appointment_id, by=by)
        self._note(
            f"{by.value} cancelled {appointment.client_id}'s "
            f"{appointment.range.start:%a %d %b %H:%M}"
        )
        if by is Party.CLIENT and not as_move:
            self._maybe_propose_late_fee(appointment, now or datetime.now())
        return appointment

    def mark_attendance(self, appointment_id: int, attended: bool) -> Appointment:
        """The provider recording whether the client turned up."""
        marked = self.store.mark_attendance(appointment_id, attended=attended)
        self._note(
            f"{marked.client_id} {'attended' if attended else 'did not show for'} "
            f"{marked.range.start:%a %d %b %H:%M}"
        )
        if attended:
            self._charge_for_session(marked)
        else:
            self._maybe_propose_no_show_fee(marked)
        return marked

    # -- money ---------------------------------------------------------

    def billing_mode(self, client_id: str) -> str:
        client = self.clients.get(client_id)
        return (client.billing_mode if client and client.billing_mode
                else self.billing.default_mode)

    def configure_billing(self, **changes) -> BillingSettings:
        """Change the practice's billing rules; unknown fields are refused,
        and nothing changes unless the result is valid as a whole."""
        current = billing.settings_dict(self.billing)
        unknown = set(changes) - set(current)
        if unknown:
            raise ValueError(f"no billing setting called {sorted(unknown)}")
        candidate = BillingSettings(**{**current, **{k: v for k, v in changes.items()
                                                      if v is not None}})
        candidate.gst_number = candidate.gst_number.strip()
        billing.validate(candidate)
        self.billing = candidate
        self._note("billing settings changed")
        return candidate

    def set_billing_mode(self, client_id: str, mode: Optional[str]) -> None:
        if mode is not None and mode not in billing.MODES:
            raise ValueError(f"billing mode must be one of {sorted(billing.MODES)}")
        self.clients[client_id].billing_mode = mode
        self._note(f"{client_id} is billed: {billing.MODES[mode] if mode else 'as the practice default'}")

    def _new_charge(self, appointment: Appointment, kind: str, amount: int,
                    status: str, reason: str = "") -> Optional[Charge]:
        if amount <= 0:
            return None
        if any(c.appointment_id == appointment.id and c.kind == kind
               for c in self.charges.values()):
            return None                    # one of each per appointment, ever
        when = f"{appointment.range.start:%a %d %b %Y}"
        description = {
            "session": f"Appointment on {when}",
            "late_cancel": f"Late cancellation of {when}",
            "no_show": f"Missed appointment on {when}",
        }[kind]
        charge = Charge(
            id=next(self._ids), client_id=appointment.client_id, kind=kind,
            amount=amount, gst=billing.gst_included(amount, self.billing),
            description=description, created=datetime.now(),
            appointment_id=appointment.id, status=status, reason=reason,
        )
        self.charges[charge.id] = charge
        self._note(f"{'proposed' if status == 'proposed' else 'charged'} "
                   f"{appointment.client_id} {billing.money(amount)} — {description}")
        return charge

    def _price_of(self, appointment: Appointment) -> int:
        return self._service_price(appointment.service_type_id) or 0

    def _charge_for_session(self, appointment: Appointment) -> Optional[Charge]:
        if appointment.client_id == PROVIDER or self.billing_mode(appointment.client_id) != "after_session":
            return None
        return self._new_charge(appointment, "session", self._price_of(appointment), "due")

    def _maybe_propose_no_show_fee(self, appointment: Appointment) -> Optional[Charge]:
        if appointment.client_id == PROVIDER or self.billing_mode(appointment.client_id) == "off":
            return None
        amount = billing.fee_amount(self.billing.no_show_fee, self._price_of(appointment))
        return self._new_charge(appointment, "no_show", amount, "proposed",
                                reason="did not come and did not cancel")

    def _maybe_propose_late_fee(self, appointment: Appointment, now: datetime) -> Optional[Charge]:
        """Only ever *proposed*: the provider confirms or waives each one."""
        if appointment.client_id == PROVIDER or self.billing_mode(appointment.client_id) == "off":
            return None
        notice = appointment.range.start - now
        window = timedelta(hours=self.billing.late_cancel_window_hours)
        if notice >= window:
            return None
        hours = max(0, int(notice.total_seconds() // 3600))
        amount = billing.fee_amount(self.billing.late_cancel_fee, self._price_of(appointment))
        return self._new_charge(
            appointment, "late_cancel", amount, "proposed",
            reason=(f"cancelled {hours} h before; the window is "
                    f"{self.billing.late_cancel_window_hours} h"))

    def confirm_fee(self, charge_id: int) -> Charge:
        charge = self.charges[charge_id]
        if charge.status != "proposed":
            raise ValueError(f"charge {charge_id} is {charge.status}, not a proposed fee")
        charge.status = "due"
        self._note(f"provider confirmed {charge.client_id}'s fee of {billing.money(charge.amount)}")
        return charge

    def waive_charge(self, charge_id: int) -> Charge:
        charge = self.charges[charge_id]
        if charge.status not in ("proposed", "due"):
            raise ValueError(f"charge {charge_id} is {charge.status}; only unpaid ones can be waived")
        if any(p.charge_id == charge_id and p.status == "pending" for p in self.payments.values()):
            raise ValueError("a payment for this is in progress; wait for it to finish")
        charge.status = "waived"
        self._note(f"provider waived {charge.client_id}'s {billing.money(charge.amount)} — {charge.description}")
        return charge

    def start_checkout(self, charge_id: int, return_url: str) -> Payment:
        """Send the client to the processor's hosted page to pay one charge."""
        charge = self.charges[charge_id]
        if charge.status != "due":
            raise ValueError(f"charge {charge_id} is {charge.status}, not owed")
        payment = Payment(id=next(self._ids), charge_id=charge_id, amount=charge.amount,
                          processor=self.processor.name, reference="")
        joiner = "&" if "?" in return_url else "?"
        back = f"{return_url}{joiner}paid={payment.id}"
        payment.reference, payment.checkout_url = self.processor.start_checkout(
            charge, f"{charge.description} — {self.practice_name}", back,
            self.billing.currency)
        payment.return_url = back
        self.payments[payment.id] = payment
        return payment

    def payment_by_reference(self, reference: str) -> Optional[Payment]:
        return next((p for p in self.payments.values() if p.reference == reference), None)

    def check_payment(self, payment_id: int) -> Payment:
        """Ask the processor what became of a checkout. Idempotent: a payment
        already settled is not asked about again, and a charge is never paid
        twice by the same money."""
        payment = self.payments[payment_id]
        if payment.status != "pending":
            return payment
        status, method = self.processor.check(payment.reference)
        if status == "pending":
            return payment
        payment.status, payment.method = status, method or payment.method
        if status == "succeeded":
            self._settle(self.charges[payment.charge_id], payment)
        return payment

    def record_payment(self, charge_id: int, method: str) -> Payment:
        """Money the provider took outside the processor."""
        if method not in billing.RECORDED_METHODS:
            raise ValueError(f"a recorded payment is one of {list(billing.RECORDED_METHODS)}")
        charge = self.charges[charge_id]
        if charge.status != "due":
            raise ValueError(f"charge {charge_id} is {charge.status}, not owed")
        payment = Payment(id=next(self._ids), charge_id=charge_id, amount=charge.amount,
                          processor="manual", reference=f"recorded by the provider",
                          status="succeeded", method=method)
        self.payments[payment.id] = payment
        self._settle(charge, payment)
        return payment

    def _settle(self, charge: Charge, payment: Payment) -> None:
        if charge.status == "paid":
            return
        charge.status, charge.paid_by = "paid", payment.id
        self._note(f"{charge.client_id} paid {billing.money(payment.amount)} "
                   f"({billing.PAY_METHODS.get(payment.method, payment.method or payment.processor)})")

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
            self.cancel_appointment(appointment_id, by=Party.CLIENT, as_move=True)
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
        # Repeating bookings first, so a session that just came into the window
        # is in the queue the policy is about to look at.
        self.raise_due_sessions(now)
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
        dry_run: bool = False,
    ) -> dict:
        """Run the engine and put the answer in front of the provider.

        `dry_run` answers "what would happen at these settings?" and changes
        nothing at all: no plan is stored, no request is parked, nothing is
        rehoused, and the run is not recorded. It is what lets an assistant
        compare settings freely — a read, not a write (`plan.preview`).

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
        if not dry_run:
            self.last_run = now

        # Bookings the provider no longer has time for. Never rescheduled as a
        # side effect of the availability changing — either the provider has
        # asked for that standing, or this only reports them.
        stranded = self.orphaned_appointments()
        warnings = []
        if stranded and self.policy.rehouse_orphans_on_run and not dry_run:
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
            client_limits=self._client_limits(
                {r.client_id for r in waiting} | {m.client_id for m in movable},
                window_start, window_end, {m.id for m in movable}),
        )

        # Anything the engine could not place is parked, so its approaching
        # date stops demanding a fresh run on every tick.
        placed_ids = {int(p.request_id) for p in result.placements}
        if dry_run:
            return {
                "ran": True, "dry_run": True,
                "params": {"alpha": alpha, "max_displacements": max_moves},
                "metrics": {
                    "placed": len(result.placements), "unplaced": len(result.unplaced),
                    "displacements": len(result.displacements),
                    "fragmentation_minutes": result.fragmentation_minutes,
                    "preference_gap_minutes": result.preference_gap_minutes,
                },
                "placements": [
                    {"request_id": int(p.request_id), "client_id": p.client_id,
                     "start": p.range.start, "end": p.range.end,
                     "wanted": self.requests[int(p.request_id)].preferred_start}
                    for p in result.placements],
                "displacements": [
                    {"appointment_id": int(d.appointment_id), "client_id": d.client_id,
                     "was_start": d.was.start, "now_start": d.now.start}
                    for d in result.displacements],
                "unplaced": [int(r) for r in result.unplaced],
                "out_of_scope": len(skipped), "warnings": warnings,
            }
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
        def written():
            return sum(1 for a in self.approvals.values()
                       if a.plan_id == plan.id and a.applied)

        before = written()
        progress = True
        while progress:
            progress = False
            for approval in list(self.approvals.values()):
                if (approval.plan_id != plan.id or approval.applied
                        or approval.status != "accepted"):
                    continue
                self._apply_approval(approval)
                if approval.applied:
                    progress = True
        # Counted from the record rather than per iteration: writing one move
        # also writes whatever was waiting on it, and those count too.
        return written() - before

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
            if blocker.status in self.DEAD:
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
        for other in list(self.approvals.values()):
            if (other.status == "accepted" and not other.applied
                    and other.plan_id == moved.plan_id
                    and moved.appointment_id in other.depends_on):
                # Through the same gate as everything else. Booking here
                # directly wrote a placement down as soon as *one* of the moves
                # it needed had happened — so with two moves needed and one
                # still unanswered, two people were booked into the same hour.
                self._apply_approval(other)

    def _book(self, approval: Approval) -> None:
        request = self.requests[approval.request_id]
        if request.status == "withdrawn":
            # Belt and braces: withdraw_request takes the offer back, so this
            # should be unreachable. If it is reached, the client's "no longer
            # wanted" outranks an earlier "yes".
            approval.status = "withdrawn"
            return
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
                    as_move=True,
                )

    # An ask in one of these states is never going to happen.
    DEAD = ("declined", "refused", "withdrawn")

    def _unmet_dependency(self, approval: Approval) -> Optional[Approval]:
        """A move this one needs that has not happened yet.

        One that never will is reported ahead of one still waiting, so that a
        booking resting on two moves is stranded as soon as either falls
        through rather than held until the other is answered.
        """
        unmet = [other for other in self.approvals.values()
                 if other.appointment_id in approval.depends_on
                 and other.plan_id == approval.plan_id
                 and not other.applied]
        dead = [other for other in unmet if other.status in self.DEAD]
        return (dead or unmet or [None])[0]

    def _drop_moves_nobody_needs(self, plan_id: int) -> None:
        """Stop asking people to move to make room for something that is no
        longer coming.

        A move exists for what rests on it. Once everything that did has fallen
        away, asking the client to move anyway is disruption for nothing. A
        move with nothing depending on it at all was approved on its own by the
        provider, and is left alone.
        """
        changed = True
        while changed:
            changed = False
            for move in self.approvals.values():
                if (move.plan_id != plan_id or move.kind != "reschedule"
                        or not self._outstanding(move)):
                    continue
                needed_by = [a for a in self.approvals.values()
                             if a.plan_id == plan_id
                             and move.appointment_id in a.depends_on]
                if needed_by and all(a.status in self.DEAD and not a.applied
                                      for a in needed_by):
                    move.status = "withdrawn"
                    changed = True
                    self._note(f"stopped asking {move.client_id} to move — "
                               f"nothing needs the slot any more")

    def _settle_if_answered(self, plan_id: int) -> None:
        plan = self.plans.get(plan_id)
        if plan and plan.status == "awaiting_clients" and not self.pending_approvals(plan_id):
            plan.status = "answered"
            self._note(f"plan {plan_id}: everyone has answered")

    def _strand(self, approval: Approval, blocker: Approval) -> None:
        """Something that can never happen now, because what it needed was
        refused.

        Final: the ask is withdrawn, so its slot stops being held and a later
        settlement cannot pick an old "yes" back up and write it down.
        """
        approval.status = "withdrawn"
        if approval.kind == "booking":
            request = self.requests.get(approval.request_id)
            if request and request.status != "withdrawn":
                request.status = "on_hold"
        self._note(f"{approval.client_id}'s slot fell through — "
                   f"{blocker.client_id} would not move")
        # The other moves made for it are now disruption for nothing — unless
        # already written down, which is past undoing.
        self._drop_moves_nobody_needs(approval.plan_id)

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
                {"id": c.id, "name": c.name, "per_day": c.per_day,
                 "per_week": c.per_week, "billing_mode": c.billing_mode,
                 **self.client_summary(c.id)}
                for c in self.clients.values()
            ],
            "services": [
                {"id": s.id, "name": s.name, "duration": s.duration_minutes,
                 "price": s.price_minor_units, "active": s.active,
                 "client_bookable": s.client_bookable}
                for s in self.catalogue.services(include_inactive=True)
            ],
            "practice_name": self.practice_name,
            # The chat's settings — what clients are told — but never a word
            # of any conversation: those are fetched through the gate.
            "chat": {"enabled": self.chat.enabled,
                     "reply_expectation": self.chat.reply_expectation,
                     "emergency_note": self.chat.emergency_note},
            "billing": {**billing.settings_dict(self.billing),
                        "modes": billing.MODES, "methods": billing.PAY_METHODS,
                        "recorded_methods": list(billing.RECORDED_METHODS),
                        "processor": self.processor.name,
                        "processor_label": self.processor.label},
            "charges": [
                {"id": c.id, "client_id": c.client_id, "kind": c.kind,
                 "amount": c.amount, "gst": c.gst, "description": c.description,
                 "status": c.status, "reason": c.reason,
                 "created": c.created.isoformat(),
                 "appointment_id": c.appointment_id, "paid_by": c.paid_by}
                for c in self.charges.values()
            ],
            "payments": [
                {"id": p.id, "charge_id": p.charge_id, "amount": p.amount,
                 "processor": p.processor, "status": p.status, "method": p.method,
                 "created": p.created.isoformat()}
                for p in self.payments.values()
            ],
            # Every person's feed secret, to anyone who asks. No worse than the
            # rest of this payload until sign-in exists — CONTROL_SURFACE.md §8
            # — but the phone only ever shows a person their own.
            # Only links already made: making one is a change, and must happen
            # in a request that saves the session, or a restart would hand out
            # a different address and every subscribed calendar would break.
            "feeds": dict(self.feed_tokens),
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
                 "origin": r.origin.value, "note": r.note,
                 "series_id": r.series_id}
                for r in self.requests.values()
            ],
            "series": [
                {"id": x.id, "client_id": x.client_id, "service_id": x.service_id,
                 "service": self._service_name(x.service_id),
                 "first_start": x.first_start.isoformat(),
                 "every_weeks": x.every_weeks,
                 "until": x.until.isoformat() if x.until else None,
                 "status": x.status,
                 "next": (lambda n: n.isoformat() if n else None)(self._next_session(x))}
                for x in self.series.values()
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
