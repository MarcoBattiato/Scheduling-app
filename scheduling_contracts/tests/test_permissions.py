"""The permission matrix is total, and delegation can only ever narrow it."""
from __future__ import annotations

import pytest

from scheduling_contracts import (
    ALLOWED,
    ASSISTANT,
    DRAFT,
    FORBIDDEN,
    MATRIX,
    PROJECTION_SCOPED,
    Access,
    Allow,
    Deny,
    NeedsConfirmation,
    Principal,
    Role,
    Session,
    Target,
    TargetKind,
    Tier,
    all_commands,
    by_name,
    decide,
    names,
    spec_of,
)
from conftest import build

ALICE = Principal(Role.CLIENT, "alice")
BOB = Principal(Role.CLIENT, "bob")
PROVIDER = Principal(Role.PROVIDER)
PLATFORM = Principal(Role.PLATFORM)
SYSTEM = Principal(Role.SYSTEM)

# Ordering of outcomes, so "delegation never widens" can be stated as <=.
RANK = {Deny: 0, NeedsConfirmation: 1, Allow: 2}


def rank(decision):
    return RANK[type(decision)]


def owned_target(cmd, owner):
    from scheduling_contracts import unresolved_target
    t = unresolved_target(cmd)
    return Target(t.kind, t.ident, owner)


# -- totality -------------------------------------------------------

def test_every_role_has_an_entry_for_every_command():
    """No fallback anywhere. A fallback is where an accidental grant hides."""
    for role, table in MATRIX.items():
        missing = set(names()) - set(table)
        assert not missing, f"{role.value} has no entry for {sorted(missing)}"
        extra = set(table) - set(names())
        assert not extra, f"{role.value} has entries for unknown {sorted(extra)}"


def test_every_command_has_an_assistant_entry():
    assert set(ASSISTANT) == set(names())


def test_assistant_values_are_from_the_small_vocabulary():
    assert set(ASSISTANT.values()) <= {ALLOWED, DRAFT, FORBIDDEN}


def test_decide_answers_for_every_role_and_command(every_command):
    for cmd in every_command:
        for principal in (ALICE, PROVIDER, PLATFORM, SYSTEM):
            session = Session(principal)
            target = owned_target(cmd, principal.client_id)
            decide(session, cmd, target)     # must not raise


# -- attenuation ----------------------------------------------------

def test_delegation_never_widens_a_permission(every_command):
    """The core safety property: an assistant's answer is computed after its
    delegator's, so it can only narrow. There is no path by which delegating
    grants something the human did not already hold.
    """
    for cmd in every_command:
        for principal in (ALICE, PROVIDER, PLATFORM, SYSTEM):
            target = owned_target(cmd, principal.client_id)
            direct = decide(Session(principal), cmd, target)
            via_bot = decide(Session(principal, via_assistant=True), cmd, target)
            assert rank(via_bot) <= rank(direct), (
                f"{spec_of(cmd).name}: assistant got {via_bot} "
                f"where {principal.role.value} got {direct}"
            )


def test_an_assistant_never_acts_alone_on_a_write(every_command):
    """Reads may go straight through; anything that changes the world needs a
    human, either by being drafted or by being refused outright.
    """
    for cmd in every_command:
        spec = spec_of(cmd)
        if spec.tier is Tier.READ:
            continue
        for principal in (ALICE, PROVIDER, PLATFORM, SYSTEM):
            target = owned_target(cmd, principal.client_id)
            decision = decide(Session(principal, via_assistant=True), cmd, target)
            assert not isinstance(decision, Allow), (
                f"{spec.name} was allowed outright via an assistant"
            )


def test_destructive_and_financial_are_never_delegable():
    for name in names():
        tier = spec_of(build(by_name(name))).tier
        if tier in (Tier.DESTRUCTIVE, Tier.FINANCIAL):
            assert ASSISTANT[name] == FORBIDDEN, name


# -- ownership ------------------------------------------------------

