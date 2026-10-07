"""The command registry is complete and internally consistent."""
from __future__ import annotations

import dataclasses

import pytest

from scheduling_contracts import (
    TargetKind,
    Tier,
    all_commands,
    by_name,
    names,
    spec_of,
    unresolved_target,
)
from conftest import build


def _target_is_optional(cls, spec):
    """Some targeted commands may name nothing, meaning "all of that kind"."""
    field = next(f for f in dataclasses.fields(cls) if f.name == spec.target_field)
    return field.default is None


def test_every_command_has_a_spec():
    for cls in all_commands():
        assert isinstance(spec_of(build(cls)).name, str)


def test_names_are_unique_and_dotted():
    assert len(set(names())) == len(names())
    for name in names():
        resource, _, action = name.partition(".")
        assert resource and action, f"{name} should read resource.action"


def test_every_command_is_a_frozen_dataclass():
    """Commands travel across a trust boundary and get hashed into a
    confirmation token; a mutable one could be altered after approval.
    """
    for cls in all_commands():
        assert dataclasses.is_dataclass(cls), cls
        assert cls.__dataclass_params__.frozen, f"{cls.__name__} is not frozen"


def test_a_targeted_command_can_name_what_it_points_at():
    for cls in all_commands():
        cmd = build(cls)
        spec = spec_of(cmd)
        target = unresolved_target(cmd)
        assert target.kind is spec.target
        if spec.target is TargetKind.NONE:
            assert target.ident is None
        elif not _target_is_optional(cls, spec):
            assert target.ident is not None, f"{spec.name} lost its target id"


def test_owner_is_never_pre_filled_by_the_command():
    """Ownership is resolved by the gate against the repository. A command that
    could assert its own owner would be asserting an identity, which is the one
    thing the design does not let a caller do.
    """
    for cls in all_commands():
        assert unresolved_target(build(cls)).owner is None


def test_no_command_carries_an_actor_field():
    """The rule the whole design rests on. `client_id` may name a *subject*,
    but nothing may name the caller, and nothing may assert which party acted.
    """
    banned = {"actor", "principal", "session", "role", "by", "by_provider",
              "as_client", "caller", "on_behalf_of"}
    for cls in all_commands():
        fields = {f.name for f in dataclasses.fields(cls)}
        assert not (fields & banned), f"{cls.__name__} carries {fields & banned}"


def test_by_name_round_trips():
    for name in names():
        assert spec_of(build(by_name(name))).name == name


def test_phi_commands_are_reads_or_writes_we_expect():
    """Anything marked PHI has to be handled by the encryption rules, so the
    set is small and deliberate rather than accidental.
    """
    phi = {n for n in names() if spec_of(build(by_name(n))).phi}
    # charge.list: a charge names the service it is for, and service names
    # are clinical (CONTROL_SURFACE.md §7) — a bill says what you were seen for.
    # message.send / message.list: people write to their clinician about why
    # they are coming. The conversation is clinical by nature.
    assert phi == {"client.summary", "appointment.read", "view.snapshot", "charge.list",
                   "message.send", "message.list"}
