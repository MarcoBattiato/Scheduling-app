"""Availability, the catalogue, and what a client can do to their own booking.

The propose/approve/answer machinery lives in test_booking_handler.py; here it
is only used as a means of getting something into the calendar.
"""
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


@pytest.fixture
def world() -> World:
    w = World()
    w.catalogue.add_service("s60", "Hour", 60, 8000)
    w.catalogue.add_service("s90", "Long", 90, 11000)
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


def ask(world, client, duration, day, from_hour, to_hour):
    return world.submit_request(client, f"s{duration}", [
        {"from": at(day, from_hour).isoformat(), "to": at(day, to_hour).isoformat()}
    ])


def place(world, client, duration, day, from_hour, to_hour):
    """Drive a request all the way into the calendar."""
    request = ask(world, client, duration, day, from_hour, to_hour)
    world.propose()
    plan = next(p for p in world.plans.values() if p.status == "draft")
    world.provider_approve(plan.id)
    for approval in world.pending_approvals(plan.id):
        world.respond_to_approval(approval.id, accept=True)
    return request


# -- availability -----------------------------------------------------


def test_availability_round_trips_through_the_weekly_grid(world):
    world.set_weekly_availability("alice", [
        {"weekday": 1, "from": "10:00", "to": "12:30"},
    ])

    assert world.snapshot()["weekly"]["alice"] == [
        {"weekday": 1, "from": "10:00", "to": "12:30"}
    ]


def test_setting_availability_replaces_rather_than_accumulates(world):
    world.set_weekly_availability("alice", [{"weekday": 0, "from": "09:00", "to": "17:00"}])
    world.set_weekly_availability("alice", [{"weekday": 3, "from": "09:00", "to": "12:00"}])

    assert [r["weekday"] for r in world.snapshot()["weekly"]["alice"]] == [3]


# -- what a client can do to their own booking ------------------------


def test_cancelling_frees_the_slot_and_keeps_the_record(world):
    place(world, "alice", 60, 0, 9, 17)
    (booked,) = world.store.appointments_for("alice", at(0, 0), at(1, 0))

    world.cancel_appointment(booked.id)

    assert world.store.appointments_for("alice", at(0, 0), at(1, 0)) == []
    history = world.store.appointment_history("alice", at(0, 0), at(1, 0))
    assert [a.status for a in history] == [AppointmentStatus.CANCELLED_BY_CLIENT]


def test_a_client_moving_their_own_booking_counts_as_their_choice(world):
    place(world, "alice", 60, 0, 9, 17)
    (booked,) = world.store.appointments_for("alice", at(0, 0), at(1, 0))

    moved = world.move_appointment(booked.id, at(1, 14), at(1, 15))

    assert moved.origin is Origin.CLIENT
    assert moved.supersedes == booked.id


def test_locked_appointments_are_never_offered_up(world):
    """Staff-pinned time is immovable, so a request that would need it simply
    fails rather than anyone being asked to move.
    """
    world.set_weekly_availability(PROVIDER, [
        {"weekday": 0, "from": "09:00", "to": "10:00"},
        {"weekday": 1, "from": "09:00", "to": "17:00"},
    ])
    world.store.book_appointment("bob", "s60", at(0, 9), at(0, 10), locked=True)
    ask(world, "alice", 60, 0, 9, 10)

    world.propose()
    plan = next(iter(world.plans.values()), None)

    assert plan is None or not plan.displacements


def test_displacement_can_be_turned_off_entirely(world):
    world.max_displacements = 0
    world.set_weekly_availability(PROVIDER, [
        {"weekday": 0, "from": "09:00", "to": "10:00"},
        {"weekday": 1, "from": "09:00", "to": "17:00"},
    ])
    world.store.book_appointment("bob", "s60", at(0, 9), at(0, 10))
    request = ask(world, "alice", 60, 0, 9, 10)

    world.propose()

    assert world.requests[request.id].status == "on_hold"
    assert not world.approvals


# -- the catalogue ----------------------------------------------------


def test_duration_comes_from_the_service_not_the_asking(world):
    requested = world.submit_request("alice", "s90", [
        {"from": at(0, 9).isoformat(), "to": at(0, 17).isoformat()}
    ])

    assert requested.duration_minutes == 90


def test_asking_for_a_service_that_does_not_exist_is_refused(world):
    with pytest.raises(KeyError):
        world.submit_request("alice", "nope", [
            {"from": at(0, 9).isoformat(), "to": at(0, 17).isoformat()}
        ])


def test_the_solver_measures_gaps_against_what_is_still_on_sale(world):
    """Withdrawing the 90-minute service should stop a 90-minute hole looking
    like something worth preserving.
    """
    world.catalogue.deactivate_service("s90")

    assert world.catalogue.bookable_durations() == (60,)


def test_a_booking_against_a_discontinued_service_still_resolves(world):
    place(world, "alice", 60, 0, 9, 17)
    world.catalogue.deactivate_service("s60")

    (booked,) = world.store.appointments_for("alice", at(0, 0), at(1, 0))
    assert world.catalogue.get_service(booked.service_type_id).name == "Hour"


# -- snapshot ---------------------------------------------------------


def test_the_snapshot_carries_what_every_view_needs(world):
    place(world, "alice", 60, 0, 9, 17)

    snap = world.snapshot()

    assert {c["id"] for c in snap["clients"]} == {"alice", "bob"}
    assert snap["appointments"] and snap["appointments"][0]["origin"] == "client"
    assert "provider-self" in snap["availability"]
    assert snap["requests"][0]["status"] == "placed"
    assert snap["services"]
    assert snap["scheduler"]["urgency_hours"]
    assert snap["log"]
