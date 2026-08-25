"""Turning "when is the provider available" into "when is the provider free".

calendar_store deliberately keeps these apart: `get_availability_segments`
reflects rules and exceptions only and never subtracts booked appointments,
because "availability" and "not double-booked" are separate constraints (its
INTERFACE.md, "Guarantees"). Netting them is the caller's job, and this is
that job — the seam SPEC.md §3 assigns to the repository adapter.

Kept as a pure function over segments rather than something that reaches into a
store. A store's interval algebra tends to run on a richer representation
(`portion.Interval` in calendar_store's case), and depending on that outside its
package would mean depending on that library too — so the subtraction is done on
the flat segment representation that crosses the boundary.

Inputs are duck-typed through `_span`, so segments from any store go in
unconverted. The return type is this package's own `TimeSegment`, because
constructing a value requires naming a class in a way that reading one does not.
"""
from __future__ import annotations

from datetime import datetime, time, timedelta
from typing import List, Sequence

from .models import RescheduleBounds, TimeRange, TimeSegment


def _span(item):
    """Accept a segment or range directly — this package's or a store's, the
    shape is what matters — or an appointment, which carries its span on
    `.range`.
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
