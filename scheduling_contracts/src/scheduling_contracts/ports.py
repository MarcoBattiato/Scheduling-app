"""The interfaces the rest of the system is written against.

Each port is declared by the *consumer* that needs it, not by the package that
happens to satisfy it. The domain says "I need a window of availability"; an
adapter maps `calendar_store` onto that. The store never learns the domain
exists. Get these arrows backwards and the coupling has been renamed rather than
removed.

Protocols rather than base classes on purpose: an implementation conforms by
having the right shape, so no package has to import this one to satisfy it.
A conformance suite decides whether it really does.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Mapping, Optional, Protocol, Sequence, Tuple

from .decisions import Decision
from .principals import Principal, Session
from .values import (
    AppointmentId,
    BudgetBounds,
    ClientId,
    Origin,
    Party,
    Target,
    TimeSpan,
)


# ----------------------------------------------------------- outward surface

class ControlSurface(Protocol):
    """The single entrance. A GUI and a chatbot are peers in front of it, and
    neither has a private path past it.
    """

    def execute(self, session: Session, command: Any) -> Any:
        """Authorise and run a command. Raises rather than returning an error
        object when the caller had no business asking.
        """
        ...

    def draft(self, session: Session, command: Any) -> "Draft":
        """Authorise a command and describe it without running it."""
        ...

    def confirm(self, session: Session, token: str) -> Any:
        """Run a previously drafted command.

        Permissions are re-checked here rather than trusted from the draft: the
        world may have moved, and the token proves intent, not entitlement.
        """
        ...


class Draft(Protocol):
    """What `draft` returns: a summary a human can act on, and a token bound to
    the exact command.

    The token must commit to a hash of the payload. Without that, an assistant
    can have a harmless draft approved and submit a different one — the
    confused-deputy problem, and the realistic attack on a model-driven caller.
    """
    token: str
    summary: str
    payload_hash: str


class AuthorizationPolicy(Protocol):
    def decide(self, session: Session, command: Any,
               target: Optional[Target] = None) -> Decision: ...


class AuditSink(Protocol):
    """Every command, permitted or refused.

    `on_behalf_of` is what distinguishes an assistant's action from its human's,
    which is the question anyone investigating an incident asks first.
    """

    def record(self, *, session: Session, command_name: str,
               decision: Decision, outcome: str,
               target: Optional[Target] = None) -> None: ...


# --------------------------------------------------------------- the domain

class CalendarRepository(Protocol):
    """What the domain needs of storage — not what a store happens to offer.

    `window` takes the clients to include, which is where per-actor scoping
    attaches: a client's session can only ever produce a snapshot containing
    themselves, so an over-broad query cannot be the leak.
    """

    def window(self, span: TimeSpan,
               clients: Sequence[ClientId]) -> "Snapshot": ...

    def owner_of(self, target: Target) -> Optional[ClientId]:
        """Resolve a target to the client who owns it, or None if there is no
        such thing. The gate calls this before the policy, so that the policy
        can stay pure.
        """
        ...

    def book(self, *, client_id: ClientId, service_id: str, span: TimeSpan,
             origin: Origin, preferred_start: Optional[datetime] = None,
             locked: bool = False) -> AppointmentId: ...

    def cancel(self, appointment_id: AppointmentId, *, by: Party) -> None: ...

    def reschedule(self, appointment_id: AppointmentId, *, to: TimeSpan,
                   origin: Origin) -> AppointmentId: ...


class Snapshot(Protocol):
    """A bounded, already-scoped view of the calendar.

    Availability is exposed already netted of appointments, because every
    consumer wants free time and exactly one place should know how to compute
    it.
    """
    span: TimeSpan
    provider_free: Sequence[Any]                    # objects with .start/.end
    client_availability: Mapping[ClientId, Sequence[Any]]
    appointments: Sequence[Any]


class SchedulerPort(Protocol):
    """What the domain asks of an optimiser.

    One call, and no CP-SAT anywhere in the signature. Changing the objective,
    or replacing the solver outright, becomes an adapter change that nothing
    else has to notice.
    """

    def solve(self, problem: "SchedulingProblem") -> "SchedulingSolution": ...

    def bounds(self) -> BudgetBounds:
        """What this implementation will accept. The gate clamps to it, so a
        solver that cannot survive a 90-day horizon can say so rather than be
        handed one.
        """
        ...


class SchedulingProblem(Protocol):
    snapshot: Snapshot
    requests: Sequence[Any]
    movable: Sequence[Any]
    alpha: float
    horizon_days: int
    max_displacements: int
    allow_chains: bool
    time_limit_seconds: float


class SchedulingSolution(Protocol):
    placements: Sequence[Any]
    displacements: Sequence[Any]
    unplaced: Sequence[Any]
    metrics: Mapping[str, Any]


# ------------------------------------------------------------ not yet needed

class NotificationPort(Protocol):
    """Delivery is somebody else's problem — the domain only says what happened
    and to whom.
    """

    def notify(self, *, client_id: ClientId, subject: str, body: str) -> None: ...


class KeyVault(Protocol):
    """One key per client, so erasure is the destruction of a key rather than
    the deletion of rows.

    The design deliberately never deletes: cancelled and superseded rows are the
    evidence habitual-slot anchoring will be judged against. Crypto-shredding is
    how that survives a right to erasure — `destroy` makes every clinical field
    unreadable while the row skeleton (times, durations, origin, status) remains,
    and what remains is anonymous by construction.
    """

    def key_for(self, client_id: ClientId) -> bytes: ...

    def destroy(self, client_id: ClientId) -> None: ...
