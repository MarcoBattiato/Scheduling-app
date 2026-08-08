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
    # These scenarios never look beyond next week, and the horizon is the
    # dominant cost now that a request spans its client's whole availability.
    # Mornings only, ten days out: these scenarios need neither a full day nor
    # three weeks, and both multiply the candidate slots for every request.
    w.policy.horizon_days = 10
    w.catalogue.add_service("s60", "Hour", 60, 8000)
    w.catalogue.add_service("s90", "Long", 90, 11000)
    w.add_client("alice", "Alice")
    w.add_client("bob", "Bob")
    w.set_weekly_availability(
        PROVIDER, [{"weekday": d, "from": "09:00", "to": "13:00"} for d in range(5)]
    )
    for client in ("alice", "bob"):
        w.set_weekly_availability(
            client, [{"weekday": d, "from": "09:00", "to": "13:00"} for d in range(5)]
        )
    return w


def ask(world, client, duration, day, from_hour, to_hour=None):
    """`from_hour` is now the slot they would like; availability decides what
    is actually possible for them."""
    return world.submit_request(client, f"s{duration}", at(day, from_hour).isoformat())


def place(world, client, duration, day, from_hour, to_hour):
    """Drive a request all the way into the calendar."""
    request = ask(world, client, duration, day, from_hour, to_hour)
    world.propose()
    plan = next(p for p in world.plans.values() if p.status == "draft")
    world.provider_approve(plan.id)
    for approval in world.pending_approvals(plan.id):
        world.respond_to_approval(approval.id, "accept")
    # Agreeing is an answer; the provider still has to say to write it down.
    world.settle_plan(plan.id, "agreed")
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
    # One hour, on one date, is the only thing that will do for alice. A
    # weekly rule would recur, and naming the hour as a *wish* would no longer
    # constrain anything — that is the point of the change.
    world.set_weekly_availability("alice", [])
    world.set_exception("alice", monday(), time(9), time(10), available=True)
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
    # One hour, on one date, is the only thing that will do for alice. A
    # weekly rule would recur, and naming the hour as a *wish* would no longer
    # constrain anything — that is the point of the change.
    world.set_weekly_availability("alice", [])
    world.set_exception("alice", monday(), time(9), time(10), available=True)
    world.store.book_appointment("bob", "s60", at(0, 9), at(0, 10))
    request = ask(world, "alice", 60, 0, 9, 10)

    world.propose()

    assert world.requests[request.id].status == "on_hold"
    assert not world.approvals


# -- the catalogue ----------------------------------------------------


def test_duration_comes_from_the_service_not_the_asking(world):
    requested = world.submit_request("alice", "s90", at(0, 9).isoformat())

    assert requested.duration_minutes == 90


def test_asking_for_a_service_that_does_not_exist_is_refused(world):
    with pytest.raises(KeyError):
        world.submit_request("alice", "nope", at(0, 9).isoformat())


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


# -- single-date exceptions -------------------------------------------


def test_an_exception_changes_one_date_without_touching_the_pattern(world):
    """The weekly grid says what a normal week looks like; an exception is for
    the week that is not normal.
    """
    away = monday() + timedelta(days=1)
    world.set_exception("alice", away, time(9), time(12), available=False)

    that_day = world.availability_segments("alice", away, away + timedelta(days=1))
    assert not any(s.start.hour < 12 for s in that_day), "morning is gone"

    a_week_later = away + timedelta(days=7)
    later = world.availability_segments(
        "alice", a_week_later, a_week_later + timedelta(days=1))
    assert any(s.start.hour == 9 for s in later), "the pattern is untouched"


def test_an_exception_can_add_time_outside_the_usual_week(world):
    saturday = monday() + timedelta(days=5)

    world.set_exception("alice", saturday, time(10), time(14), available=True)

    segments = world.availability_segments(
        "alice", saturday, saturday + timedelta(days=1))
    assert [(s.start.hour, s.end.hour) for s in segments] == [(10, 14)]


def test_exceptions_are_reported_for_every_calendar(world):
    world.set_exception("alice", monday(), time(9), time(10), available=False)

    snap = world.snapshot()
    assert snap["exceptions"]["alice"]
    assert "provider-self" in snap["exceptions"]


# -- what the calendar needs to draw -----------------------------------


def test_appointments_carry_enough_to_be_worth_hovering_over(world):
    place(world, "alice", 60, 0, 9, 17)

    (shown,) = [a for a in world.snapshot()["appointments"] if a["status"] == "booked"]
    assert shown["service"] == "Hour"
    assert shown["price"] == 8000
    assert "origin" in shown and "status" in shown