def test_a_client_cannot_reach_another_clients_records():
    """The predicate that makes the whole model work, and the one the current
    code has no equivalent of.
    """
    from scheduling_contracts import CancelAppointment
    cmd = CancelAppointment(appointment_id=41)
    target = Target(TargetKind.APPOINTMENT, 41, owner="bob")

    decision = decide(Session(ALICE), cmd, target)
    assert isinstance(decision, Deny)
    assert decision.disguise_as_missing, (
        "denial must not confirm that another client's appointment exists"
    )


def test_the_same_command_is_allowed_on_your_own_record():
    from scheduling_contracts import CancelAppointment
    cmd = CancelAppointment(appointment_id=41)
    target = Target(TargetKind.APPOINTMENT, 41, owner="alice")
    assert isinstance(decide(Session(ALICE), cmd, target), Allow)


def test_an_unowned_target_is_refused_rather_than_waved_through():
    """A target the repository could not resolve has owner None. That must read
    as "no", not as "no owner, so no objection".
    """
    from scheduling_contracts import CancelAppointment
    target = Target(TargetKind.APPOINTMENT, 999, owner=None)
    assert isinstance(decide(Session(ALICE), CancelAppointment(999), target), Deny)


# -- the named invariants -------------------------------------------

def test_a_provider_cannot_answer_a_clients_consent_question():
    """The one command the provider does not hold. Answering for someone would
    forge the agreement the negotiation exists to obtain.
    """
    from scheduling_contracts import RespondToApproval
    cmd = RespondToApproval(approval_id=7, answer="accept")
    target = Target(TargetKind.APPROVAL, 7, owner="alice")
    assert isinstance(decide(Session(PROVIDER), cmd, target), Deny)
    assert isinstance(decide(Session(ALICE), cmd, target), Allow)


def test_the_platform_cannot_read_clinical_data():
    """The platform operates the deployment. That is not a reason to see
    anybody's medical history.
    """
    for name in names():
        if spec_of(build(by_name(name))).phi:
            assert MATRIX[Role.PLATFORM][name] is Access.DENIED, name


def test_the_provider_cannot_raise_their_own_resource_limits():
    assert MATRIX[Role.PROVIDER]["optimiser.set_bounds"] is Access.DENIED
    assert MATRIX[Role.PLATFORM]["optimiser.set_bounds"] is Access.ANY


def test_only_the_clock_can_tick():
    for role in (Role.PROVIDER, Role.CLIENT, Role.PLATFORM):
        assert MATRIX[role]["plan.tick"] is Access.DENIED
    assert MATRIX[Role.SYSTEM]["plan.tick"] is Access.ANY


def test_broadly_granted_client_reads_are_declared_projection_scoped():
    """A read granted to a client with no target to own has nothing for the
    ownership predicate to check, so its scoping is the projection's job. This
    pins the list, so a new one cannot be added without deciding that.
    """
    broad = {
        name for name in names()
        if MATRIX[Role.CLIENT][name] is Access.ANY
        and spec_of(build(by_name(name))).target is TargetKind.NONE
        and spec_of(build(by_name(name))).tier is Tier.READ
    }
    assert broad - {"service.list"} == PROJECTION_SCOPED


def test_an_own_grant_always_has_something_to_own():
    """`OWN` on a command with no target would raise at decide-time. Catch it
    here instead of in production.
    """
    for role, table in MATRIX.items():
        for name, access in table.items():
            if access is Access.OWN:
                assert spec_of(build(by_name(name))).target is not TargetKind.NONE, (
                    f"{role.value}/{name} is OWN but points at nothing"
                )


def test_a_client_cannot_ask_for_the_whole_queue():
    """An optional target left unset means "everything of that kind". There is
    then no owner to check against, so a role holding only `OWN` is refused —
    a client cannot turn `request.list` into a directory of other people.
    """
    from scheduling_contracts import ListRequests, unresolved_target
    cmd = ListRequests(client_id=None)
    target = unresolved_target(cmd)

    assert isinstance(decide(Session(ALICE), cmd, target), Deny)
    assert isinstance(decide(Session(PROVIDER), cmd, target), Allow)


def test_a_client_may_list_their_own_requests():
    from scheduling_contracts import ListRequests
    cmd = ListRequests(client_id="alice")
    target = Target(TargetKind.CLIENT, "alice", owner="alice")
    assert isinstance(decide(Session(ALICE), cmd, target), Allow)
