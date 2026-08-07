"""Propose, provider approves, clients answer.

Nothing reaches the calendar until the client whose time it is has agreed, and
each answer stands on its own.
"""
from datetime import date, datetime, time, timedelta

import pytest
from calendar_store import Origin

from mock_ui.policy import SchedulingPolicy, due, previous_occurrence
from mock_ui.state import PROVIDER, World


def monday() -> date:
    today = date.today()
    return today + timedelta(days=(7 - today.weekday()) % 7 or 7)


def at(day_offset: int, hour: int, minute: int = 0) -> datetime:
    return datetime.combine(monday() + timedelta(days=day_offset), time(hour, minute))


@pytest.fixture
def world() -> World:
    w = World()
    w.catalogue.add_service("s60", "Hour", 60, 8000)
    for who in ("alice", "bob"):
        w.add_client(who, who.title())
        w.set_weekly_availability(
            who, [{"weekday": d, "from": "09:00", "to": "17:00"} for d in range(5)]
        )
    w.set_weekly_availability(
        PROVIDER, [{"weekday": d, "from": "09:00", "to": "17:00"} for d in range(5)]
    )
    return w


def ask(world, client, day, from_hour, to_hour, service="s60"):
    return world.submit_request(client, service, [
        {"from": at(day, from_hour).isoformat(), "to": at(day, to_hour).isoformat()}
    ])


def only_plan(world):
    return next(iter(world.plans.values()))


# -- the scheduler does not fire on its own -------------------------


def test_submitting_a_request_does_not_run_the_scheduler(world):
    """A request arriving is not a reason to re-plan the week."""
    ask(world, "alice", 0, 9, 17)

    assert not world.plans
    assert not world.store._appointments


def test_a_proposal_writes_nothing_until_everyone_has_agreed(world):
    ask(world, "alice", 0, 9, 17)

    world.propose()

    assert only_plan(world).status == "draft"
    assert not world.store._appointments, "the calendar is untouched"
    assert not world.approvals, "clients are not asked until the provider agrees"


def test_the_provider_approving_is_what_asks_the_clients(world):
    ask(world, "alice", 0, 9, 17)
    world.propose()

    world.provider_approve(only_plan(world).id)

    assert only_plan(world).status == "awaiting_clients"
    assert [a.client_id for a in world.pending_approvals()] == ["alice"]
    assert not world.store._appointments, "still nothing booked"


def test_only_a_client_saying_yes_puts_it_in_the_calendar(world):
    ask(world, "alice", 0, 9, 17)
    world.propose()
    world.provider_approve(only_plan(world).id)
    (approval,) = world.pending_approvals()

    world.respond_to_approval(approval.id, accept=True)

    (booked,) = world.store.appointments_for("alice", at(0, 0), at(1, 0))
    assert booked.origin is Origin.CLIENT
    assert world.requests[approval.request_id].status == "placed"


def test_the_provider_rejecting_sends_it_back_to_the_queue(world):
    ask(world, "alice", 0, 9, 17)
    world.propose()
    plan = only_plan(world)

    world.provider_reject(plan.id)

    assert plan.status == "rejected"
    assert not world.store._appointments
    assert not world.approvals


# -- refusals ---------------------------------------------------------


def test_a_refused_slot_becomes_a_hole_in_that_client_s_availability(world):
    """Only on that date: saying no to next Tuesday at three says nothing
    about Tuesdays in general (engine SPEC.md §9).
    """
    ask(world, "alice", 0, 9, 17)
    world.propose()
    world.provider_approve(only_plan(world).id)
    (approval,) = world.pending_approvals()
    offered = approval.now_start

    world.respond_to_approval(approval.id, accept=False)

    same_day = world.availability_segments(
        "alice", offered.date(), offered.date() + timedelta(days=1)
    )
    assert not any(s.start <= offered < s.end for s in same_day), "that slot is blocked"

    a_week_later = offered + timedelta(days=7)
    later = world.availability_segments(
        "alice", a_week_later.date(), a_week_later.date() + timedelta(days=1)
    )
    assert any(s.start <= a_week_later < s.end for s in later), "same weekday untouched"


def test_a_refusal_puts_the_request_back_on_hold_rather_than_losing_it(world):
    request = ask(world, "alice", 0, 9, 17)
    world.propose()
    world.provider_approve(only_plan(world).id)
    (approval,) = world.pending_approvals()

    world.respond_to_approval(approval.id, accept=False)

    assert world.requests[request.id].status == "on_hold"
    assert not world.store._appointments


# -- independent application -----------------------------------------


def test_one_client_declining_does_not_undo_another_s_agreement(world):
    """Engine SPEC.md §7.4 — each confirmed move is applied on its own."""
    ask(world, "alice", 0, 9, 17)
    ask(world, "bob", 0, 9, 17)
    world.propose()
    world.provider_approve(only_plan(world).id)
    first, second = sorted(world.pending_approvals(), key=lambda a: a.client_id)

    world.respond_to_approval(first.id, accept=True)
    world.respond_to_approval(second.id, accept=False)

    assert world.store.appointments_for(first.client_id, at(0, 0), at(1, 0))
    assert not world.store.appointments_for(second.client_id, at(0, 0), at(1, 0))