def test_a_client_summary_counts_what_actually_happened(world):
    place(world, "alice", 60, 0, 9, 17)
    (booked,) = world.store.appointments_for("alice", at(0, 0), at(1, 0))
    world.mark_attendance(booked.id, attended=False)
    place(world, "alice", 60, 1, 9, 17)
    (second,) = world.store.appointments_for("alice", at(1, 0), at(2, 0))
    world.cancel_appointment(second.id)

    summary = world.client_summary("alice")

    assert summary["no_show"] == 1
    assert summary["cancelled"] == 1
    assert summary["completed"] == 0


def test_a_summary_counts_moves_the_clinic_imposed(world):
    world.set_weekly_availability(PROVIDER, [
        {"weekday": 0, "from": "09:00", "to": "10:00"},
        {"weekday": 1, "from": "09:00", "to": "17:00"},
    ])
    # One hour, on one date, is the only thing that will do for alice. A
    # weekly rule would recur, and naming the hour as a *wish* would no longer
    # constrain anything — that is the point of the change.
    world.set_weekly_availability("alice", [])
    world.set_exception("alice", monday(), time(9), time(10), available=True)
    world.store.book_appointment("bob", "s60", at(0, 9), at(0, 10))
    ask(world, "alice", 60, 0, 9, 10)
    world.propose()
    plan = next(p for p in world.plans.values() if p.status == "draft")
    world.provider_approve(plan.id)
    for approval in world.pending_approvals(plan.id):
        world.respond_to_approval(approval.id, "accept")
    world.settle_plan(plan.id, "agreed")

    assert world.client_summary("bob")["moved_by_us"] == 1


# -- adding clients ---------------------------------------------------


def test_a_new_client_can_be_given_the_provider_hours(world):
    """A client with no availability can be booked but never *moved*, so a
    calendar populated with such clients would never exercise displacement.
    """
    world.add_client("dana", "Dana", mirror_provider=True)

    weekly = world.snapshot()["weekly"]["dana"]
    assert weekly == world.snapshot()["weekly"][PROVIDER]


def test_a_new_client_can_be_left_without_availability(world):
    world.add_client("dana", "Dana", mirror_provider=False)

    assert world.snapshot()["weekly"]["dana"] == []


def test_client_ids_are_not_reused(world):
    with pytest.raises(ValueError, match="already a client"):
        world.add_client("alice", "Another Alice")


def test_a_client_needs_an_id(world):
    with pytest.raises(ValueError, match="needs an id"):
        world.add_client("  ", "Nameless")


def test_a_new_client_can_be_scheduled_straight_away(world):
    world.add_client("dana", "Dana", mirror_provider=True)

    place(world, "dana", 60, 0, 9, 17)

    assert world.store.appointments_for("dana", at(0, 0), at(1, 0))


# -- undoing a single-date override -----------------------------------


def test_an_exception_can_be_cleared_again(world):
    away = monday() + timedelta(days=1)
    world.set_exception("alice", away, time(9), time(12), available=False)
    assert world.snapshot()["exceptions"]["alice"]

    world.clear_exception("alice", away, time(9), time(12), was_available=False)

    assert world.snapshot()["exceptions"]["alice"] == []
    restored = world.availability_segments("alice", away, away + timedelta(days=1))
    assert any(s.start.hour == 9 for s in restored), "the pattern is back"


def test_clearing_an_added_exception_removes_it_too(world):
    saturday = monday() + timedelta(days=5)
    world.set_exception("alice", saturday, time(10), time(14), available=True)

    world.clear_exception("alice", saturday, time(10), time(14), was_available=True)

    assert world.snapshot()["exceptions"]["alice"] == []
    assert world.availability_segments(
        "alice", saturday, saturday + timedelta(days=1)) == []


# -- persistence ------------------------------------------------------


def test_a_saved_session_resumes_the_workflow_not_just_the_calendar(world, tmp_path):
    """A restored session with the bookings but an empty queue — no pending
    request, no draft to approve, nobody waiting to answer — would be missing
    most of what there is to play with.
    """
    from mock_ui import persistence

    ask(world, "alice", 60, 0, 9, 17)
    world.propose()
    plan = next(p for p in world.plans.values() if p.status == "draft")
    world.provider_approve(plan.id)
    world.policy.urgency_hours = 6
    world.immovable.add(99)

    path = tmp_path / "session.json"
    persistence.save(world, path)
    back = persistence.load(path)

    assert len(back.requests) == len(world.requests)
    assert len(back.plans) == len(world.plans)
    assert len(back.approvals) == len(world.approvals)
    assert back.policy.urgency_hours == 6
    assert 99 in back.immovable
    assert back.last_run is not None


def test_a_resumed_session_can_be_carried_on(world, tmp_path):
    from mock_ui import persistence

    ask(world, "alice", 60, 0, 9, 17)
    world.propose()
    plan = next(p for p in world.plans.values() if p.status == "draft")
    world.provider_approve(plan.id)

    path = tmp_path / "session.json"
    persistence.save(world, path)
    back = persistence.load(path)

    (approval,) = back.pending_approvals()
    back.respond_to_approval(approval.id, "accept")
    back.settle_plan(approval.plan_id, "agreed")

    assert back.store.appointments_for("alice", at(0, 0), at(1, 0))


