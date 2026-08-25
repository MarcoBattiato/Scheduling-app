"""Identifiers, spans, and the enumerations the rest of the contract is built
from.

These types cross every boundary in the system, so they are deliberately inert:
data with no behaviour, no validation that needs a database, and no imports
beyond the standard library.

On `TimeSpan` and structural compatibility: `calendar_store.TimeSegment`,
`scheduling_engine.TimeSegment` and this type are all `(start, end)` and none of
them import each other. That is not duplication to be tidied away later — it is
what lets three packages agree on a shape without agreeing on a dependency.
Anything that only *reads* a span should accept any object carrying `.start` and
`.end` rather than demanding a particular class.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Optional

ClientId = str
AppointmentId = int
RequestId = int
ApprovalId = int
PlanId = int
ServiceId = str


@dataclass(frozen=True)
class TimeSpan:
    start: datetime
    end: datetime

    @property
    def minutes(self) -> int:
        return int((self.end - self.start).total_seconds() // 60)


class Role(Enum):
    """Who is acting. Not a job title — a trust level.

    PLATFORM exists so that resource limits (SPEC: `optimiser.set_bounds`) can
    be withheld from the provider without inventing a second admin concept
    later. It is the operator of the deployment, not a member of the practice.
    """
    PROVIDER = "provider"
    CLIENT = "client"
    PLATFORM = "platform"
    SYSTEM = "system"


class Tier(Enum):
    """How much ceremony a command needs, independent of which resource it
    touches. See CONTROL_SURFACE.md §3.
    """
    READ = "R"
    ROUTINE = "W"
    CONSEQUENTIAL = "C"
    AUTHORITY = "A"
    DESTRUCTIVE = "X"
    FINANCIAL = "$"


class TargetKind(Enum):
    """What a command points at, and therefore how its owner is found.

    The gate resolves a target to a `ClientId` before consulting the policy —
    which is why the policy itself stays pure and exhaustively testable.
    NONE means practice-wide: there is no client to own it.
    """
    NONE = "none"
    CLIENT = "client"
    APPOINTMENT = "appointment"
    REQUEST = "request"
    APPROVAL = "approval"
    PLAN = "plan"


class Access(Enum):
    """What a role may do with a command."""
    DENIED = "denied"
    OWN = "own"      # only where the target belongs to this principal
    ANY = "any"      # anywhere in the practice


class Party(Enum):
    """Who performed an action, where that distinction is itself evidence.

    Mirrors `calendar_store.Party` structurally and deliberately does not import
    it. A client dropping a slot says something about that slot; the provider
    closing a day says nothing about the client.
    """
    CLIENT = "client"
    PROVIDER = "provider"


class Origin(Enum):
    """Whether a slot was chosen or imposed.

    The single most important field to get right in the whole system: anything
    learning a client's habits from history must not read the scheduler's own
    rescheduling as a preference.
    """
    CLIENT = "client"
    DISPLACED = "displaced"


@dataclass(frozen=True)
class Target:
    """A resolved pointer: what kind of thing, which one, and who owns it.

    `owner` is None for practice-wide commands and for a target that could not
    be found — the gate must not distinguish those two to a caller, or denial
    becomes an existence oracle.
    """
    kind: TargetKind
    ident: Optional[object] = None
    owner: Optional[ClientId] = None


NO_TARGET = Target(TargetKind.NONE)


@dataclass(frozen=True)
class BudgetBounds:
    """The limits a provider's optimiser settings must fall inside.

    Compute is shared, and the horizon multiplies candidate slots for every
    request in the queue, so an innocent-looking `horizon_days` is the one
    setting that can make the practice unusable. The provider sets the value;
    the platform sets the bound. Both the standing default and any per-run
    override are clamped to the same numbers — an override is not an escape
    hatch.
    """
    max_horizon_days: int = 31
    max_displacements_ceiling: int = 5
    min_grid_minutes: int = 15
    max_time_limit_seconds: float = 30.0
    allow_chains_permitted: bool = False

    def clamp_horizon(self, days: int) -> int:
        return max(1, min(days, self.max_horizon_days))

    def clamp_displacements(self, n: int) -> int:
        return max(0, min(n, self.max_displacements_ceiling))
