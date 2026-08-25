"""The permission matrix, as data, plus the reference policy that reads it.

CONTROL_SURFACE.md §4 is a table. This is that table in executable form, which
is the point: a matrix can be checked for completeness, and a command added
without an entry fails a test rather than quietly inheriting someone's default.

Two invariants the tests pin:

- **Totality.** Every role has an explicit entry for every command. There is no
  fallback, because a fallback is where an accidental grant hides.
- **Attenuation.** An assistant's answer is computed *after* the delegator's,
  so it can only ever narrow. There is no path by which delegation grants
  something the human did not already hold.
"""
from __future__ import annotations

from typing import Dict, Mapping, Optional

from .commands import CommandSpec, names, spec_of, unresolved_target
from .decisions import ALLOW, Decision, Deny, NeedsConfirmation
from .principals import Session
from .values import Access, Role, Target, TargetKind, Tier

# What an assistant may do with a command its delegator already holds.
ALLOWED = "allowed"        # act directly (reads, and nothing else)
DRAFT = "draft"            # compose it; a human confirms
FORBIDDEN = "forbidden"    # not delegable at any tier

D = Access.DENIED
OWN = Access.OWN
ANY = Access.ANY


PROVIDER_MATRIX: Mapping[str, Access] = {
    "client.add": ANY,
    "client.summary": ANY,
    "service.add": ANY,
    "service.set_active": ANY,
    "service.list": ANY,
    "availability.set_weekly": ANY,
    "availability.set_exception": ANY,
    "availability.clear_exception": ANY,
    "availability.read": ANY,
    "request.submit": ANY,
    "request.withdraw": ANY,
    "request.list": ANY,
    "appointment.cancel": ANY,
    "appointment.move": ANY,
    "appointment.request_reschedule": ANY,
    "appointment.mark_attendance": ANY,
    "appointment.place_manually": ANY,
    "appointment.read": ANY,
    "plan.propose": ANY,
    "plan.tick": D,                 # runs are fired by the clock, not by hand
    "plan.list": ANY,
    "plan.discard": ANY,
    "plan.approve": ANY,
    "approval.respond": D,          # forging consent — see the module note below
    "plan.settle": ANY,
    "plan.reject": ANY,
    "approval.list": ANY,
    "provider.away": ANY,
    "orphans.list": ANY,
    "orphans.rehouse": ANY,
    "orphans.cancel": ANY,
    "optimiser.read": ANY,
    "optimiser.set_objective": ANY,
    "optimiser.set_policy": ANY,
    "optimiser.set_budget": ANY,
    "optimiser.set_bounds": D,      # resource control belongs to the platform
    "view.snapshot": ANY,
    "view.holds": ANY,
    "system.reset": D,
    "system.save": D,
}

CLIENT_MATRIX: Mapping[str, Access] = {
    "client.add": D,
    "client.summary": OWN,
    "service.add": D,
    "service.set_active": D,
    "service.list": ANY,            # the catalogue is not anybody's private data
    "availability.set_weekly": OWN,
    "availability.set_exception": OWN,
    "availability.clear_exception": OWN,
    "availability.read": OWN,
    "request.submit": OWN,
    "request.withdraw": OWN,
    "request.list": OWN,
    "appointment.cancel": OWN,
    "appointment.move": OWN,
    "appointment.request_reschedule": OWN,
    "appointment.mark_attendance": D,
    "appointment.place_manually": D,
    "appointment.read": OWN,
    "plan.propose": D,
    "plan.tick": D,
    "plan.list": D,
    "plan.discard": D,
    "plan.approve": D,
    "approval.respond": OWN,        # the only write a provider cannot also do
    "plan.settle": D,
    "plan.reject": D,
    "approval.list": ANY,           # projection-scoped, see PROJECTION_SCOPED
    "provider.away": D,
    "orphans.list": D,
    "orphans.rehouse": D,
    "orphans.cancel": D,
    "optimiser.read": D,
    "optimiser.set_objective": D,
    "optimiser.set_policy": D,
    "optimiser.set_budget": D,
    "optimiser.set_bounds": D,
    "view.snapshot": ANY,           # projection-scoped
    "view.holds": ANY,              # projection-scoped
    "system.reset": D,
    "system.save": D,
}

# The platform operates the deployment; it is not a member of the practice and
# deliberately cannot read clinical data. Its whole remit is resource limits and
# the lifecycle of the deployment itself.
PLATFORM_MATRIX: Mapping[str, Access] = {name: D for name in names()}
PLATFORM_MATRIX = dict(PLATFORM_MATRIX, **{
    "optimiser.read": ANY,
    "optimiser.set_bounds": ANY,
    "system.reset": ANY,
    "system.save": ANY,
})