def test_ids_issued_after_a_reload_do_not_collide(world, tmp_path):
    from mock_ui import persistence

    ask(world, "alice", 60, 0, 9, 17)
    world.propose()
    path = tmp_path / "session.json"
    persistence.save(world, path)

    back = persistence.load(path)
    fresh = back.submit_request("alice", "s60", at(1, 9).isoformat())

    assert fresh.id not in {r.id for r in world.requests.values()} | set(world.plans)


# -- a client asking to be moved --------------------------------------


def test_a_client_can_ask_to_move_without_naming_a_slot(world):
    """Different from moving themselves: they say only "not this time" and the
    scheduler looks, so it comes back as an offer they can accept."""
    place(world, "alice", 60, 0, 9, 17)
    (booked,) = world.store.appointments_for("alice", at(0, 0), at(1, 0))

    request = world.request_reschedule(
        booked.id, at(2, 11).isoformat(), release_slot=False)

    assert request.replaces_appointment_id == booked.id
    assert request.preferred_start == at(2, 11)
    assert world.store.get_appointment(booked.id).occupies_slot, (
        "they keep the slot until there is somewhere to go"
    )


def test_keeping_the_slot_means_it_goes_only_when_the_new_one_is_booked(world):
    place(world, "alice", 60, 0, 9, 17)
    (booked,) = world.store.appointments_for("alice", at(0, 0), at(1, 0))
    world.request_reschedule(booked.id, at(2, 11).isoformat(), release_slot=False)

    world.propose()
    plan = next(p for p in world.plans.values() if p.status == "draft")
    world.provider_approve(plan.id)
    for approval in world.pending_approvals(plan.id):
        world.respond_to_approval(approval.id, "accept")
    world.settle_plan(plan.id, "agreed")

    old = world.store.get_appointment(booked.id)
    assert old.status is AppointmentStatus.CANCELLED_BY_CLIENT
    live = [a for a in world.store.appointment_history("alice", at(0, 0), at(7, 0))
            if a.occupies_slot]
    assert len(live) == 1, "one booking throughout, never none and never two"
    assert live[0].range.start != booked.range.start
    assert live[0].origin is Origin.CLIENT, "they asked to move, so it is their choice"


def test_giving_the_slot_up_frees_it_at_once(world):
    place(world, "alice", 60, 0, 9, 17)
    (booked,) = world.store.appointments_for("alice", at(0, 0), at(1, 0))

    world.request_reschedule(booked.id, at(2, 11).isoformat(), release_slot=True)

    assert world.store.get_appointment(booked.id).is_cancelled
    assert not world.store.appointments_for("alice", at(0, 0), at(1, 0))


def test_a_booking_on_its_way_out_is_not_also_offered_to_somebody_else(world):
    """It would be absurd to ask a client to move a booking they have already
    asked to move."""
    place(world, "alice", 60, 0, 9, 17)
    (booked,) = world.store.appointments_for("alice", at(0, 0), at(1, 0))
    world.request_reschedule(booked.id, at(2, 11).isoformat(), release_slot=False)

    ask(world, "bob", 60, 0, 9, 17)
    world.propose()

    moves = [d for p in world.plans.values() if p.status == "draft"
             for d in p.displacements]
    assert all(d["appointment_id"] != booked.id for d in moves)


def test_asking_twice_to_move_the_same_booking_is_refused(world):
    place(world, "alice", 60, 0, 9, 17)
    (booked,) = world.store.appointments_for("alice", at(0, 0), at(1, 0))
    world.request_reschedule(booked.id, at(2, 11).isoformat(), release_slot=False)

    with pytest.raises(ValueError):
        world.request_reschedule(booked.id, at(3, 11).isoformat(), release_slot=False)


def test_a_cancelled_booking_cannot_be_rescheduled(world):
    place(world, "alice", 60, 0, 9, 17)
    (booked,) = world.store.appointments_for("alice", at(0, 0), at(1, 0))
    world.cancel_appointment(booked.id)

    with pytest.raises(ValueError):
        world.request_reschedule(booked.id, at(2, 11).isoformat(), release_slot=False)


def test_a_client_can_see_whether_their_slot_is_settled(world):
    """The question a client actually has: is this happening or not?"""
    place(world, "alice", 60, 0, 9, 17)
    (booked,) = world.store.appointments_for("alice", at(0, 0), at(1, 0))

    settled = next(a for a in world.snapshot()["appointments"] if a["id"] == booked.id)
    assert settled["pending"] is None

    world.request_reschedule(booked.id, at(2, 11).isoformat(), release_slot=False)
    moving = next(a for a in world.snapshot()["appointments"] if a["id"] == booked.id)
    assert moving["pending"]["state"] == "moving"
