"""Interval-algebra query helpers on top of `portion.Interval` calendars
returned by `AvailabilityStore.get_availability`.
"""
from __future__ import annotations

import functools
import operator
from datetime import date, datetime, time

import portion as P


def _bounds(window_start: date, window_end: date) -> P.Interval:
    return P.closedopen(
        datetime.combine(window_start, time.min),
        datetime.combine(window_end, time.min),
    )


def crop(calendar: P.Interval, window_start: date, window_end: date) -> P.Interval:
    """Restrict `calendar` to the half-open window [window_start, window_end)."""
    return calendar & _bounds(window_start, window_end)


def union(*calendars: P.Interval) -> P.Interval:
    if not calendars:
        return P.empty()
    return functools.reduce(operator.or_, calendars)


def intersect(*calendars: P.Interval) -> P.Interval:
    """Availability common to all given calendars (e.g. client ∩ provider)."""
    if not calendars:
        return P.empty()
    return functools.reduce(operator.and_, calendars)


def negate(calendar: P.Interval, window_start: date, window_end: date) -> P.Interval:
    """Unavailable time within [window_start, window_end) — the bounded
    complement of `calendar`. An unbounded complement isn't meaningful here.
    """
    return _bounds(window_start, window_end) - calendar