def test_a_booking_that_needed_a_move_does_not_happen_if_the_move_is_refused(world):
    """The one thing that cannot be independent: a slot that was only free
    because somebody was going to vacate it.
    """
    world.set_weekly_availability(PROVIDER, [
        {"weekday": 0, "from": "09:00", "to": "10:00"},
        {"weekday": 1, "from": "09:00", "to": "17:00"},
    ])
    world.store.book_appointment("bob", "s60", at(0, 9), at(0, 10))
    ask(world, "alice", 0, 9, 10)

    world.propose()
    plan = only_plan(world)
    assert plan.displacements, "this scenario needs bob moved"
    world.provider_approve(plan.id)

    booking = next(a for a in world.pending_approvals() if a.kind == "booking")
    move = next(a for a in world.pending_approvals() if a.kind == "reschedule")

    world.respond_to_approval(booking.id, accept=True)   # alice is happy
    assert not world.store.appointments_for("alice", at(0, 0), at(1, 0)), (
        "cannot be booked while the slot is still occupied"
    )

    world.respond_to_approval(move.id, accept=False)     # bob will not move
    assert not world.store.appointments_for("alice", at(0, 0), at(1, 0))
    assert world.store.appointments_for("bob", at(0, 0), at(1, 0))[0].range.start == at(0, 9)


def test_a_booking_waiting_on_a_move_is_made_once_the_move_is_agreed(world):
    world.set_weekly_availability(PROVIDER, [
        {"weekday": 0, "from": "09:00", "to": "10:00"},
        {"weekday": 1, "from": "09:00", "to": "17:00"},
    ])
    world.store.book_appointment("bob", "s60", at(0, 9), at(0, 10))
    ask(world, "alice", 0, 9, 10)
    world.propose()
    world.provider_approve(only_plan(world).id)

    booking = next(a for a in world.pending_approvals() if a.kind == "booking")
    move = next(a for a in world.pending_approvals() if a.kind == "reschedule")
    world.respond_to_approval(booking.id, accept=True)
    world.respond_to_approval(move.id, accept=True)

    assert world.store.appointments_for("alice", at(0, 0), at(1, 0))
    moved = [a for a in world.store.appointment_history("bob", at(0, 0), at(5, 0))
             if a.occupies_slot]
    assert moved[0].origin is Origin.DISPLACED


# -- ON_HOLD ----------------------------------------------------------


def test_an_unplaceable_request_is_parked_rather_than_left_pending(world):
    world.set_weekly_availability(PROVIDER, [])      # provider works never
    request = ask(world, "alice", 0, 9, 17)

    world.propose()

    assert world.requests[request.id].status == "on_hold"


def test_a_parked_request_stops_demanding_runs_of_its_own(world):
    """The point of ON_HOLD: an unplaceable request whose date is approaching
    would otherwise satisfy the urgency trigger on every single tick.
    """
    world.set_weekly_availability(PROVIDER, [])
    ask(world, "alice", 0, 9, 17)
    world.propose()                                   # parks it
    world.last_run = datetime.now()

    assert world.tick() is None


def test_a_parked_request_is_still_reconsidered_by_runs_that_happen_anyway(world):
    """Parked is not abandoned — being parked stops it *triggering* a run, it
    does not exclude it from one.
    """
    world.set_weekly_availability(PROVIDER, [])
    request = ask(world, "alice", 0, 9, 17)
    world.propose()
    assert world.requests[request.id].status == "on_hold"

    world.set_weekly_availability(
        PROVIDER, [{"weekday": d, "from": "09:00", "to": "17:00"} for d in range(5)]
    )
    world.propose()

    assert only_plan(world).placements, "it was picked up again"


# -- trigger policy ---------------------------------------------------


def test_nothing_fires_when_the_provider_has_turned_it_off():
    policy = SchedulingPolicy(auto_run=False)
    assert due(policy, datetime(2026, 5, 4, 9), None, [datetime(2026, 5, 4, 10)], True) is None


def test_the_weekly_run_fires_once_per_occurrence():
    policy = SchedulingPolicy(weekly_runs=[(0, time(8, 0))], urgency_hours=0)
    monday_nine = datetime(2026, 5, 4, 9, 0)

    assert due(policy, monday_nine, None, [], False).reason == "weekly"
    just_after = datetime(2026, 5, 4, 8, 1)
    assert due(policy, monday_nine, just_after, [], False) is None, "already ran"


def test_an_imminent_request_earns_a_run_of_its_own():
    policy = SchedulingPolicy(weekly_runs=[], urgency_hours=24)
    now = datetime(2026, 5, 6, 9, 0)

    soon = due(policy, now, now, [now + timedelta(hours=3)], True)
    assert soon.reason == "urgent"

    far = due(policy, now, now, [now + timedelta(days=5)], True)
    assert far is None


def test_unsatisfied_work_is_retried_after_the_cooldown():
    policy = SchedulingPolicy(weekly_runs=[], urgency_hours=0, retry_after_minutes=60)
    now = datetime(2026, 5, 6, 12, 0)

    assert due(policy, now, now - timedelta(minutes=10), [], True) is None
    assert due(policy, now, now - timedelta(minutes=90), [], True).reason == "retry"
    assert due(policy, now, now - timedelta(minutes=90), [], False) is None


def test_previous_occurrence_looks_backwards_never_forwards():
    wednesday = datetime(2026, 5, 6, 7, 0)          # before 08:00

    assert previous_occurrence(wednesday, 2, time(8, 0)) == datetime(2026, 4, 29, 8, 0)
    assert previous_occurrence(wednesday, 2, time(6, 0)) == datetime(2026, 5, 6, 6, 0)


def test_one_proposal_at_a_time(world):
    ask(world, "alice", 0, 9, 17)
    world.propose()

    second = world.propose()

    assert second["ran"] is False
    assert "already in flight" in second["reason"]