# The clock. One command, nothing else — a scheduled run must not be a way in.
SYSTEM_MATRIX: Mapping[str, Access] = dict(
    {name: D for name in names()}, **{"plan.tick": ANY}
)

MATRIX: Mapping[Role, Mapping[str, Access]] = {
    Role.PROVIDER: PROVIDER_MATRIX,
    Role.CLIENT: CLIENT_MATRIX,
    Role.PLATFORM: PLATFORM_MATRIX,
    Role.SYSTEM: SYSTEM_MATRIX,
}


ASSISTANT: Mapping[str, str] = {
    "client.add": DRAFT,
    "client.summary": ALLOWED,
    "service.add": DRAFT,
    "service.set_active": DRAFT,
    "service.list": ALLOWED,
    "availability.set_weekly": DRAFT,
    "availability.set_exception": DRAFT,
    "availability.clear_exception": DRAFT,
    "availability.read": ALLOWED,
    "request.submit": DRAFT,
    "request.withdraw": DRAFT,
    "request.list": ALLOWED,
    "appointment.cancel": DRAFT,
    "appointment.move": DRAFT,
    "appointment.request_reschedule": DRAFT,
    "appointment.mark_attendance": DRAFT,
    "appointment.place_manually": DRAFT,
    "appointment.read": ALLOWED,
    "plan.propose": DRAFT,
    "plan.tick": FORBIDDEN,
    "plan.list": ALLOWED,
    "plan.discard": DRAFT,
    "plan.approve": DRAFT,
    "approval.respond": DRAFT,      # never auto-accepted; agreeing is the human's
    "plan.settle": DRAFT,
    "plan.reject": DRAFT,
    "approval.list": ALLOWED,
    "provider.away": DRAFT,
    "orphans.list": ALLOWED,
    "orphans.rehouse": DRAFT,
    "orphans.cancel": FORBIDDEN,
    "optimiser.read": ALLOWED,
    # Steering the optimiser is withheld entirely rather than merely drafted.
    # These are practice-wide and their effects are diffuse: a mis-set horizon
    # does not look wrong in a confirmation dialog the way a cancelled
    # appointment does, so confirmation buys nothing here.
    "optimiser.set_objective": FORBIDDEN,
    "optimiser.set_policy": FORBIDDEN,
    "optimiser.set_budget": FORBIDDEN,
    "optimiser.set_bounds": FORBIDDEN,
    "view.snapshot": ALLOWED,
    "view.holds": ALLOWED,
    "system.reset": FORBIDDEN,
    "system.save": FORBIDDEN,
}


# Reads granted broadly whose scoping is the projection's job rather than the
# ownership predicate's, because they have no single target to own. Every one of
# these must return a per-actor view. `test_permissions.py` pins the list so a
# new broadly-granted read cannot be added without deciding this.
PROJECTION_SCOPED = frozenset({
    "approval.list",
    "view.snapshot",
    "view.holds",
})


def summarise(cmd) -> str:
    """A human-readable description of what a command would do.

    Produced from the command itself so that what a person confirms is what the
    system will run — never a description supplied by whatever asked for it.
    """
    spec = spec_of(cmd)
    target = unresolved_target(cmd)
    if target.kind is TargetKind.NONE:
        return spec.name
    return f"{spec.name} on {target.kind.value} {target.ident}"


def decide(session: Session, cmd, target: Optional[Target] = None) -> Decision:
    """The reference policy: pure, total, and the same answer every time.

    `target` is the *resolved* target — the gate looks the owner up before
    calling this, which is what keeps the function free of I/O and therefore
    exhaustively testable.
    """
    spec: CommandSpec = spec_of(cmd)
    if target is None:
        target = unresolved_target(cmd)

    access = MATRIX[session.principal.role].get(spec.name)
    if access is None:
        raise LookupError(
            f"no permission entry for {session.principal.role.value} / {spec.name}"
        )

    if access is Access.DENIED:
        return Deny(f"{session.principal.role.value} may not {spec.name}")

    if access is Access.OWN:
        if target.kind is TargetKind.NONE:
            raise RuntimeError(
                f"{spec.name} is granted as OWN but points at nothing to own"
            )
        if not session.principal.owns(target.owner):
            # Not "forbidden" — the caller must not learn the thing exists.
            return Deny(f"{spec.name}: target not owned", disguise_as_missing=True)

    if session.delegated:
        delegated = ASSISTANT.get(spec.name)
        if delegated is None:
            raise LookupError(f"no assistant entry for {spec.name}")
        if delegated == FORBIDDEN:
            return Deny(f"{spec.name} is not delegable to an assistant")
        if delegated == DRAFT:
            return NeedsConfirmation(summarise(cmd))

    return ALLOW
