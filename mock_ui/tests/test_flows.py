"""The flows a person can actually drive, exercised without a browser."""
from datetime import date, datetime, time, timedelta

import pytest
from calendar_store import AppointmentStatus, Origin

from mock_ui.state import PROVIDER, World


def monday() -> date:
    """The next Monday, so tests never depend on today being a weekday."""
    today = date.today()
    return today + timedelta(days=(7 - today.weekday()) % 7 or 7)


def at(day_offset: int, hour: int, minute: int = 0) -> datetime:
    return datetime.combine(monday() + timedelta(days=day_offset), time(hour, minute))


def iso(moment: datetime) -> str:
    return moment.isoformat()


@pytest.fixture
def world() -> World:
    w = World()
    w.add_client("alice", "Alice")
    w.add_client("bob", "Bob")
    w.set_weekly_availability(
        PROVIDER, [{"weekday": d, "from": "09:00", "to": "17:00"} for d in range(5)]
    )
    for client in ("alice", "bob"):
        w.set_weekly_availability(
            client, [{"weekday": d, "from": "09:00", "to": "17:00"} for d in range(5)]
        )
    return w


def request(world, client, duration, day, from_hour, to_hour):
    return world.submit_request(client, duration, [
        {"from": iso(at(day, from_hour)), "to": iso(at(day, to_hour))}
    ])


# -- the ordinary path ----------------------------------------------


def test_a_request_that_fits_is_booked_without_bothering_anyone(world):
    request(world, "alice", 60, 0, 9, 17)
    outcome = world.solve()

    assert outcome["placed"] == 1
    assert outcome["awaiting_approval"] == 0
    assert not world.approvals
    (booked,) = world.store.appointments_for("alice", at(0, 0), at(1, 0))
    assert booked.origin is Origin.CLIENT, "a slot the client asked for is their choice"


def test_availability_round_trips_through_the_weekly_grid(world):
    world.set_weekly_availability("alice", [
        {"weekday": 1, "from": "10:00", "to": "12:30"},
    ])
    weekly = world.snapshot()["weekly"]["alice"]

    assert weekly == [{"weekday": 1, "from": "10:00", "to": "12:30"}]


def test_setting_availability_replaces_rather_than_accumulates(world):
    world.set_weekly_availability("alice", [{"weekday": 0, "from": "09:00", "to": "17:00"}])
    world.set_weekly_availability("alice", [{"weekday": 3, "from": "09:00", "to": "12:00"}])

    assert [r["weekday"] for r in world.snapshot()["weekly"]["alice"]] == [3]


def test_cancelling_frees_the_slot_and_keeps_the_record(world):
    request(world, "alice", 60, 0, 9, 17)
    world.solve()
    (booked,) = world.store.appointments_for("alice", at(0, 0), at(1, 0))

    world.cancel_appointment(booked.id)

    assert world.store.appointments_for("alice", at(0, 0), at(1, 0)) == []
    history = world.store.appointment_history("alice", at(0, 0), at(1, 0))
    assert [a.status for a in history] == [AppointmentStatus.CANCELLED]


def test_a_client_moving_their_own_booking_counts_as_their_choice(world):
    request(world, "alice", 60, 0, 9, 17)
    world.solve()
    (booked,) = world.store.appointments_for("alice", at(0, 0), at(1, 0))

    moved = world.move_appointment(booked.id, at(1, 14), at(1, 15))

    assert moved.origin is Origin.CLIENT
    assert moved.supersedes == booked.id


# -- the negotiation the mock owns -----------------------------------


def _full_monday(world):
    """Monday holds exactly one hour and bob has it, so a Monday request can
    only be met by moving him — and Tuesday is open, so he has somewhere to go.
    """
    world.set_weekly_availability(PROVIDER, [
        {"weekday": 0, "from": "09:00", "to": "10:00"},
        {"weekday": 1, "from": "09:00", "to": "17:00"},
    ])
    world.set_weekly_availability(
        "bob", [{"weekday": d, "from": "09:00", "to": "17:00"} for d in range(5)]
    )
    world.store.book_appointment("bob", "session", at(0, 9), at(0, 10))


