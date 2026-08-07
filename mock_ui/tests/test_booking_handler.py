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


# -- comparing variants ----------------------------------------------


def test_several_drafts_can_sit_side_by_side(world):
    """Proposing costs nothing — nothing is reserved until one is approved —
    so the provider can try settings and compare rather than being told.
    """
    ask(world, "alice", 0, 9, 17)

    packed = world.propose(alpha=1.0)
    early = world.propose(alpha=0.0)

    assert packed["ran"] and early["ran"]
    drafts = [p for p in world.plans.values() if p.status == "draft"]
    assert len(drafts) == 2
    assert {p.params["alpha"] for p in drafts} == {0.0, 1.0}


def test_each_draft_carries_what_it_was_asked_for_and_what_it_achieved(world):
    ask(world, "alice", 0, 9, 17)

    world.propose(alpha=0.25, max_displacements=2)
    (draft,) = [p for p in world.plans.values() if p.status == "draft"]

    assert draft.params == {"alpha": 0.25, "max_displacements": 2,
                            "allow_chains": False}
    assert draft.metrics["placed"] == 1
    assert "fragmentation_minutes" in draft.metrics
    assert "earliness_minutes" in draft.metrics


def test_trying_settings_does_not_change_the_defaults(world):
    ask(world, "alice", 0, 9, 17)
    world.propose(alpha=1.0, max_displacements=5)

    assert world.alpha == 0.5
    assert world.max_displacements == 1


def test_approving_one_draft_discards_the_others(world):
    ask(world, "alice", 0, 9, 17)
    world.propose(alpha=1.0)
    chosen = world.propose(alpha=0.0)

    result = world.provider_approve(chosen["plan_id"])

    assert result["discarded"] == 1
    assert [p.status for p in world.plans.values() if p.id != chosen["plan_id"]] == ["rejected"]


def test_only_one_plan_may_be_out_with_the_clients(world):
    """Drafts are free; a plan being asked about is not. Two in flight could
    quietly promise the same slot twice.
    """
    ask(world, "alice", 0, 9, 17)
    world.propose()
    world.provider_approve(only_plan(world).id)

    assert world.propose()["ran"] is False


# -- partial authorisation --------------------------------------------


def test_the_provider_can_send_on_only_part_of_a_plan(world):
    ask(world, "alice", 0, 9, 17)
    ask(world, "bob", 0, 9, 17)
    world.propose()
    plan = only_plan(world)
    keep = plan.item_key("booking", plan.placements[0]["request_id"])
    drop = plan.item_key("booking", plan.placements[1]["request_id"])

    world.provider_approve(plan.id, items=[keep])

    assert [a.client_id for a in world.pending_approvals()] == [
        plan.placements[0]["client_id"]
    ]
    dropped = plan.placements[1]["request_id"]
    assert world.requests[dropped].status == "on_hold", "back in the queue, not lost"


def test_approving_a_booking_pulls_in_the_move_it_needs(world):
    """Sending on a booking while holding back the move that frees its slot
    would promise something that cannot happen.
    """
    world.set_weekly_availability(PROVIDER, [
        {"weekday": 0, "from": "09:00", "to": "10:00"},
        {"weekday": 1, "from": "09:00", "to": "17:00"},
    ])
    world.store.book_appointment("bob", "s60", at(0, 9), at(0, 10))
    ask(world, "alice", 0, 9, 10)
    world.propose()
    plan = only_plan(world)
    booking = plan.item_key("booking", plan.placements[0]["request_id"])

    result = world.provider_approve(plan.id, items=[booking])

    move = plan.item_key("reschedule", plan.displacements[0]["appointment_id"])
    assert move in result["approved"], "the move came along with it"
    assert {a.kind for a in world.pending_approvals()} == {"booking", "reschedule"}


def test_a_move_can_be_sent_on_without_the_booking_that_wanted_it(world):
    """The dependency only runs one way."""
    world.set_weekly_availability(PROVIDER, [
        {"weekday": 0, "from": "09:00", "to": "10:00"},
        {"weekday": 1, "from": "09:00", "to": "17:00"},
    ])
    world.store.book_appointment("bob", "s60", at(0, 9), at(0, 10))
    ask(world, "alice", 0, 9, 10)
    world.propose()
    plan = only_plan(world)
    move = plan.item_key("reschedule", plan.displacements[0]["appointment_id"])

    result = world.provider_approve(plan.id, items=[move])

    assert result["approved"] == [move]
    assert {a.kind for a in world.pending_approvals()} == {"reschedule"}


def test_approving_something_that_is_not_in_the_plan_is_refused(world):
    ask(world, "alice", 0, 9, 17)
    world.propose()

    with pytest.raises(ValueError, match="no item"):
        world.provider_approve(only_plan(world).id, items=["p:9999"])


def test_a_discarded_draft_cannot_be_approved(world):
    ask(world, "alice", 0, 9, 17)
    world.propose()
    plan = only_plan(world)
    world.discard_plan(plan.id)

    with pytest.raises(ValueError, match="rejected"):
        world.provider_approve(plan.id)


def test_a_chained_refusal_strands_everything_that_was_waiting_on_it(world):
    """With chains permitted the dependency can be two deep, and the engine
    reports it — the mock does not have to guess from overlapping times.
    """
    world.max_displacements = 2
    world.allow_chains = True
    world.set_weekly_availability(PROVIDER, [
        {"weekday": 0, "from": "09:00", "to": "11:00"},
        {"weekday": 1, "from": "09:00", "to": "10:00"},
    ])
    world.set_weekly_availability(
        "bob", [{"weekday": 0, "from": "09:00", "to": "11:00"}])
    world.add_client("carol", "Carol")
    world.set_weekly_availability(
        "carol", [{"weekday": 1, "from": "09:00", "to": "10:00"}])
    world.store.book_appointment("bob", "s60", at(0, 9), at(0, 10))
    world.store.book_appointment("carol", "s60", at(0, 10), at(0, 11))
    ask(world, "alice", 0, 9, 10)

    world.propose()
    plan = only_plan(world)
    if len(plan.displacements) < 2:
        pytest.skip("this calendar did not need a chain")

    world.provider_approve(plan.id)
    approvals = {a.client_id: a for a in world.pending_approvals()}
    for approval in approvals.values():
        if approval.client_id != "carol":
            world.respond_to_approval(approval.id, accept=True)

    world.respond_to_approval(approvals["carol"].id, accept=False)

    assert not world.store.appointments_for("alice", at(0, 0), at(1, 0)), (
        "alice's booking rested on bob moving, which rested on carol moving"
    )
    assert world.store.appointments_for("bob", at(0, 0), at(1, 0))[0].range.start == at(0, 9)


def test_dependencies_come_from_the_engine_not_from_guesswork(world):
    world.set_weekly_availability(PROVIDER, [
        {"weekday": 0, "from": "09:00", "to": "10:00"},
        {"weekday": 1, "from": "09:00", "to": "17:00"},
    ])
    world.store.book_appointment("bob", "s60", at(0, 9), at(0, 10))
    ask(world, "alice", 0, 9, 10)
    world.propose()

    plan = only_plan(world)
    (placement,) = plan.placements
    (displacement,) = plan.displacements

    assert placement["depends_on"] == [displacement["appointment_id"]]
    assert displacement["depends_on"] == []
