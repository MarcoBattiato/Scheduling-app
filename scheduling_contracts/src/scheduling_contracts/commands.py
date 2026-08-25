"""Every action that can be triggered from outside, as a typed value.

Two properties matter more than the list itself.

**No command carries an identity.** There is no `client_id` parameter meaning
"who I am" anywhere below. Where a `client_id` does appear it names the *subject*
of the command, and the gate still checks it against the authenticated principal.
Likewise `appointment.cancel` has no `by_provider` flag: who cancelled is derived
from the session, because it is evidence rather than input.

**Metadata is data.** Each command registers its tier and what it points at, so
the permission model in `permissions.py` is a table that can be checked for
completeness rather than a pile of `if` statements. Adding a command without a
permission entry fails a test.

Payments and documents are absent on purpose. They are named in
CONTROL_SURFACE.md but their payloads are not designed, and inventing them here
would put speculative shapes into the one place that is expensive to change.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time
from typing import Dict, Optional, Sequence, Tuple, Type

from .values import (
    AppointmentId,
    ApprovalId,
    BudgetBounds,
    ClientId,
    PlanId,
    RequestId,
    ServiceId,
    Target,
    TargetKind,
    Tier,
    TimeSpan,
)


@dataclass(frozen=True)
class CommandSpec:
    name: str
    tier: Tier
    target: TargetKind
    target_field: Optional[str] = None
    # Carries or returns clinical content, and so is subject to the encryption
    # and minimisation rules rather than only the permission rules.
    phi: bool = False


_SPECS: Dict[type, CommandSpec] = {}
_BY_NAME: Dict[str, type] = {}


def command(name, tier, target=TargetKind.NONE, target_field=None, phi=False):
    """Register a command class and attach its spec."""
    def register(cls):
        if name in _BY_NAME:
            raise RuntimeError(f"duplicate command name {name!r}")
        if (target is TargetKind.NONE) != (target_field is None):
            raise RuntimeError(
                f"{name}: a targeted command needs a target_field, and a "
                f"practice-wide one must not have any"
            )
        if target_field is not None and target_field not in cls.__annotations__:
            raise RuntimeError(f"{name}: no field named {target_field!r}")
        spec = CommandSpec(name, tier, target, target_field, phi)
        _SPECS[cls] = spec
        _BY_NAME[name] = cls
        cls.__command_spec__ = spec
        return cls
    return register


def spec_of(cmd) -> CommandSpec:
    try:
        return _SPECS[type(cmd)]
    except KeyError:
        raise LookupError(f"{type(cmd).__name__} is not a registered command") from None


def unresolved_target(cmd) -> Target:
    """What the command points at, before the gate has found its owner.

    Returns the kind and the identifier only. Resolving that to a `ClientId` is
    a repository lookup and therefore the gate's job, not the command's — which
    is what keeps the policy a pure function.
    """
    spec = spec_of(cmd)
    if spec.target_field is None:
        return Target(TargetKind.NONE)
    ident = getattr(cmd, spec.target_field)
    # An optional target left unset means "all of that kind" — `request.list`
    # with no client is the whole queue. There is then no owner to resolve, so
    # a role holding only `OWN` is refused, which is the intended answer. A
    # gate may instead substitute the principal's own id before calling the
    # policy, turning "list requests" into "list mine"; that is a convenience
    # of the gate and deliberately not a rule of the contract.
    return Target(spec.target, ident)


def all_commands() -> Tuple[Type, ...]:
    return tuple(_SPECS)


def by_name(name: str) -> type:
    return _BY_NAME[name]


def names() -> Tuple[str, ...]:
    return tuple(sorted(_BY_NAME))


@dataclass(frozen=True)
class WeeklyWindow:
    """One recurring band in a weekly availability pattern."""
    weekday: int          # 0 = Monday
    start: time
    end: time


# ---------------------------------------------------------------- A. directory

@command("client.add", Tier.ROUTINE)
@dataclass(frozen=True)
class AddClient:
    client_id: ClientId
    name: str
    mirror_provider_hours: bool = True


@command("client.summary", Tier.READ, TargetKind.CLIENT, "client_id", phi=True)
@dataclass(frozen=True)
class ReadClientSummary:
    client_id: ClientId


@command("service.add", Tier.ROUTINE)
@dataclass(frozen=True)
class AddService:
    service_id: ServiceId
    name: str
    duration_minutes: int
    price_minor_units: int


@command("service.set_active", Tier.CONSEQUENTIAL)
@dataclass(frozen=True)
class SetServiceActive:
    service_id: ServiceId
    active: bool


@command("service.list", Tier.READ)
@dataclass(frozen=True)
class ListServices:
    include_inactive: bool = False


# ------------------------------------------------------------- B. availability

@command("availability.set_weekly", Tier.ROUTINE, TargetKind.CLIENT, "client_id")
@dataclass(frozen=True)
class SetWeeklyAvailability:
    client_id: ClientId
    windows: Sequence[WeeklyWindow]


@command("availability.set_exception", Tier.ROUTINE, TargetKind.CLIENT, "client_id")
@dataclass(frozen=True)
class SetAvailabilityException:
    client_id: ClientId
    on_date: date
    start: time
    end: time
    available: bool


@command("availability.clear_exception", Tier.ROUTINE, TargetKind.CLIENT, "client_id")
@dataclass(frozen=True)
class ClearAvailabilityException:
    client_id: ClientId
    on_date: date
    start: time
    end: time


@command("availability.read", Tier.READ, TargetKind.CLIENT, "client_id")
@dataclass(frozen=True)
class ReadAvailability:
    client_id: ClientId
    window: TimeSpan


# ----------------------------------------------------------------- C. requests

@command("request.submit", Tier.ROUTINE, TargetKind.CLIENT, "client_id")
@dataclass(frozen=True)
class SubmitRequest:
    client_id: ClientId
    service_id: ServiceId
    preferred_start: datetime
    note: str = ""


@command("request.withdraw", Tier.ROUTINE, TargetKind.REQUEST, "request_id")
@dataclass(frozen=True)
class WithdrawRequest:
    request_id: RequestId


@command("request.list", Tier.READ, TargetKind.CLIENT, "client_id")
@dataclass(frozen=True)
class ListRequests:
    # None means "the whole queue", which only a practice-wide role may ask for.
    client_id: Optional[ClientId] = None


# ------------------------------------------------------------- D. appointments

@command("appointment.cancel", Tier.CONSEQUENTIAL, TargetKind.APPOINTMENT, "appointment_id")
@dataclass(frozen=True)
class CancelAppointment:
    """No `by` field. Whether this is a client cancelling or the provider
    closing a day is derived from the session, because the two mean different
    things to anything learning from history and a caller must not get to
    choose which one it looks like.
    """
    appointment_id: AppointmentId
    reason: str = ""


@command("appointment.move", Tier.CONSEQUENTIAL, TargetKind.APPOINTMENT, "appointment_id")
@dataclass(frozen=True)
class MoveAppointment:
    """A client moving their own booking. Their choice, so `Origin.CLIENT`."""
    appointment_id: AppointmentId
    to: TimeSpan


@command("appointment.request_reschedule", Tier.ROUTINE, TargetKind.APPOINTMENT, "appointment_id")
@dataclass(frozen=True)
class RequestReschedule:
    """Asking to be moved without naming where — the slot is kept until there
    is somewhere to go.
    """
    appointment_id: AppointmentId
    preferred_start: Optional[datetime] = None
    release_slot: bool = False


@command("appointment.mark_attendance", Tier.AUTHORITY, TargetKind.APPOINTMENT, "appointment_id")
@dataclass(frozen=True)
class MarkAttendance:
    appointment_id: AppointmentId
    attended: bool


@command("appointment.place_manually", Tier.AUTHORITY, TargetKind.REQUEST, "request_id")
@dataclass(frozen=True)
class PlaceManually:
    request_id: RequestId
    start: datetime
    lock: bool = True


@command("appointment.read", Tier.READ, TargetKind.CLIENT, "client_id", phi=True)
@dataclass(frozen=True)
class ReadAppointments:
    client_id: ClientId
    window: TimeSpan
    include_history: bool = False


# ---------------------------------------------------------- E. scheduling runs

@command("plan.propose", Tier.CONSEQUENTIAL)
@dataclass(frozen=True)
class ProposePlan:
    """Run the solver. Overrides are per-run and clamp to the same
    `BudgetBounds` as the standing defaults.
    """
    reason: str = "manual"
    horizon_days: Optional[int] = None
    request_ids: Optional[Sequence[RequestId]] = None
    alpha: Optional[float] = None
    max_displacements: Optional[int] = None
    allow_chains: Optional[bool] = None


@command("plan.tick", Tier.ROUTINE)
@dataclass(frozen=True)
class TickScheduler:
    """Fire a run if the policy says one is due. System only — a human asking
    for a run issues `plan.propose` instead.
    """
    now: Optional[datetime] = None


@command("plan.list", Tier.READ)
@dataclass(frozen=True)
class ListPlans:
    pass


@command("plan.discard", Tier.ROUTINE, TargetKind.PLAN, "plan_id")
@dataclass(frozen=True)
class DiscardPlan:
    plan_id: PlanId


# ------------------------------------------------------------- F. negotiation

@command("plan.approve", Tier.CONSEQUENTIAL, TargetKind.PLAN, "plan_id")
@dataclass(frozen=True)
class ApprovePlan:
    """Provider approval only means the plan is worth asking clients about."""
    plan_id: PlanId
    items: Optional[Sequence[str]] = None


@command("approval.respond", Tier.CONSEQUENTIAL, TargetKind.APPROVAL, "approval_id")
@dataclass(frozen=True)
class RespondToApproval:
    """Accept, decline (not that slot), or refuse (not at all).

    The one command a provider must not hold. Answering on someone's behalf
    would forge the consent the whole negotiation exists to obtain.
    """
    approval_id: ApprovalId
    answer: str          # accept | decline | refuse


@command("plan.settle", Tier.CONSEQUENTIAL, TargetKind.PLAN, "plan_id")
@dataclass(frozen=True)
class SettlePlan:
    plan_id: PlanId
    how: str             # agreed | agreed_and_drop | reject


@command("plan.reject", Tier.ROUTINE, TargetKind.PLAN, "plan_id")
@dataclass(frozen=True)
class RejectPlan:
    plan_id: PlanId


@command("approval.list", Tier.READ)
@dataclass(frozen=True)
class ListApprovals:
    """Scoped by projection rather than by ownership: a client sees the
    questions addressed to them, and learns nothing about anyone else's.
    """
    plan_id: Optional[PlanId] = None


# ------------------------------------------------------ G. provider operations

@command("provider.away", Tier.AUTHORITY)
@dataclass(frozen=True)
class ProviderAway:
    span: TimeSpan
    action: str = "flag"     # flag | rehouse | cancel


@command("orphans.list", Tier.READ)
@dataclass(frozen=True)
class ListOrphans:
    horizon_days: int = 28


@command("orphans.rehouse", Tier.CONSEQUENTIAL)
@dataclass(frozen=True)
class RehouseOrphans:
    appointment_ids: Optional[Sequence[AppointmentId]] = None


@command("orphans.cancel", Tier.DESTRUCTIVE)
@dataclass(frozen=True)
class CancelOrphans:
    appointment_ids: Optional[Sequence[AppointmentId]] = None


# --------------------------------------------------------- H. optimiser control

@command("optimiser.read", Tier.READ)
@dataclass(frozen=True)
class ReadOptimiser:
    pass


@command("optimiser.set_objective", Tier.AUTHORITY)
@dataclass(frozen=True)
class SetObjective:
    """What a good schedule means. Wrong values give a schedule the provider
    dislikes; nothing breaks.
    """
    alpha: Optional[float] = None


@command("optimiser.set_policy", Tier.AUTHORITY)
@dataclass(frozen=True)
class SetRunPolicy:
    auto_run: Optional[bool] = None
    weekly_runs: Optional[Sequence[Tuple[int, time]]] = None
    urgency_hours: Optional[int] = None
    retry_after_minutes: Optional[int] = None
    scope_to_horizon: Optional[bool] = None
    rehouse_orphans_on_run: Optional[bool] = None


@command("optimiser.set_budget", Tier.AUTHORITY)
@dataclass(frozen=True)
class SetBudget:
    """What a run is allowed to cost. Clamped to `BudgetBounds`."""
    horizon_days: Optional[int] = None
    max_displacements: Optional[int] = None
    allow_chains: Optional[bool] = None
    grid_minutes: Optional[int] = None
    time_limit_seconds: Optional[float] = None


@command("optimiser.set_bounds", Tier.DESTRUCTIVE)
@dataclass(frozen=True)
class SetBudgetBounds:
    """Platform only. The single command withheld from the provider for reasons
    of resource control rather than authority.
    """
    bounds: BudgetBounds


# ------------------------------------------------------------- I. read surface

@command("view.snapshot", Tier.READ, phi=True)
@dataclass(frozen=True)
class ViewSnapshot:
    """Everything a view needs — as a projection computed for this actor, never
    the whole world filtered afterwards by the caller.
    """
    horizon_days: int = 28


@command("view.holds", Tier.READ)
@dataclass(frozen=True)
class ViewHolds:
    pass


# ------------------------------------------------------------------- J. system

@command("system.reset", Tier.DESTRUCTIVE)
@dataclass(frozen=True)
class ResetSystem:
    scenario: str = "simple"


@command("system.save", Tier.ROUTINE)
@dataclass(frozen=True)
class SaveSnapshot:
    pass
