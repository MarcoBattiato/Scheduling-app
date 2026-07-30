"""In-memory repository for client availability rules and single-date
exceptions, plus the materialization function that turns them into a
concrete availability calendar for a queried window.

Assumptions (not asked about, kept simple since nothing in scope needs
otherwise): all times are naive (single implicit timezone per client), and a
rule/exception never spans midnight.
"""
from __future__ import annotations

import itertools
from datetime import date, datetime, time
from typing import List, Optional, Tuple

import portion as P
from dateutil.rrule import rrule, WEEKLY, MO, TU, WE, TH, FR, SA, SU

from .models import AvailabilityException, ClientAvailabilityRule, Kind

_WEEKDAY_CONST = [MO, TU, WE, TH, FR, SA, SU]

# Sentinel standing in for "no end" during the date-axis sweep in
# _apply_rule_change, so open-ended rules can be compared/sorted like any
# other breakpoint. Never leaves this module — models.py uses None.
_UNBOUNDED = date.max


class AvailabilityStore:
    def __init__(self) -> None:
        self._rules: List[ClientAvailabilityRule] = []
        self._exceptions: List[AvailabilityException] = []
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
        """Sweep-and-merge normalization. See DESIGN_NOTES.md ("Rule
        normalization algorithm") for the worked-through rationale.
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
        DESIGN_NOTES.md ("Exception normalization").
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