def test_a_displacement_waits_for_the_client_rather_than_happening(world):
    _full_monday(world)
    request(world, "alice", 60, 0, 9, 10)

    outcome = world.solve()

    assert outcome["awaiting_approval"] == 1
    assert outcome["placed"] == 0, "nothing is written until the client agrees"
    (approval,) = world.approvals.values()
    assert approval.client_id == "bob"
    # bob has not moved, and alice is not booked
    assert world.store.appointments_for("bob", at(0, 0), at(1, 0))[0].range.start == at(0, 9)
    assert world.store.appointments_for("alice", at(0, 0), at(1, 0)) == []


def test_accepting_applies_the_plan_and_records_it_as_a_displacement(world):
    _full_monday(world)
    request(world, "alice", 60, 0, 9, 10)
    world.solve()
    (approval,) = world.approvals.values()

    world.respond_to_approval(approval.id, accept=True)

    (alice,) = world.store.appointments_for("alice", at(0, 0), at(1, 0))
    assert alice.range.start == at(0, 9)

    moved = [a for a in world.store.appointment_history("bob", at(0, 0), at(5, 0))
             if a.is_live]
    assert len(moved) == 1
    assert moved[0].origin is Origin.DISPLACED, (
        "bob agreed to move but did not choose the slot — recording it as his "
        "preference is exactly what the origin field exists to prevent"
    )


def test_declining_leaves_everything_alone_and_stops_re_asking(world):
    _full_monday(world)
    request(world, "alice", 60, 0, 9, 10)
    world.solve()
    (approval,) = world.approvals.values()
    bob_before = world.store.appointments_for("bob", at(0, 0), at(1, 0))[0]

    world.respond_to_approval(approval.id, accept=False)

    assert world.store.appointments_for("bob", at(0, 0), at(1, 0))[0] == bob_before
    assert world.store.appointments_for("alice", at(0, 0), at(1, 0)) == []
    assert bob_before.id in world.immovable
    # Re-solving must not pester bob again with the same ask.
    world.solve()
    assert not [a for a in world.approvals.values() if a.status == "pending"]


def test_a_displaced_client_is_never_simply_cancelled(world):
    _full_monday(world)
    request(world, "alice", 60, 0, 9, 10)
    world.solve()
    (approval,) = world.approvals.values()
    world.respond_to_approval(approval.id, accept=True)

    live = [a for a in world.store.appointment_history("bob", at(-7, 0), at(14, 0))
            if a.is_live]
    assert len(live) == 1, "bob still has an appointment; he was moved, not dropped"


def test_locked_appointments_are_never_offered_up(world):
    """Same situation as above, except bob's hour is staff-pinned — so the
    request simply fails rather than anyone being asked to move.
    """
    world.set_weekly_availability(PROVIDER, [
        {"weekday": 0, "from": "09:00", "to": "10:00"},
        {"weekday": 1, "from": "09:00", "to": "17:00"},
    ])
    world.store.book_appointment("bob", "session", at(0, 9), at(0, 10), locked=True)
    request(world, "alice", 60, 0, 9, 10)

    outcome = world.solve()

    assert outcome["awaiting_approval"] == 0
    assert outcome["unplaced"] == 1


def test_displacement_can_be_turned_off_entirely(world):
    world.max_displacements = 0
    _full_monday(world)
    request(world, "alice", 60, 0, 9, 10)

    outcome = world.solve()

    assert outcome["awaiting_approval"] == 0
    assert outcome["unplaced"] == 1


# -- snapshot --------------------------------------------------------


def test_the_snapshot_carries_what_every_view_needs(world):
    request(world, "alice", 60, 0, 9, 17)
    world.solve()

    snap = world.snapshot()

    assert {c["id"] for c in snap["clients"]} == {"alice", "bob"}
    assert snap["appointments"] and snap["appointments"][0]["origin"] == "client"
    assert "provider-self" in snap["availability"]
    assert snap["requests"][0]["status"] == "placed"
    assert snap["log"]
