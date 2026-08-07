"""Placement-side types. See SPEC.md §2 for the wider domain model; this module
carries only what the batch placement solver (§7) reads and returns.

`TimeRange` is deliberately not `calendar_store.TimeSegment` despite the
identical shape — see SPEC.md §2. Availability flows *in* as `TimeSegment`;
request windows and resulting placements are this package's own concern.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional, Sequence, Tuple

ClientId = str
RequestId = str

# The provider's service catalogue, in minutes. Gap usability is measured
# against this (see fragmentation.py): a leftover gap is only worth keeping if
# some combination of real services still fits in it.
DEFAULT_SERVICE_DURATIONS = (60, 90)


@dataclass(frozen=True)
class TimeRange:
    start: datetime
    end: datetime


@dataclass(frozen=True)
class RescheduleBounds:
    """How far an *involuntary* move may shift an appointment (SPEC.md §4).
    Not read by the placement solver below — placement only ever fills free
    time — but carried here because per-appointment overrides live on this
    side of the boundary rather than on `calendar_store.Appointment`, which
    has no concept of negotiation.
    """
    max_days_earlier: int
    max_days_later: int


@dataclass(frozen=True)
class BookingRequest:
    """A queued, not-yet-placed booking.

    `desired` is where this client may be booked *at all* — normally their
    availability, cropped to the horizon. It is a hard constraint.

    `preferred_start` is where they would *like* to be, and is only a cost.
    Keeping the two apart matters: when the ask itself was the constraint, a
    client could name a single slot and thereby become impossible to place
    anywhere else, while an existing booking could be relocated anywhere in its
    owner's much wider availability. Narrow asks therefore unseated settled
    bookings almost for free. Now both sides are bounded by availability and
    both pay for being moved away from what they wanted.

    With no preference stated, the earliest feasible slot is taken as the
    preference, which is exactly the old "book as early as possible".
    """
    id: RequestId
    client_id: ClientId
    duration_minutes: int
    desired: Sequence[TimeRange]
    preferred_start: Optional[datetime] = None


@dataclass(frozen=True)
class CostConfig:
    """Tuning for the placement objective (SPEC.md §10 is still deferred for
    the wider disruption cost; this covers placement into free time only).

    `alpha` is the provider-facing slider: 1.0 packs the calendar tightly
    (fragmentation dominates), 0.0 books everyone as early as they can go.
    The two terms are normalised first so `alpha` is a genuine mixing weight
    rather than a de-facto priority order — raw fragmentation is minutes-to-
    hours while earliness is routinely days.
    """
    alpha: float = 0.5
    # Placement resolution. The default is the catalogue's own granularity
    # (gcd(60, 90) = 30) on purpose: a finer grid only adds start times that
    # sit off the service lattice, and with a 60/90 catalogue those can only
    # ever split free time into pieces too small to sell. Measured on a 30-day
    # horizon it roughly doubles the candidate count and quadruples solve time
    # to reach the same answer. Set it finer only if the provider genuinely
    # wants to offer, say, :15 starts.
    grid_minutes: int = 30
    service_durations: Sequence[int] = DEFAULT_SERVICE_DURATIONS
    # Divisors that put both terms on a comparable scale. Read as "one
    # service-length of waste" and "one day of delay" respectively.
    fragmentation_scale_minutes: int = 60
    earliness_scale_minutes: int = 1440

    def __post_init__(self) -> None:
        if not 0.0 <= self.alpha <= 1.0:
            raise ValueError(f"alpha must be in [0, 1], got {self.alpha}")
        if self.grid_minutes <= 0:
            raise ValueError("grid_minutes must be positive")
        for d in self.service_durations:
            if d % self.grid_minutes:
                raise ValueError(
                    f"service duration {d} is not a multiple of grid_minutes "
                    f"{self.grid_minutes}"
                )


@dataclass(frozen=True)
class MovableAppointment:
    """An already-accepted booking the solver is allowed to relocate.

    Only ever offered as a last resort — see `solve_placements`. `allowed` is
    where this booking may go instead: the client's own availability cropped by
    their reschedule bounds, computed by the caller (see
    `availability.reschedule_windows`). Staying put is always legal and is not
    part of `allowed`, since a client's recorded availability may have drifted
    since they booked.

    Appointments that must not move — `locked`, or too close to now to give
    fair notice (SPEC.md §4) — are simply not passed in.
    """
    id: str
    client_id: ClientId
    range: TimeRange
    allowed: Sequence[TimeRange] = ()
    # What the client asked for when this was booked, carried forward so a
    # later move is measured against their wish rather than against wherever
    # they were last put. A booking already displaced away from its preferred
    # slot can therefore be moved *back* towards it at no cost.
    preferred_start: Optional[datetime] = None


@dataclass(frozen=True)
class Displacement:
    """An already-accepted booking the solver wants to move, and where to.

    A proposal, not a fact: obtaining the client's agreement is a separate
    step this engine does not yet model.
    """
    appointment_id: str
    client_id: ClientId
    was: TimeRange
    now: TimeRange
    # Appointments whose own move must happen first, because this one lands
    # in space they are vacating. Empty unless chains are permitted.
    depends_on: Tuple[str, ...] = ()

    @property
    def shift_minutes(self) -> int:
        """How far the booking moves, in minutes, in either direction."""
        return abs(int((self.now.start - self.was.start).total_seconds() // 60))


@dataclass(frozen=True)
class Placement:
    request_id: RequestId
    client_id: ClientId
    range: TimeRange
    # Appointments that must move before this booking can be made, because it
    # occupies space they currently hold. Direct dependencies only: if one of
    # those moves has dependencies of its own, walk them.
    #
    # Reported rather than left to be inferred. A consumer could guess by
    # comparing this range against each displacement's old one, but that is
    # only right while chains are forbidden — with chains a placement can rest
    # on a sequence of moves that overlap tells you nothing about.
    depends_on: Tuple[str, ...] = ()


@dataclass(frozen=True)
class PlacementResult:
    """A partial solution is a valid outcome (SPEC.md §5) — `unplaced` being
    non-empty is not an error.
    """
    placements: Sequence[Placement] = field(default_factory=tuple)
    unplaced: Sequence[RequestId] = field(default_factory=tuple)
    # Already-accepted bookings this solution wants to move. Empty unless
    # displacement was both permitted and necessary.
    displacements: Sequence[Displacement] = field(default_factory=tuple)
    # Reported raw (unnormalised, in minutes) so the numbers stay legible when
    # tuning `alpha`.
    fragmentation_minutes: int = 0
    # Total distance between where things landed and where they were wanted —
    # for a request, from its preferred slot (or the earliest feasible one if
    # it named none); for a moved booking, from the slot its owner asked for.
    preference_gap_minutes: int = 0

    @property
    def all_placed(self) -> bool:
        return not self.unplaced

    @property
    def shift_minutes(self) -> int:
        return sum(d.shift_minutes for d in self.displacements)


@dataclass(frozen=True)
class _Candidate:
    """One (request, start time) option the solver may choose."""
    request_index: int
    start_cell: int
    cell_span: int
    gap_minutes: int          # distance from what this client asked for


@dataclass(frozen=True)
class _Move:
    """One (movable appointment, new start time) option. Staying put is not a
    `_Move` — it is a separate literal, so "unchanged" is always available.
    """
    movable_index: int
    start_cell: int
    cell_span: int
    gap_minutes: int          # distance from what this client asked for
    shift_minutes: int        # distance actually travelled, for reporting
