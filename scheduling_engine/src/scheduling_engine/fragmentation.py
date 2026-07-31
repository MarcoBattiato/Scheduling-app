"""How bad is the free time a placement leaves behind?

A leftover gap is only worth anything if a *real* future booking can still go
in it, so waste is measured against the provider's service catalogue rather
than against round numbers:

    waste(gap) = gap - (the most service-time that can still be packed into it)

For a 60/90-minute catalogue this collapses to the obvious rule — a gap under
60 minutes is a total loss, and anything above it wastes only the remainder
past the nearest 30 — but deriving it from the catalogue means adding, say, a
45-minute service changes the cost model for free, with no magic numbers to
chase down.

Counting *gaps* rather than scoring a single placement is what makes edge
placement beat middle placement: putting an appointment against a block
boundary leaves one gap, putting it in the middle splits the block into two.
"""
from __future__ import annotations

from typing import List, Sequence


def waste_table(max_cells: int, service_cells: Sequence[int]) -> List[int]:
    """`table[n]` = cells wasted by a leftover gap of `n` grid cells.

    Unbounded knapsack over the catalogue: `best[n]` is the most cells a gap of
    `n` can still be filled with, so the rest is dead space. A gap can hold
    several future bookings, hence *unbounded* rather than a single best fit.
    """
    usable = sorted({d for d in service_cells if d > 0})
    best = [0] * (max_cells + 1)
    for n in range(1, max_cells + 1):
        # Carry the previous best forward: a gap is never worse off for being
        # bigger, even when the extra cell fits nothing new.
        best[n] = best[n - 1]
        for d in usable:
            if d <= n:
                best[n] = max(best[n], best[n - d] + d)
    return [n - best[n] for n in range(max_cells + 1)]


def waste_minutes(gap_minutes: int, service_durations: Sequence[int]) -> int:
    """Direct, grid-free version of the above — for a single gap.

    The solver uses `waste_table` instead (it needs every gap length up front,
    as a lookup indexed by a decision variable), but this is the definition
    that reads clearly in tests and at a call site.
    """
    if gap_minutes <= 0:
        return 0
    return waste_table(gap_minutes, [int(d) for d in service_durations])[gap_minutes]
