"""Turning "when is the provider available" into "when is the provider free".

calendar_store deliberately keeps these apart: `get_availability_segments`
reflects rules and exceptions only and never subtracts booked appointments,
because "availability" and "not double-booked" are separate constraints (its
INTERFACE.md, "Guarantees"). Netting them is the caller's job, and this is
that job — the seam SPEC.md §3 assigns to the repository adapter.

Kept as a pure function over `TimeSegment`s rather than something that reaches
into an `AvailabilityStore`. calendar_store's interval algebra runs on
`portion.Interval`, and its INTERFACE.md is explicit that depending on that
outside the package means depending on `portion` too — so the subtraction is
done on the flat segment representation that crosses the boundary.
"""
from __future__ import annotations

from datetime import datetime, time, timedelta
from typing import List, Sequence

from calendar_store import TimeSegment

from .models import RescheduleBounds, TimeRange


def _span(item):
    """Accept a `TimeSegment`/`TimeRange` directly, or an `Appointment` (which
    carries its span on `.range`).
    """
    inner = getattr(item, "range", item)
    return inner.start, inner.end


def free_time(
    availability: Sequence, booked: Sequence = ()
) -> List[TimeSegment]:
    """Availability with every booked span cut out of it.

    Overlapping or unsorted `booked` entries are fine. Output is sorted and
    non-overlapping, matching what calendar_store guarantees for segments, so
    it can be handed straight to `solve_placements`.
    """
    cuts = sorted(_span(item) for item in booked)

    free: List[TimeSegment] = []
    for segment in sorted(availability, key=lambda s: _span(s)[0]):
        start, end = _span(segment)
        cursor = start
        for cut_start, cut_end in cuts:
            if cut_end <= cursor or cut_start >= end:
                continue
            if cut_start > cursor:
                free.append(TimeSegment(cursor, cut_start))
            cursor = max(cursor, cut_end)
        if cursor < end:
            free.append(TimeSegment(cursor, end))
    return free


def reschedule_windows(
    current: TimeRange,
    client_availability: Sequence,
    bounds: RescheduleBounds,
) -> List[TimeRange]:
    """Where an accepted booking may be moved to.

    The client's own availability, clipped to the days their reschedule bounds
    allow (SPEC.md §4). Bounds are measured in whole days from the appointment's
    current date, so `{0, 0}` confines a move to the same day — which is what
    keeps a `provider-self` block such as lunch from being pushed to another
    date at all.

    Staying put is deliberately not included: it is always legal regardless of
    what the client's availability says now, since availability may have been
    edited since they booked.
    """
    day = current.start.date()
    lower = datetime.combine(day - timedelta(days=bounds.max_days_earlier), time.min)
    upper = datetime.combine(
        day + timedelta(days=bounds.max_days_later) + timedelta(days=1), time.min
    )

    windows = []
    for segment in client_availability:
        inner = getattr(segment, "range", segment)
        start, end = max(inner.start, lower), min(inner.end, upper)
        if start < end:
            windows.append(TimeRange(start, end))
    return sorted(windows, key=lambda w: w.start)
