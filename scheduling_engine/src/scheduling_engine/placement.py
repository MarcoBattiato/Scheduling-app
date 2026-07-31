"""Joint placement of a queue of pending bookings into the provider's free
time (SPEC.md §5, §7 — the "fill free time" half; nothing here ever moves or
cancels an existing appointment).

Solving the whole queue at once rather than one request at a time is not an
optimisation detail: fragmentation is a shared resource. Two requests landing
in the same free block interact — the cost of one depends on where the other
went — so per-request greedy placement cannot see the arrangement that keeps
the calendar packed.

Objective, lexicographic:

  1. maximise the number of requests placed (partial solutions are valid, §5)
  2. minimise  alpha * fragmentation/F  +  (1 - alpha) * earliness/E

Both terms are minutes, normalised by F and E so `alpha` mixes rather than
merely ranks (see CostConfig). Earliness is measured per request from that
request's own earliest feasible start: recurring clients should get their
slot as early as *they* can take it, so later bookings in the series don't
get pushed progressively further out.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from typing import Dict, List, Optional, Sequence, Tuple

from calendar_store import TimeSegment
from ortools.sat.python import cp_model

from .fragmentation import waste_minutes, waste_table
from .models import (
    BookingRequest,
    CostConfig,
    Placement,
    PlacementResult,
    TimeRange,
    _Candidate,
)

# CP-SAT needs integer objective coefficients. The two normalised weights sit
# roughly an order of magnitude apart, so this has to be large enough that
# rounding the smaller one doesn't quietly flatten it to zero.
_WEIGHT_PRECISION = 1_000_000


@dataclass(frozen=True)
class _Block:
    """A maximal run of provider free time, in whole grid cells.

    Cell ids are global (cells since `origin`), and blocks never touch, so a
    cell id identifies both the block and the position within it.
    """
    start_cell: int
    n_cells: int

    @property
    def end_cell(self) -> int:
        return self.start_cell + self.n_cells


def solve_placements(
    requests: Sequence[BookingRequest],
    provider_free: Sequence[TimeSegment],
    config: Optional[CostConfig] = None,
    *,
    time_limit_seconds: float = 10.0,
) -> PlacementResult:
    """Place as many of `requests` as will fit into `provider_free`.

    `provider_free` is provider availability with existing appointments
    already subtracted — calendar_store returns those separately and does not
    net them (see its INTERFACE.md "Guarantees"), so the repository adapter
    does that before handing a snapshot here.

    `time_limit_seconds` applies to each of the two solve phases (see `_solve`),
    so a fully saturated call can take twice it. On timeout the result is the
    best found rather than the proven best: if the first phase is cut short,
    the second optimises cost against a request count that may be one short of
    achievable, and says so nowhere. Raise the limit rather than treat a
    returned result as certified optimal.
    """
    config = config or CostConfig()
    grid = config.grid_minutes

    seen = set()
    for request in requests:
        if request.duration_minutes <= 0:
            raise ValueError(f"request {request.id}: duration must be positive")
        if request.duration_minutes % grid:
            raise ValueError(
                f"request {request.id}: duration {request.duration_minutes} is not "
                f"a multiple of grid_minutes {grid}"
            )
        # Results are reported by id, so duplicates would produce placements the
        # caller cannot tell apart and an `unplaced` list they cannot act on.
        if request.id in seen:
            raise ValueError(f"duplicate request id {request.id!r}")
        seen.add(request.id)

    origin = _origin(requests, provider_free)
    blocks = _blocks(provider_free, origin, grid)

    candidates: List[_Candidate] = []
    for index, request in enumerate(requests):
        candidates.extend(_candidates_for(request, index, blocks, origin, config))

    # Nothing to decide — but the calendar still has whatever waste it had, and
    # reporting 0 here would make the figure mean different things on different
    # paths.
    if not candidates:
        return PlacementResult(
            unplaced=tuple(r.id for r in requests),
            fragmentation_minutes=_idle_waste(blocks, config),
        )

    return _solve(requests, blocks, candidates, origin, config, time_limit_seconds)


def _idle_waste(blocks: Sequence[_Block], config: CostConfig) -> int:
    """Waste of a calendar with nothing placed in it."""
    return sum(
        waste_minutes(block.n_cells * config.grid_minutes, config.service_durations)
        for block in blocks
    )


# --------------------------------------------------------------------------
# Grid construction
# --------------------------------------------------------------------------


def _origin(
    requests: Sequence[BookingRequest], provider_free: Sequence[TimeSegment]
) -> datetime:
    """Anchor the grid to midnight of the earliest day in play, so cell
    boundaries land on whole clock times (09:00, 09:15, ...) rather than on
    whatever minute the first free segment happens to start at.
    """
    moments = [seg.start for seg in provider_free]
    moments.extend(window.start for r in requests for window in r.desired)
    earliest = min(moments) if moments else datetime.min
    return datetime.combine(earliest.date(), time.min)


def _minutes(moment: datetime, origin: datetime) -> int:
    return int((moment - origin).total_seconds() // 60)


def _inner_cells(
    start: datetime, end: datetime, origin: datetime, grid: int
) -> Tuple[int, int]:
    """The half-open cell range fully contained in [start, end).

    Snapped inward on both sides: a placement must sit entirely inside real
    free time, so a partial cell at either edge is unusable. The discarded
    sliver is under one grid cell and is simply not accounted for — it could
    never hold a booking anyway.
    """
    start_min = _minutes(start, origin)
    end_min = _minutes(end, origin)
    return -(-start_min // grid), end_min // grid


def _blocks(
    provider_free: Sequence[TimeSegment], origin: datetime, grid: int
) -> List[_Block]:
    """Free segments as grid-aligned blocks, with touching segments merged.

    Merging matters for cost, not tidiness: two back-to-back free segments are
    one usable gap, and scoring them separately would invent fragmentation
    that isn't there.
    """
    ranges: List[List[int]] = []
    for seg in sorted(provider_free, key=lambda s: s.start):
        start_cell, end_cell = _inner_cells(seg.start, seg.end, origin, grid)
        if end_cell <= start_cell:
            continue
        if ranges and start_cell <= ranges[-1][1]:
            ranges[-1][1] = max(ranges[-1][1], end_cell)
        else:
            ranges.append([start_cell, end_cell])
    return [_Block(start_cell=s, n_cells=e - s) for s, e in ranges]


def _candidates_for(
    request: BookingRequest,
    index: int,
    blocks: Sequence[_Block],
    origin: datetime,
    config: CostConfig,
) -> List[_Candidate]:
    """Every start time this request could legally take.

    Feasibility here is `desired ∩ provider free` only. The client's *general*
    availability from calendar_store is deliberately not consulted: it becomes
    relevant when asking whether an already-booked client would move, which
    this pass never does.
    """
    grid = config.grid_minutes
    span = request.duration_minutes // grid

    starts: List[int] = []
    for window in request.desired:
        want_start, want_end = _inner_cells(window.start, window.end, origin, grid)
        for block in blocks:
            # The appointment must fit inside one contiguous stretch of
            # desired-and-free time; it may not bridge a gap between two.
            low = max(want_start, block.start_cell)
            high = min(want_end, block.end_cell)
            starts.extend(range(low, high - span + 1))

    if not starts:
        return []

    starts = sorted(set(starts))
    earliest = starts[0]
    return [
        _Candidate(
            request_index=index,
            start_cell=start,
            cell_span=span,
            earliness_minutes=(start - earliest) * grid,
        )
        for start in starts
    ]


# --------------------------------------------------------------------------
# Model
# --------------------------------------------------------------------------


def _solve(
    requests: Sequence[BookingRequest],
    blocks: Sequence[_Block],
    candidates: Sequence[_Candidate],
    origin: datetime,
    config: CostConfig,
    time_limit_seconds: float,
) -> PlacementResult:
    """Two solves, not one.

    The objective is lexicographic — requests placed always beats cost — and
    the obvious encoding is a single objective with a big-M term. This is the
    same answer, and chosen mainly for robustness rather than speed: with
    big-M, the ordering is only correct if M genuinely exceeds every
    achievable cost, so a mis-derived bound silently buys a worse count for a
    better cost. Pinning the count makes that structural. Measured at the
    default grid it is also about 12% faster — worth having, but not the
    reason.
    """
    grid = config.grid_minutes
    reachable, fixed_waste = _reachable_blocks(blocks, candidates, config)

    model, chosen, by_request = _base_model(requests, candidates)
    model.Maximize(sum(chosen))
    solver = _solver(time_limit_seconds)
    _check(solver.Solve(model), solver)
    placeable = int(round(solver.ObjectiveValue()))

    model, chosen, by_request = _base_model(requests, candidates)
    model.Add(sum(chosen) == placeable)

    occupied = _occupancy(model, reachable, chosen, candidates)
    fragmentation = _fragmentation_term(model, reachable, occupied, config)
    earliness = sum(
        candidate.earliness_minutes * chosen[i] for i, candidate in enumerate(candidates)
    )

    # Normalisation folded into integer weights: cost = a*frag/F + (1-a)*earli/E.
    w_frag, w_earli = _weights(config)
    model.Minimize(w_frag * fragmentation + w_earli * earliness)

    solver = _solver(time_limit_seconds)
    _check(solver.Solve(model), solver)

    # At the extremes of the slider one term carries no weight at all — and not
    # only at exactly 0 or 1, since a small enough share rounds away entirely.
    # Every arrangement tying on the other term is then equally optimal, so the
    # solver returns whichever its workers reach first: a different answer on
    # each run of identical input. Pin what was achieved and spend the leftover
    # freedom on the term that was ignored, which makes the result both stable
    # and the sensible reading of the slider — "earliest among equally packed"
    # at alpha 1, "least wasteful among equally early" at alpha 0.
    if not w_frag or not w_earli:
        model.Add(
            w_frag * fragmentation + w_earli * earliness
            == int(round(solver.ObjectiveValue()))
        )
        model.Minimize(earliness if not w_earli else fragmentation)
        solver = _solver(time_limit_seconds)
        _check(solver.Solve(model), solver)

    placements = []
    placed_requests = set()
    for i, candidate in enumerate(candidates):
        if not solver.BooleanValue(chosen[i]):
            continue
        request = requests[candidate.request_index]
        start = origin + timedelta(minutes=candidate.start_cell * grid)
        placements.append(
            Placement(
                request_id=request.id,
                client_id=request.client_id,
                range=TimeRange(start, start + timedelta(minutes=request.duration_minutes)),
            )
        )
        placed_requests.add(candidate.request_index)

    placements.sort(key=lambda p: p.range.start)
    return PlacementResult(
        placements=tuple(placements),
        unplaced=tuple(
            r.id for i, r in enumerate(requests) if i not in placed_requests
        ),
        fragmentation_minutes=int(solver.Value(fragmentation)) + fixed_waste,
        earliness_minutes=int(solver.Value(earliness)),
    )


def _base_model(
    requests: Sequence[BookingRequest], candidates: Sequence[_Candidate]
) -> Tuple[cp_model.CpModel, List[cp_model.IntVar], Dict[int, List[int]]]:
    """Assignment only: one slot per request at most, and no two placements
    sharing a cell. Both solve phases start from this.
    """
    model = cp_model.CpModel()
    chosen = [model.NewBoolVar(f"x{i}") for i in range(len(candidates))]

    # Zero placements for a request is allowed, which is what makes a partial
    # solution expressible rather than infeasible (SPEC.md §5).
    by_request: Dict[int, List[int]] = {}
    for i, candidate in enumerate(candidates):
        by_request.setdefault(candidate.request_index, []).append(i)
    for indices in by_request.values():
        model.AddAtMostOne([chosen[i] for i in indices])

    for indices in _covering_cells(candidates).values():
        if len(indices) > 1:
            model.AddAtMostOne([chosen[i] for i in indices])

    _break_symmetry(model, requests, candidates, by_request, chosen)
    return model, chosen, by_request


def _covering_cells(candidates: Sequence[_Candidate]) -> Dict[int, List[int]]:
    """cell -> the candidates that would occupy it."""
    covering: Dict[int, List[int]] = {}
    for i, candidate in enumerate(candidates):
        for cell in range(candidate.start_cell, candidate.start_cell + candidate.cell_span):
            covering.setdefault(cell, []).append(i)
    return covering


def _break_symmetry(
    model: cp_model.CpModel,
    requests: Sequence[BookingRequest],
    candidates: Sequence[_Candidate],
    by_request: Dict[int, List[int]],
    chosen: Sequence[cp_model.IntVar],
) -> None:
    """Two requests wanting the same duration in the same windows are
    interchangeable: swapping them is a different assignment with an identical
    cost, and the solver would otherwise explore every permutation. Force them
    into start order, and make the later one placeable only if the earlier one
    is.
    """
    groups: Dict[Tuple, List[int]] = {}
    for index, request in enumerate(requests):
        key = (
            request.duration_minutes,
            tuple(sorted((w.start, w.end) for w in request.desired)),
        )
        groups.setdefault(key, []).append(index)

    for members in groups.values():
        members = [m for m in members if m in by_request]
        for earlier, later in zip(members, members[1:]):
            placed_later = sum(chosen[i] for i in by_request[later])
            model.Add(placed_later <= sum(chosen[i] for i in by_request[earlier]))
            model.Add(
                sum(candidates[i].start_cell * chosen[i] for i in by_request[earlier])
                <= sum(candidates[i].start_cell * chosen[i] for i in by_request[later])
            ).OnlyEnforceIf(_as_literal(model, placed_later))


def _as_literal(model: cp_model.CpModel, expression) -> cp_model.IntVar:
    literal = model.NewBoolVar("")
    model.Add(expression == literal)
    return literal


def _occupancy(
    model: cp_model.CpModel,
    blocks: Sequence[_Block],
    chosen: Sequence[cp_model.IntVar],
    candidates: Sequence[_Candidate],
) -> Dict[int, cp_model.IntVar]:
    covering = _covering_cells(candidates)
    occupied: Dict[int, cp_model.IntVar] = {}
    for block in blocks:
        for cell in range(block.start_cell, block.end_cell):
            var = model.NewBoolVar(f"occ{cell}")
            occupied[cell] = var
            model.Add(sum(chosen[i] for i in covering.get(cell, [])) == var)
    return occupied


def _reachable_blocks(
    blocks: Sequence[_Block], candidates: Sequence[_Candidate], config: CostConfig
) -> Tuple[List[_Block], int]:
    """Split the calendar into blocks the solver can actually change and the
    rest, whose waste is already decided.

    A block no candidate can reach stays empty whatever the solver does, so its
    waste is a constant — worth computing in Python rather than paying for a
    variable per cell.

    How much this saves depends entirely on how much of the horizon the queue's
    windows actually cover, so it is insurance rather than a reliable win: on a
    dense queue it pruned 1 block of 22, while on a queue of narrow windows over
    60 days it cut the model from 3710 variables to 430. Solve time barely moved
    in either case. What it buys is that model size tracks what the solver can
    actually decide, instead of growing with the length of the window.
    """
    touched = set()
    for candidate in candidates:
        touched.update(range(candidate.start_cell, candidate.start_cell + candidate.cell_span))

    reachable, fixed = [], 0
    for block in blocks:
        if touched.intersection(range(block.start_cell, block.end_cell)):
            reachable.append(block)
        else:
            fixed += waste_minutes(block.n_cells * config.grid_minutes, config.service_durations)
    return reachable, fixed


def _weights(config: CostConfig) -> Tuple[int, int]:
    """Integer stand-ins for `alpha/F` and `(1-alpha)/E`, reduced by their gcd
    — CP-SAT works better with small coefficients, and the ratio is all that
    matters.
    """
    w_frag = round(_WEIGHT_PRECISION * config.alpha / config.fragmentation_scale_minutes)
    w_earli = round(
        _WEIGHT_PRECISION * (1.0 - config.alpha) / config.earliness_scale_minutes
    )
    divisor = math.gcd(w_frag, w_earli) or 1
    return w_frag // divisor, w_earli // divisor


def _solver(time_limit_seconds: float) -> cp_model.CpSolver:
    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = time_limit_seconds
    solver.parameters.num_search_workers = 8
    return solver


def _check(status: int, solver: cp_model.CpSolver) -> None:
    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        raise RuntimeError(
            f"placement solve returned {solver.StatusName(status)} — expected at "
            "least a feasible solution, since placing nothing is always feasible"
        )


def _fragmentation_term(
    model: cp_model.CpModel,
    blocks: Sequence[_Block],
    occupied: Dict[int, cp_model.IntVar],
    config: CostConfig,
):
    """Total wasted minutes across every gap the placement leaves.

    Gap lengths aren't known until the solver has chosen, so they're built as
    variables: a run-length counter that resets on every occupied cell, read
    only at the cell where a run ends, and converted to wasted minutes through
    a precomputed table (see fragmentation.py).
    """
    grid = config.grid_minutes
    longest = max(block.n_cells for block in blocks)
    table = [
        cells * grid
        for cells in waste_table(longest, [d // grid for d in config.service_durations])
    ]
    worst = max(table)

    terms = []
    for block in blocks:
        run = 0  # cells of free time ending at the previous cell
        for offset in range(block.n_cells):
            cell = block.start_cell + offset
            is_free = occupied[cell].Not()

            length = model.NewIntVar(0, block.n_cells, f"len{cell}")
            model.Add(length == 0).OnlyEnforceIf(occupied[cell])
            model.Add(length == run + 1).OnlyEnforceIf(is_free)
            run = length

            # The run ends here if this cell is free and the next one isn't
            # (or there is no next one — a block boundary always ends a run).
            ends_here = model.NewBoolVar(f"end{cell}")
            if offset == block.n_cells - 1:
                model.Add(ends_here == 1).OnlyEnforceIf(is_free)
                model.Add(ends_here == 0).OnlyEnforceIf(occupied[cell])
            else:
                nxt = occupied[cell + 1]
                model.AddBoolAnd([is_free, nxt]).OnlyEnforceIf(ends_here)
                model.AddBoolOr([occupied[cell], nxt.Not()]).OnlyEnforceIf(ends_here.Not())

            wasted = model.NewIntVar(0, worst, f"waste{cell}")
            model.AddElement(length, table, wasted)

            counted = model.NewIntVar(0, worst, f"frag{cell}")
            model.Add(counted == wasted).OnlyEnforceIf(ends_here)
            model.Add(counted == 0).OnlyEnforceIf(ends_here.Not())
            terms.append(counted)

    total = model.NewIntVar(0, sum(b.n_cells for b in blocks) * grid, "fragmentation")
    model.Add(total == sum(terms))
    return total
