"""In-memory repository for client availability rules, single-date exceptions,
and booked appointments, plus the materialization function that turns rules
and exceptions into a concrete availability calendar for a queried window.

Assumptions (not asked about, kept simple since nothing in scope needs
otherwise): all times are naive (single implicit timezone per client), and a
rule/exception never spans midnight.
"""
from __future__ import annotations

import itertools
from dataclasses import replace
from datetime import date, datetime, time
from typing import List, Optional, Tuple

import portion as P
from dateutil.rrule import rrule, WEEKLY, MO, TU, WE, TH, FR, SA, SU

from .models import (
    Appointment,
    AppointmentStatus,
    AvailabilityException,
    ClientAvailabilityRule,
    Kind,
    Origin,
    Party,
)
from .segments import TimeSegment, to_segments

_WEEKDAY_CONST = [MO, TU, WE, TH, FR, SA, SU]

# Sentinel standing in for "no end" during the date-axis sweep in
# _apply_rule_change, so open-ended rules can be compared/sorted like any
# other breakpoint. Never leaves this module — models.py uses None.
_UNBOUNDED = date.max


class AvailabilityStore:
    def __init__(self) -> None:
        self._rules: List[ClientAvailabilityRule] = []
        self._exceptions: List[AvailabilityException] = []
        self._appointments: List[Appointment] = []
        self._ids = itertools.count(1)

    # -- mutations: recurring rules ----------------------------------

    def add_recurring_availability(
        self,
        client_id: str,
        weekday: int,
        start_time: time,
        end_time: time,
        effective_from: date,
        effective_until: Optional[date] = None,
    ) -> List[ClientAvailabilityRule]:
        return self._apply_rule_change(
            client_id, weekday, P.closedopen(start_time, end_time),
            effective_from, effective_until, Kind.ADD,
        )

    def remove_recurring_availability(
        self,
        client_id: str,
        weekday: int,
        start_time: time,
        end_time: time,
        effective_from: date,
        effective_until: Optional[date] = None,
    ) -> List[ClientAvailabilityRule]:
        return self._apply_rule_change(
            client_id, weekday, P.closedopen(start_time, end_time),
            effective_from, effective_until, Kind.REMOVE,
        )

    def rules_for(self, client_id: str, weekday: Optional[int] = None) -> List[ClientAvailabilityRule]:
        return [
            r for r in self._rules
            if r.client_id == client_id and (weekday is None or r.weekday == weekday)
        ]

    def _apply_rule_change(
        self,
        client_id: str,
        weekday: int,
        time_range: P.Interval,
        date_from: date,
        date_until: Optional[date],
        op: Kind,
    ) -> List[ClientAvailabilityRule]:
        """Sweep-and-merge normalization. See SPEC.md
        ("Rule normalization algorithm") for the worked-through rationale.
        """
        new_until = date_until or _UNBOUNDED

        existing = [r for r in self._rules if r.client_id == client_id and r.weekday == weekday]
        unrelated = [r for r in self._rules if not (r.client_id == client_id and r.weekday == weekday)]

        breakpoints = sorted(
            {date_from, new_until}
            | {r.effective_from for r in existing}
            | {r.effective_until or _UNBOUNDED for r in existing}
        )

        segments: List[Tuple[date, date, P.Interval]] = []
        for seg_start, seg_end in zip(breakpoints, breakpoints[1:]):
            if seg_start == seg_end:
                continue

            coverage = P.empty()
            for r in existing:
                if r.effective_from <= seg_start and (r.effective_until or _UNBOUNDED) >= seg_end:
                    coverage |= P.closedopen(r.start_time, r.end_time)

            if date_from <= seg_start and new_until >= seg_end:
                coverage = (coverage | time_range) if op is Kind.ADD else (coverage - time_range)

            if not coverage.empty:
                segments.append((seg_start, seg_end, coverage))

        merged: List[Tuple[date, date, P.Interval]] = []
        for seg in segments:
            if merged and merged[-1][1] == seg[0] and merged[-1][2] == seg[2]:
                merged[-1] = (merged[-1][0], seg[1], merged[-1][2])
            else:
                merged.append(seg)

        new_rules = []
        for seg_start, seg_end, coverage in merged:
            until = None if seg_end == _UNBOUNDED else seg_end
            for atomic in coverage:
                new_rules.append(ClientAvailabilityRule(
                    id=next(self._ids),
                    client_id=client_id,
                    weekday=weekday,
                    start_time=atomic.lower,
                    end_time=atomic.upper,
                    effective_from=seg_start,
                    effective_until=until,
                ))

        self._rules = unrelated + new_rules
        return new_rules

    # -- mutations: single-date exceptions ---------------------------

    def add_exception_availability(
        self, client_id: str, on_date: date, start_time: time, end_time: time,
    ) -> List[AvailabilityException]:
        return self._apply_exception_change(
            client_id, on_date, P.closedopen(start_time, end_time), Kind.ADD,
        )

    def remove_exception_availability(
        self, client_id: str, on_date: date, start_time: time, end_time: time,
    ) -> List[AvailabilityException]:
        return self._apply_exception_change(
            client_id, on_date, P.closedopen(start_time, end_time), Kind.REMOVE,
        )

    def exceptions_for(self, client_id: str, on_date: Optional[date] = None) -> List[AvailabilityException]:
        return [
            e for e in self._exceptions
            if e.client_id == client_id and (on_date is None or e.date == on_date)
        ]

    def _rule_coverage(self, client_id: str, on_date: date) -> P.Interval:
        """What the (already-normalized) rules alone provide on this single date."""
        weekday = on_date.weekday()
        coverage = P.empty()
        for r in self._rules:
            if (
                r.client_id == client_id and r.weekday == weekday
                and r.effective_from <= on_date
                and (r.effective_until is None or on_date < r.effective_until)
            ):
                coverage |= P.closedopen(r.start_time, r.end_time)
        return coverage

    def _apply_exception_change(
        self, client_id: str, on_date: date, time_range: P.Interval, op: Kind,
    ) -> List[AvailabilityException]:
        """Resolved against the day's current effective truth (rule coverage
        adjusted by whatever exceptions already exist for that date), not
        just the rule — so a remove can cancel out an earlier add-exception
        even where no rule is involved. Only the net deviation from the rule
        is ever stored: an exception fully redundant with (or fully outside)
        that truth naturally reduces to nothing and is discarded. See
        SPEC.md ("Exception normalization algorithm").
        """
        rule_coverage = self._rule_coverage(client_id, on_date)

        others = [e for e in self._exceptions if not (e.client_id == client_id and e.date == on_date)]
        existing = [e for e in self._exceptions if e.client_id == client_id and e.date == on_date]

        stored_adds = P.empty()
        stored_removes = P.empty()
        for e in existing:
            span = P.closedopen(e.start_time, e.end_time)
            if e.kind is Kind.ADD:
                stored_adds |= span
            else:
                stored_removes |= span

        current_truth = (rule_coverage - stored_removes) | stored_adds
        new_truth = (current_truth | time_range) if op is Kind.ADD else (current_truth - time_range)

        new_adds = new_truth - rule_coverage
        new_removes = rule_coverage - new_truth

        new_exceptions = []
        for atomic in new_adds:
            new_exceptions.append(AvailabilityException(
                id=next(self._ids), client_id=client_id, date=on_date,
                start_time=atomic.lower, end_time=atomic.upper, kind=Kind.ADD,
            ))
        for atomic in new_removes:
            new_exceptions.append(AvailabilityException(
                id=next(self._ids), client_id=client_id, date=on_date,
                start_time=atomic.lower, end_time=atomic.upper, kind=Kind.REMOVE,
            ))

        self._exceptions = others + new_exceptions
        return new_exceptions

    # -- mutations: booked appointments -------------------------------
    #
    # Not availability, not normalized — see SPEC.md ("Booked appointments
    # are not availability"). No double-booking check on `book_appointment`;
    # that's left to whoever queries this store.

    def book_appointment(
        self,
        client_id: str,
        service_type_id: str,
        start: datetime,
        end: datetime,
        *,
        locked: bool = False,
        notes: Optional[str] = None,
        preferred_start: Optional[datetime] = None,
        origin: Origin = Origin.CLIENT,
    ) -> Appointment:
        """`origin` defaults to CLIENT because most bookings are asked for.

        It is not always so. A booking that only exists because the clinic had
        to rehouse somebody is DISPLACED from the moment it is made — the
        client will have agreed to the slot, but they did not choose it, and
        recording that as a preference is the failure this field exists to
        prevent.
        """
        appointment = Appointment(
            id=next(self._ids),
            client_id=client_id,
            service_type_id=service_type_id,
            range=TimeSegment(start, end),
            locked=locked,
            notes=notes,
            preferred_start=preferred_start,
            origin=origin,
        )
        self._appointments.append(appointment)
        return appointment

    def cancel_appointment(self, appointment_id: int, *, by: Party) -> Appointment:
        """Mark an appointment cancelled. The row stays; it is history now.

        Deleting it would destroy the only evidence that the client ever held
        that slot, which is exactly what anything learning from past bookings
        needs. Cancelled rows no longer occupy time — see `appointments_for`.

        `by` has no default on purpose. A client dropping their own slot is
        evidence about that slot; the provider closing a day says nothing about
        the client, and treating the two alike would poison any reading of the
        history. Making the caller state it means it cannot be forgotten.
        """
        appointment = self._get_appointment(appointment_id)
        cancelled = replace(appointment, status=(
            AppointmentStatus.CANCELLED_BY_CLIENT if by is Party.CLIENT
            else AppointmentStatus.CANCELLED_BY_PROVIDER
        ))
        self._appointments[self._appointments.index(appointment)] = cancelled
        return cancelled

    def mark_attendance(self, appointment_id: int, *, attended: bool) -> Appointment:
        """Record whether the client turned up. The provider's call.

        Only a booked appointment can be marked: an appointment that was
        cancelled or moved never happened at that time, so it can be neither
        attended nor missed.
        """
        appointment = self._get_appointment(appointment_id)
        if appointment.status is not AppointmentStatus.BOOKED:
            raise ValueError(
                f"appointment {appointment_id} is {appointment.status.value}, "
                "so attendance cannot be recorded for it"
            )
        marked = replace(appointment, status=(
            AppointmentStatus.COMPLETED if attended else AppointmentStatus.NO_SHOW
        ))
        self._appointments[self._appointments.index(appointment)] = marked
        return marked

    def reschedule_appointment(
        self,
        appointment_id: int,
        start: datetime,
        end: datetime,
        *,
        origin: Origin = Origin.CLIENT,
    ) -> Appointment:
        """Move an appointment by writing a *new* row and retiring the old one.

        Overwriting the range in place would erase where the booking originally
        sat, and that is the signal worth keeping: a client who books Tuesday
        at 15:00 every week has a habit, but one repeatedly pushed to Friday to
        make room for others does not — they have been disrupted. `origin`
        records which of the two this move is, so the difference survives.

        Returns the new row; the old one keeps its id with status SUPERSEDED.
        """
        appointment = self._get_appointment(appointment_id)
        retired = replace(appointment, status=AppointmentStatus.SUPERSEDED)
        self._appointments[self._appointments.index(appointment)] = retired

        moved = replace(
            appointment,
            id=next(self._ids),
            range=TimeSegment(start, end),
            status=AppointmentStatus.BOOKED,
            origin=origin,
            supersedes=appointment.id,
        )
        self._appointments.append(moved)
        return moved

    def appointments_for(
        self, client_id: str, window_start: datetime, window_end: datetime
    ) -> List[Appointment]:
        """Live appointments for `client_id` overlapping the half-open window
        [window_start, window_end).

        Cancelled and superseded rows are excluded: they no longer occupy
        time, and a caller checking for double-booking must not see them. Use
        `appointment_history` to read those.
        """
        return [
            a for a in self._appointments
            if a.occupies_slot and a.client_id == client_id
            and a.range.start < window_end and window_start < a.range.end
        ]

    def appointment_history(
        self, client_id: str, window_start: datetime, window_end: datetime
    ) -> List[Appointment]:
        """Every row for `client_id` overlapping the window, whatever its
        status — what was booked, cancelled, and moved, and by whom.

        This is the raw material for working out a client's habitual slot; it
        is deliberately separate from `appointments_for` so that no caller
        asking "what is booked" can accidentally see retired rows.
        """
        return [
            a for a in self._appointments
            if a.client_id == client_id
            and a.range.start < window_end and window_start < a.range.end
        ]

    def get_appointment(self, appointment_id: int) -> Appointment:
        """One appointment by id, whatever its status.

        Includes superseded and cancelled ones: the history is append-only, so
        looking up an id must not depend on the record still being live.
        """
        for a in self._appointments:
            if a.id == appointment_id:
                return a
        raise KeyError(f"no appointment with id {appointment_id}")

    _get_appointment = get_appointment      # the older, internal name

    # -- query --------------------------------------------------------

    def get_availability(self, client_id: str, window_start: date, window_end: date) -> P.Interval:
        """Concrete availability calendar over the half-open window
        [window_start, window_end), as a `portion.Interval` of datetimes.
        """
        calendar = P.empty()
        for rule in self._rules:
            if rule.client_id != client_id:
                continue
            calendar |= self._expand_rule(rule, window_start, window_end)

        exception_adds = P.empty()
        exception_removes = P.empty()
        for exc in self._exceptions:
            if exc.client_id != client_id or not (window_start <= exc.date < window_end):
                continue
            span = P.closedopen(
                datetime.combine(exc.date, exc.start_time),
                datetime.combine(exc.date, exc.end_time),
            )
            if exc.kind is Kind.ADD:
                exception_adds |= span
            else:
                exception_removes |= span

        # Safe regardless of order: by construction, exception_removes is
        # always a subset of what the rules provide on its date, and
        # exception_adds never overlaps rule coverage on its date.
        calendar = (calendar - exception_removes) | exception_adds

        window = P.closedopen(
            datetime.combine(window_start, time.min),
            datetime.combine(window_end, time.min),
        )
        return calendar & window

    def get_availability_segments(
        self, client_id: str, window_start: date, window_end: date
    ) -> List[TimeSegment]:
        """The public boundary entry point — see SPEC.md ("Boundary with
        scheduling_engine"). Everything outside this package should call this,
        not `get_availability`.
        """
        return to_segments(self.get_availability(client_id, window_start, window_end))

    def _expand_rule(
        self, rule: ClientAvailabilityRule, window_start: date, window_end: date
    ) -> P.Interval:
        start = max(rule.effective_from, window_start)
        end = min(rule.effective_until, window_end) if rule.effective_until else window_end
        if start >= end:
            return P.empty()

        occurrences = rrule(
            WEEKLY,
            byweekday=_WEEKDAY_CONST[rule.weekday],
            dtstart=datetime.combine(start, time.min),
            until=datetime.combine(end, time.min),
        )
        span = P.empty()
        for occ in occurrences:
            day = occ.date()
            if day >= end:
                continue
            span |= P.closedopen(
                datetime.combine(day, rule.start_time),
                datetime.combine(day, rule.end_time),
            )
        return span
