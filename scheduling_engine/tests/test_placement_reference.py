"""Cross-check the solver against exhaustive enumeration.

The gap model in placement.py is the one piece that isn't obviously correct by
reading: leftover-gap lengths are decision-dependent, so they're built as a
run-length counter over cells and converted to waste through a lookup table.
These tests re-derive the same cost in plain Python, enumerate every legal
assignment for deliberately tiny instances, and assert the solver returns one
of the true optima.
"""
import itertools
import random
from datetime import date, datetime, time, timedelta

import pytest
from calendar_store import TimeSegment

from scheduling_engine import (
    BookingRequest,
    CostConfig,
    TimeRange,
    solve_placements,
    waste_minutes,
)
from scheduling_engine.placement import _weights

ORIGIN = datetime.combine(date(2026, 5, 5), time.min)
GRID = 30


def moment(minutes: int) -> datetime:
    return ORIGIN + timedelta(minutes=minutes)


def overlaps(a, b):
    return a[0] < b[1] and b[0] < a[1]


def feasible_starts(duration, windows, free, grid):
    """Every start where the appointment fits inside one contiguous stretch of
    free-and-wanted time.
    """
    starts = []
    for w_start, w_end in windows:
        for f_start, f_end in free:
            lo, hi = max(w_start, f_start), min(w_end, f_end)
            lo = -(-lo // grid) * grid
            starts.extend(range(lo, hi - duration + 1, grid))
    return sorted(set(starts))


def reference_fragmentation(placed, free, catalogue):
    """Waste summed over every gap the placement leaves, computed directly
    from gap lengths rather than through the solver's cell machinery.
    """
    total = 0
    for f_start, f_end in free:
        inside = sorted(p for p in placed if f_start <= p[0] < f_end)
        cursor = f_start
        for p_start, p_end in inside:
            total += waste_minutes(p_start - cursor, catalogue)
            cursor = p_end
        total += waste_minutes(f_end - cursor, catalogue)
    return total


def brute_force(requests, free, config):
    """(most requests placed, cheapest cost at that count) by enumeration."""
    options = []
    for duration, windows in requests:
        starts = feasible_starts(duration, windows, free, config.grid_minutes)
        earliest = starts[0] if starts else 0
        options.append(
            [None] + [(s, s + duration, s - earliest) for s in starts]
        )

    w_frag, w_earli = _weights(config)
    best = None
    for combo in itertools.product(*options):
        taken = [c for c in combo if c]
        if any(
            overlaps(a, b) for a, b in itertools.combinations(taken, 2)
        ):
            continue
        frag = reference_fragmentation(
            [(s, e) for s, e, _ in taken], free, config.service_durations
        )
        earliness = sum(delay for _, _, delay in taken)
        key = (-len(taken), w_frag * frag + w_earli * earliness)
        if best is None or key < best:
            best = key
    return best


def run(requests, free, config):
    booked = [
        BookingRequest(
            id=f"r{i}",
            client_id=f"c{i}",
            duration_minutes=duration,
            desired=[TimeRange(moment(s), moment(e)) for s, e in windows],
        )
        for i, (duration, windows) in enumerate(requests)
    ]
    segments = [TimeSegment(moment(s), moment(e)) for s, e in free]
    return solve_placements(booked, segments, config)


# Hand-picked to exercise the awkward shapes: blocks that don't divide evenly
# by a service length, windows that start mid-block, and queues that can't
# fully fit.
INSTANCES = [
    ([(60, [(540, 720)])], [(540, 720)]),
    ([(60, [(570, 720)])], [(540, 720)]),
    ([(60, [(540, 720)]), (90, [(540, 720)])], [(540, 720)]),
    ([(90, [(540, 780)]), (90, [(540, 780)])], [(540, 780)]),
    ([(60, [(540, 720)]), (60, [(540, 720)]), (60, [(540, 720)])], [(540, 690)]),
    ([(60, [(540, 660)]), (90, [(600, 780)])], [(540, 660), (690, 810)]),
    ([(60, [(540, 900)]), (60, [(660, 900)])], [(540, 630), (660, 810)]),
    ([(90, [(540, 900)]), (60, [(540, 900)]), (60, [(720, 900)])], [(540, 750), (780, 900)]),
]


@pytest.mark.parametrize("requests,free", INSTANCES)
@pytest.mark.parametrize("alpha", [0.0, 0.5, 0.9, 1.0])
def test_solver_matches_exhaustive_enumeration(requests, free, alpha):
    config = CostConfig(alpha=alpha, grid_minutes=GRID)
    result = run(requests, free, config)

    placed_count, best_cost = brute_force(requests, free, config)
    assert len(result.placements) == -placed_count

    w_frag, w_earli = _weights(config)
    actual = w_frag * result.fragmentation_minutes + w_earli * result.earliness_minutes
    assert actual == best_cost


def _random_instance(rng):
    """Blocks and windows on the grid, sized to stay brute-forceable."""
    free, cursor = [], rng.randrange(0, 24) * GRID
    for _ in range(rng.randint(1, 3)):
        length = rng.randint(2, 8) * GRID
        free.append((cursor, cursor + length))
        cursor += length + rng.randint(1, 4) * GRID

    requests = []
    horizon_lo, horizon_hi = free[0][0], free[-1][1]
    for _ in range(rng.randint(1, 3)):
        start = horizon_lo + rng.randint(0, 6) * GRID
        end = start + rng.randint(2, 12) * GRID
        requests.append((rng.choice([60, 90]), [(start, min(end, horizon_hi + 120))]))
    return requests, free


@pytest.mark.parametrize("seed", range(40))
def test_random_instances_match_exhaustive_enumeration(seed):
    """The hand-picked instances above cover shapes I thought of. This covers
    the ones I didn't — same comparison, arbitrary calendars.
    """
    rng = random.Random(20260730 + seed)
    requests, free = _random_instance(rng)
    config = CostConfig(
        alpha=rng.choice([0.0, 0.25, 0.5, 0.75, 1.0]), grid_minutes=GRID
    )

    result = run(requests, free, config)
    placed_count, best_cost = brute_force(requests, free, config)
    w_frag, w_earli = _weights(config)

    assert len(result.placements) == -placed_count
    assert w_frag * result.fragmentation_minutes + w_earli * result.earliness_minutes == best_cost


@pytest.mark.parametrize("seed", range(40))
def test_random_placements_respect_every_hard_constraint(seed):
    rng = random.Random(20260730 + seed)
    requests, free = _random_instance(rng)
    result = run(requests, free, CostConfig(alpha=0.5, grid_minutes=GRID))

    spans = sorted(
        (
            int((p.range.start - ORIGIN).total_seconds() // 60),
            int((p.range.end - ORIGIN).total_seconds() // 60),
        )
        for p in result.placements
    )

    for start, end in spans:
        assert any(f_start <= start and end <= f_end for f_start, f_end in free), (
            "placement must sit inside a single free block"
        )
    for a, b in zip(spans, spans[1:]):
        assert a[1] <= b[0], "placements must not overlap"

    placed_ids = {p.request_id for p in result.placements}
    assert not placed_ids & set(result.unplaced)
    assert len(placed_ids) + len(result.unplaced) == len(requests)

    for placement in result.placements:
        duration, windows = requests[int(placement.request_id[1:])]
        start = int((placement.range.start - ORIGIN).total_seconds() // 60)
        assert placement.range.end - placement.range.start == timedelta(minutes=duration)
        assert any(w_start <= start and start + duration <= w_end for w_start, w_end in windows)


@pytest.mark.parametrize("requests,free", INSTANCES)
def test_reported_fragmentation_matches_the_placements_returned(requests, free):
    config = CostConfig(alpha=0.7, grid_minutes=GRID)
    result = run(requests, free, config)

    placed = [
        (
            int((p.range.start - ORIGIN).total_seconds() // 60),
            int((p.range.end - ORIGIN).total_seconds() // 60),
        )
        for p in result.placements
    ]
    assert result.fragmentation_minutes == reference_fragmentation(
        placed, free, config.service_durations
    )
