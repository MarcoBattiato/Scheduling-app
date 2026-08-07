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
  2. minimise  alpha * fragmentation/F  +  (1 - alpha) * preference_gap/E

Both terms are minutes, normalised by F and E so `alpha` mixes rather than
merely ranks (see CostConfig).

The preference gap is how far everything ended up from what its client asked
for. A request states a preferred slot; a booking being moved carries the one
it was booked against. Where no preference is stated the earliest feasible
slot stands in, which is exactly "book as early as possible".

That both sides are measured the same way is the point. When the *ask* was the
hard constraint, naming a single slot made a request impossible to place
elsewhere while a settled booking could be relocated anywhere in its owner's
much wider availability — so a narrow ask unseated a settled client almost for
free. Availability now bounds both, and preference costs both.
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
    Displacement,
    MovableAppointment,
    Placement,
    PlacementResult,
    TimeRange,
    _Candidate,
    _Move,
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
    movable: Sequence[MovableAppointment] = (),
    max_displacements: int = 0,
    allow_chains: bool = False,
    time_limit_seconds: float = 10.0,
) -> PlacementResult:
    """Place as many of `requests` as will fit into `provider_free`.

    `provider_free` is provider availability with existing appointments
    already subtracted — calendar_store returns those separately and does not
    net them (see its INTERFACE.md "Guarantees"), so the repository adapter
    does that before handing a snapshot here. That includes the appointments
    passed as `movable`: their current slots come back into play here, because
    the space is usable only if its occupant agrees to move.

    Displacement is off by default (`max_displacements=0`), and is a last
    resort when on: the objective maximises requests placed first, then
    minimises how many accepted bookings had to move, so nobody is disturbed
    to buy something the calendar could have fitted anyway. `max_displacements`
    is a hard ceiling on top of that — both a business limit and, since it
    prunes hard, what keeps the solve fast.

    `allow_chains=False` forbids a displaced booking from moving into a slot
    another displaced booking is vacating, so every new placement depends on
    at most one client agreeing rather than a sequence of them. Chains cost
    5-10x solve time and bought nothing measurable; the flag exists because
    the strongest argument against them (compounding refusal risk, and
    SPEC.md §7.4's independent application of confirmed moves) belongs to
    negotiation, which is not modelled yet.

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

    movable = list(movable) if max_displacements > 0 else []
    for appointment in movable:
        if appointment.range.end <= appointment.range.start:
            raise ValueError(f"appointment {appointment.id}: empty or reversed range")

    origin = _origin(requests, provider_free, movable)
    free_blocks = _blocks(provider_free, origin, grid)

    # A movable booking's slot is space the solver may hand to someone else, so
    # it belongs in the domain requests are placed into — unlike a booking that
    # cannot move, which stays subtracted.
    movable, blocks, candidates = _with_movable(
        requests, provider_free, movable, origin, config, allow_chains
    )
    moves = _moves_for(movable, blocks, free_blocks, origin, config, allow_chains)

    # Nothing to decide — but the calendar still has whatever waste it had, and
    # reporting 0 here would make the figure mean different things on different
    # paths.
    if not candidates:
        return PlacementResult(
            unplaced=tuple(r.id for r in requests),
            fragmentation_minutes=_idle_waste(blocks, config),
        )

    return _solve(
        requests, movable, blocks, candidates, moves, origin, config,
        max_displacements, time_limit_seconds,
    )


def _with_movable(
    requests: Sequence[BookingRequest],
    provider_free: Sequence[TimeSegment],
    movable: Sequence[MovableAppointment],
    origin: datetime,
    config: CostConfig,
    allow_chains: bool,
) -> Tuple[List[MovableAppointment], List[_Block], List[_Candidate]]:
    """Work out which bookings are worth offering up, and the domain that
    leaves.

    Vacating a slot no request could use helps nobody, so with chains
    forbidden it is enough to keep the bookings whose slot overlaps somewhere a
    request actually wants — and that filter is exact, not a heuristic, because
    a displaced booking may only move into space that is already free. Dropped
    bookings stay put, so their slots leave the domain again.
    """
    grid = config.grid_minutes

    def domain_for(keep: Sequence[MovableAppointment]) -> List[_Block]:
        spans = list(provider_free) + [m.range for m in keep]
        return _blocks(spans, origin, grid)

    def candidates_for(blocks: Sequence[_Block]) -> List[_Candidate]:
        found: List[_Candidate] = []
        for index, request in enumerate(requests):
            found.extend(_candidates_for(request, index, blocks, origin, config))
        return found

    blocks = domain_for(movable)
    candidates = candidates_for(blocks)
    if not movable or allow_chains:
        return list(movable), blocks, candidates

    wanted = set()
    for candidate in candidates:
        wanted.update(range(candidate.start_cell, candidate.start_cell + candidate.cell_span))

    keep = [
        m for m in movable
        if wanted.intersection(range(*_inner_cells(m.range.start, m.range.end, origin, grid)))
    ]
    if len(keep) == len(movable):
        return keep, blocks, candidates

    # Re-derive against the smaller domain. This settles in one step: a request
    # whose slot needed a dropped booking's cells would have kept that booking.
    blocks = domain_for(keep)
    return keep, blocks, candidates_for(blocks)


def _moves_for(
    movable: Sequence[MovableAppointment],
    blocks: Sequence[_Block],
    free_blocks: Sequence[_Block],
    origin: datetime,
    config: CostConfig,
    allow_chains: bool,
) -> List[_Move]:
    """Where each movable booking could go instead of where it is.

    With chains forbidden the targets are drawn from time that is free right
    now, not from the wider domain — so no move depends on another move
    happening first.
    """
    grid = config.grid_minutes
    targets = blocks if allow_chains else free_blocks

    moves: List[_Move] = []
    for index, appointment in enumerate(movable):
        start_cell, end_cell = _inner_cells(
            appointment.range.start, appointment.range.end, origin, grid
        )
        span = end_cell - start_cell
        if span <= 0:
            continue
        starts = set()
        for window in appointment.allowed:
            want_start, want_end = _inner_cells(window.start, window.end, origin, grid)
            for block in targets:
                low = max(want_start, block.start_cell)
                high = min(want_end, block.end_cell)
                starts.update(range(low, high - span + 1))
        starts.discard(start_cell)
        # Measured from what the client asked for, not from wherever they were
        # last put — so a booking already displaced can be moved back towards
        # its preferred slot for free, rather than being charged for it.
        wanted = (_nearest_cell(appointment.preferred_start, origin, grid)
                  if appointment.preferred_start else start_cell)
        moves.extend(
            _Move(
                movable_index=index,
                start_cell=start,
                cell_span=span,
                gap_minutes=abs(start - wanted) * grid,
                shift_minutes=abs(start - start_cell) * grid,
            )
            for start in sorted(starts)
        )
    return moves


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
    requests: Sequence[BookingRequest],
    provider_free: Sequence[TimeSegment],
    movable: Sequence[MovableAppointment] = (),
) -> datetime:
    """Anchor the grid to midnight of the earliest day in play, so cell
    boundaries land on whole clock times (09:00, 09:15, ...) rather than on
    whatever minute the first free segment happens to start at.
    """
    moments = [seg.start for seg in provider_free]
    moments.extend(window.start for r in requests for window in r.desired)
    moments.extend(m.range.start for m in movable)
    moments.extend(w.start for m in movable for w in m.allowed)
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
    # With no stated preference the earliest feasible slot *is* the preference,
    # which reproduces "book as early as possible" exactly.
    wanted = (_nearest_cell(request.preferred_start, origin, grid)
              if request.preferred_start else starts[0])
    return [
        _Candidate(
            request_index=index,
            start_cell=start,
            cell_span=span,
            gap_minutes=abs(start - wanted) * grid,
        )
        for start in starts
    ]


def _nearest_cell(moment: datetime, origin: datetime, grid: int) -> int:
    """The grid cell a wished-for time falls in. Rounded rather than snapped
    inward: a preference is a target, not a boundary."""
    return int(round(_minutes(moment, origin) / grid))


# --------------------------------------------------------------------------
# Model
# --------------------------------------------------------------------------


def _solve(
    requests: Sequence[BookingRequest],
    movable: Sequence[MovableAppointment],
    blocks: Sequence[_Block],
    candidates: Sequence[_Candidate],
    moves: Sequence[_Move],
    origin: datetime,
    config: CostConfig,
    max_displacements: int,
    time_limit_seconds: float,
) -> PlacementResult:
    """Three solves, not one.

    The objective is lexicographic — requests placed always beats cost — and
    the obvious encoding is a single objective with a big-M term. This is the
    same answer, and chosen mainly for robustness rather than speed: with
    big-M, the ordering is only correct if M genuinely exceeds every
    achievable cost, so a mis-derived bound silently buys a worse count for a
    better cost. Pinning the count makes that structural. Measured at the
    default grid it is also about 12% faster — worth having, but not the
    reason.

    Displacement adds a tier between the two: among the arrangements that place
    the most requests, prefer the one disturbing the fewest accepted bookings.
    That ordering is what makes rescheduling a last resort rather than merely
    another option — if the calendar could have fitted a request without moving
    anyone, that solution wins outright.
    """
    grid = config.grid_minutes

    # Cells the solver can do anything with: somewhere a request could land, a
    # booking could move to, or a booking currently sits. Anything else is
    # settled before the solve starts.
    touched = set()
    for candidate in candidates:
        touched.update(range(candidate.start_cell, candidate.start_cell + candidate.cell_span))
    for move in moves:
        touched.update(range(move.start_cell, move.start_cell + move.cell_span))
    for appointment in movable:
        touched.update(range(*_inner_cells(
            appointment.range.start, appointment.range.end, origin, grid
        )))
    reachable, fixed_waste = _reachable_blocks(blocks, touched, config)

    def build():
        return _base_model(
            requests, candidates, movable, moves, max_displacements, origin, grid
        )

    model, chosen, kept, moved, _ = build()
    model.Maximize(sum(chosen))
    solver = _solver(time_limit_seconds)
    _check(solver.Solve(model), solver)
    placeable = int(round(solver.ObjectiveValue()))

    displaced = 0
    if moves:
        model, chosen, kept, moved, _ = build()
        model.Add(sum(chosen) == placeable)
        model.Minimize(sum(moved))
        solver = _solver(time_limit_seconds)
        _check(solver.Solve(model), solver)
        displaced = int(round(solver.ObjectiveValue()))

    model, chosen, kept, moved, covering = build()
    model.Add(sum(chosen) == placeable)
    if moves:
        model.Add(sum(moved) == displaced)

    occupied = _occupancy(model, reachable, covering)
    fragmentation = _fragmentation_term(model, reachable, occupied, config)
    # One term, not two: how far everything ended up from what its client
    # asked for. A request with no stated preference is measured from the
    # earliest slot it could have had, a booking being moved from the slot its
    # owner originally wanted. Weighting the two alike is provisional; SPEC.md
    # §10 owns the real model.
    preference_gap = sum(
        candidate.gap_minutes * chosen[i] for i, candidate in enumerate(candidates)
    ) + sum(move.gap_minutes * moved[i] for i, move in enumerate(moves))

    # Normalisation folded into integer weights: cost = a*frag/F + (1-a)*earli/E.
    w_frag, w_earli = _weights(config)
    model.Minimize(w_frag * fragmentation + w_earli * preference_gap)

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
            w_frag * fragmentation + w_earli * preference_gap
            == int(round(solver.ObjectiveValue()))
        )
        model.Minimize(preference_gap if not w_earli else fragmentation)
        solver = _solver(time_limit_seconds)
        _check(solver.Solve(model), solver)

    # Which cells are only free because their occupant agreed to move. Built
    # from the chosen assignment rather than inferred downstream, so it stays
    # right when chains are allowed and a placement can rest on a sequence of
    # moves that overlap alone would not reveal.
    vacated: Dict[int, str] = {}
    for i, move in enumerate(moves):
        if not solver.BooleanValue(moved[i]):
            continue
        appointment = movable[move.movable_index]
        start, end = _inner_cells(
            appointment.range.start, appointment.range.end, origin, grid
        )
        for cell in range(start, end):
            vacated[cell] = appointment.id

    def _needs(cells, exclude=None):
        found = {vacated[c] for c in cells if c in vacated}
        found.discard(exclude)
        return tuple(sorted(found))

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
                depends_on=_needs(
                    range(candidate.start_cell, candidate.start_cell + candidate.cell_span)
                ),
            )
        )
        placed_requests.add(candidate.request_index)

    displacements = []
    for i, move in enumerate(moves):
        if not solver.BooleanValue(moved[i]):
            continue
        appointment = movable[move.movable_index]
        start = origin + timedelta(minutes=move.start_cell * grid)
        displacements.append(
            Displacement(
                appointment_id=appointment.id,
                client_id=appointment.client_id,
                was=appointment.range,
                now=TimeRange(start, start + (appointment.range.end - appointment.range.start)),
                depends_on=_needs(
                    range(move.start_cell, move.start_cell + move.cell_span),
                    exclude=appointment.id,
                ),
            )
        )

    placements.sort(key=lambda p: p.range.start)
    displacements.sort(key=lambda d: d.was.start)
    return PlacementResult(
        placements=tuple(placements),
        unplaced=tuple(
            r.id for i, r in enumerate(requests) if i not in placed_requests
        ),
        displacements=tuple(displacements),
        fragmentation_minutes=int(solver.Value(fragmentation)) + fixed_waste,
        preference_gap_minutes=int(solver.Value(preference_gap)),
    )


def _base_model(
    requests: Sequence[BookingRequest],
    candidates: Sequence[_Candidate],
    movable: Sequence[MovableAppointment],
    moves: Sequence[_Move],
    max_displacements: int,
    origin: datetime,
    grid: int,
):
    """Assignment only: at most one slot per request, exactly one per accepted
    booking, and no two of anything sharing a cell. Every solve phase starts
    from this.

    The asymmetry between the two is the point. A request may end up with no
    slot at all, which is what makes a partial solution expressible rather than
    infeasible (SPEC.md §5). An accepted booking must land somewhere — staying
    put counts — because the solver is never allowed to cancel a third party
    to make room (SPEC.md §7.2).
    """
    model = cp_model.CpModel()
    chosen = [model.NewBoolVar(f"x{i}") for i in range(len(candidates))]
    moved = [model.NewBoolVar(f"y{i}") for i in range(len(moves))]
    kept = [model.NewBoolVar(f"stay{i}") for i in range(len(movable))]

    by_request: Dict[int, List[int]] = {}
    for i, candidate in enumerate(candidates):
        by_request.setdefault(candidate.request_index, []).append(i)
    for indices in by_request.values():
        model.AddAtMostOne([chosen[i] for i in indices])

    by_movable: Dict[int, List[int]] = {}
    for i, move in enumerate(moves):
        by_movable.setdefault(move.movable_index, []).append(i)
    for index in range(len(movable)):
        model.AddExactlyOne(
            [kept[index]] + [moved[i] for i in by_movable.get(index, [])]
        )

    if moves:
        model.Add(sum(moved) <= max_displacements)

    covering = _covering(candidates, chosen, moves, moved, movable, kept, origin, grid)
    for literals in covering.values():
        if len(literals) > 1:
            model.AddAtMostOne(literals)

    _break_symmetry(model, requests, candidates, by_request, chosen)
    return model, chosen, kept, moved, covering


def _covering(
    candidates: Sequence[_Candidate],
    chosen: Sequence,
    moves: Sequence[_Move],
    moved: Sequence,
    movable: Sequence[MovableAppointment],
    kept: Sequence,
    origin: datetime,
    grid: int,
) -> Dict[int, List]:
    """cell -> the literals whose being true would occupy it.

    A booking that stays occupies its current cells, so `kept` appears here
    exactly as a placement does. That is what stops a request being dropped on
    top of a booking nobody agreed to move.
    """
    covering: Dict[int, List] = {}

    def mark(cells, literal):
        for cell in cells:
            covering.setdefault(cell, []).append(literal)

    for i, candidate in enumerate(candidates):
        mark(range(candidate.start_cell, candidate.start_cell + candidate.cell_span),
             chosen[i])
    for i, move in enumerate(moves):
        mark(range(move.start_cell, move.start_cell + move.cell_span), moved[i])
    for index, appointment in enumerate(movable):
        start, end = _inner_cells(
            appointment.range.start, appointment.range.end, origin, grid
        )
        mark(range(start, end), kept[index])
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
    covering: Dict[int, List],
) -> Dict[int, cp_model.IntVar]:
    occupied: Dict[int, cp_model.IntVar] = {}
    for block in blocks:
        for cell in range(block.start_cell, block.end_cell):
            var = model.NewBoolVar(f"occ{cell}")
            occupied[cell] = var
            model.Add(sum(covering.get(cell, [])) == var)
    return occupied


def _reachable_blocks(
    blocks: Sequence[_Block],
    touched: Sequence[int],
    config: CostConfig,
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
    touched = set(touched)
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
    wasteful = [n for n in range(1, longest + 1) if table[n]]

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

            # Only a few run lengths waste anything — with a 60/90 catalogue
            # on a 30-minute grid, exactly one does (a single isolated cell).
            # Naming those directly is far cheaper to presolve than an
            # AddElement over the whole table, and the table is mostly zeros.
            for run_cells in wasteful:
                exact = model.NewBoolVar(f"len{cell}_{run_cells}")
                model.Add(length == run_cells).OnlyEnforceIf(exact)
                model.Add(length != run_cells).OnlyEnforceIf(exact.Not())
                both = model.NewBoolVar(f"waste{cell}_{run_cells}")
                model.AddBoolAnd([exact, ends_here]).OnlyEnforceIf(both)
                model.AddBoolOr([exact.Not(), ends_here.Not()]).OnlyEnforceIf(both.Not())
                terms.append((table[run_cells], both))

    total = model.NewIntVar(0, sum(b.n_cells for b in blocks) * grid, "fragmentation")
    model.Add(total == sum(cost * flag for cost, flag in terms))
    return total
