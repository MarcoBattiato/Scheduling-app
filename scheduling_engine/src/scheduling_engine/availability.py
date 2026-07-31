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

from typing import List, Sequence

from calendar_store import TimeSegment


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
