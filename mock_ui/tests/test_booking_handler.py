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
    # These scenarios never look beyond next week, and the horizon is the
    # dominant cost now that a request spans its client's whole availability.
    # Mornings only, ten days out: these scenarios need neither a full day nor
    # three weeks, and both multiply the candidate slots for every request.
    w.policy.horizon_days = 10
    w.catalogue.add_service("s60", "Hour", 60, 8000)
    for who in ("alice", "bob"):
        w.add_client(who, who.title())
        w.set_weekly_availability(
            who, [{"weekday": d, "from": "09:00", "to": "13:00"} for d in range(5)]
        )
    w.set_weekly_availability(
        PROVIDER, [{"weekday": d, "from": "09:00", "to": "13:00"} for d in range(5)]
    )
    return w


def ask(world, client, day, from_hour, to_hour=None, service="s60"):
    """`from_hour` is now the slot they would like; availability decides what
    is actually possible for them."""
    return world.submit_request(client, service, at(day, from_hour).isoformat())


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


def test_a_yes_is_necessary_but_not_sufficient(world):
    """Two gates, not one. The client agreeing makes the change possible; the
    provider settling the plan makes it happen."""
    ask(world, "alice", 0, 9, 17)
    world.propose()
    plan = only_plan(world)
    world.provider_approve(plan.id)
    (approval,) = world.pending_approvals()

    world.respond_to_approval(approval.id, "accept")
    assert not world.store.appointments_for("alice", at(0, 0), at(1, 0)), (
        "agreeing is an answer, not an action"
    )
    assert approval.status == "accepted" and not approval.applied

    world.settle_plan(plan.id, "agreed")

    (booked,) = world.store.appointments_for("alice", at(0, 0), at(1, 0))
    assert booked.origin is Origin.CLIENT
    assert world.requests[approval.request_id].status == "placed"
    assert approval.applied


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

    world.respond_to_approval(approval.id, "decline")

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

    world.respond_to_approval(approval.id, "decline")

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

    world.respond_to_approval(first.id, "accept")
    world.respond_to_approval(second.id, "decline")
    world.settle_plan(only_plan(world).id, "agreed")

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
    # One hour, on one date, is the only thing that will do for alice. A
    # weekly rule would recur, and naming the hour as a *wish* would no longer
    # constrain anything — that is the point of the change.
    world.set_weekly_availability("alice", [])
    world.set_exception("alice", monday(), time(9), time(10), available=True)
    world.store.book_appointment("bob", "s60", at(0, 9), at(0, 10))
    ask(world, "alice", 0, 9, 10)

    world.propose()
    plan = only_plan(world)
    assert plan.displacements, "this scenario needs bob moved"
    world.provider_approve(plan.id)

    booking = next(a for a in world.pending_approvals() if a.kind == "booking")
    move = next(a for a in world.pending_approvals() if a.kind == "reschedule")

    world.respond_to_approval(booking.id, "accept")   # alice is happy
    assert not world.store.appointments_for("alice", at(0, 0), at(1, 0)), (
        "cannot be booked while the slot is still occupied"
    )

    world.respond_to_approval(move.id, "refuse")          # bob will not move
    assert not world.store.appointments_for("alice", at(0, 0), at(1, 0))
    assert world.store.appointments_for("bob", at(0, 0), at(1, 0))[0].range.start == at(0, 9)


def test_a_booking_waiting_on_a_move_is_made_once_the_move_is_agreed(world):
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
    ask(world, "alice", 0, 9, 10)
    world.propose()
    world.provider_approve(only_plan(world).id)

    booking = next(a for a in world.pending_approvals() if a.kind == "booking")
    move = next(a for a in world.pending_approvals() if a.kind == "reschedule")
    world.respond_to_approval(booking.id, "accept")
    world.respond_to_approval(move.id, "accept")
    world.settle_plan(only_plan(world).id, "agreed")

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
        PROVIDER, [{"weekday": d, "from": "09:00", "to": "13:00"} for d in range(5)]
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
    assert "preference_gap_minutes" in draft.metrics


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


# -- planning around what is already promised ------------------------


def test_a_slot_out_with_a_client_is_not_offered_to_anyone_else(world):
    """Re-planning while an answer is outstanding used to be forbidden. It is
    allowed now because the promised slot is described to the engine as taken —
    the engine is a pure function of the world it is given, so no reservation
    mechanism is needed.
    """
    world.set_weekly_availability(
        PROVIDER, [{"weekday": 0, "from": "09:00", "to": "10:00"}])
    ask(world, "alice", 0, 9, 10)
    world.propose()
    world.provider_approve(only_plan(world).id)
    promised = world.pending_approvals()[0].now_start

    ask(world, "bob", 0, 9, 10)
    world.propose()

    fresh = [p for p in world.plans.values() if p.status == "draft"]
    offered = [x["start"] for p in fresh for x in p.placements]
    assert promised not in offered, "the same hour was offered twice"


def test_a_client_still_thinking_is_not_offered_a_second_slot(world):
    ask(world, "alice", 0, 9, 17)
    world.propose()
    world.provider_approve(only_plan(world).id)

    world.propose()

    fresh = [p for p in world.plans.values() if p.status == "draft"]
    assert not [x for p in fresh for x in p.placements
                if x["client_id"] == "alice"]


def test_a_booking_asked_to_move_is_not_asked_about_twice(world):
    world.set_weekly_availability(PROVIDER, [
        {"weekday": 0, "from": "09:00", "to": "10:00"},
        {"weekday": 1, "from": "09:00", "to": "17:00"},
    ])
    # One hour, on one date, is the only thing that will do for alice. A
    # weekly rule would recur, and naming the hour as a *wish* would no longer
    # constrain anything — that is the point of the change.
    world.set_weekly_availability("alice", [])
    world.set_exception("alice", monday(), time(9), time(10), available=True)
    booked = world.store.book_appointment("bob", "s60", at(0, 9), at(0, 10))
    ask(world, "alice", 0, 9, 10)
    world.propose()
    world.provider_approve(only_plan(world).id)

    ask(world, "alice", 0, 9, 10)      # someone else wanting the same hour
    world.propose()

    fresh = [p for p in world.plans.values() if p.status == "draft"]
    assert not [d for p in fresh for d in p.displacements
                if d["appointment_id"] == booked.id]


def test_a_refusal_releases_the_hold(world):
    world.set_weekly_availability(
        PROVIDER, [{"weekday": 0, "from": "09:00", "to": "10:00"}])
    ask(world, "alice", 0, 9, 10)
    world.propose()
    world.provider_approve(only_plan(world).id)
    (approval,) = world.pending_approvals()

    world.respond_to_approval(approval.id, "decline")

    assert world.pending_holds() == [], "nothing is promised any more"


def test_locking_part_of_a_plan_and_re_running_plans_around_it(world):
    """Lock-and-reoptimise falls out of the same mechanism: approving part of
    a plan makes those slots holds, and the next run works around them.
    """
    ask(world, "alice", 0, 9, 17)
    ask(world, "bob", 0, 9, 17)
    world.propose()
    plan = only_plan(world)
    keep = plan.item_key("booking", plan.placements[0]["request_id"])
    locked_at = plan.placements[0]["start"]

    world.provider_approve(plan.id, items=[keep])
    world.propose(alpha=1.0)

    fresh = [p for p in world.plans.values() if p.status == "draft"]
    assert fresh, "re-running is allowed while an answer is outstanding"
    assert locked_at not in [x["start"] for p in fresh for x in p.placements]


# -- partial authorisation --------------------------------------------


def test_the_provider_can_send_on_only_part_of_a_plan(world):
    ask(world, "alice", 0, 9, 17)
    ask(world, "bob", 0, 9, 17)
    world.propose()
    plan = only_plan(world)
    keep = plan.item_key("booking", plan.placements[0]["request_id"])
    drop = plan.item_key("booking", plan.placements[1]["request_id"])

    result = world.provider_approve(plan.id, items=[keep])

    assert result["approved"] == [keep] and drop not in result["approved"]
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
    # One hour, on one date, is the only thing that will do for alice. A
    # weekly rule would recur, and naming the hour as a *wish* would no longer
    # constrain anything — that is the point of the change.
    world.set_weekly_availability("alice", [])
    world.set_exception("alice", monday(), time(9), time(10), available=True)
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
    # One hour, on one date, is the only thing that will do for alice. A
    # weekly rule would recur, and naming the hour as a *wish* would no longer
    # constrain anything — that is the point of the change.
    world.set_weekly_availability("alice", [])
    world.set_exception("alice", monday(), time(9), time(10), available=True)
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
    world.add_client("carol", "Carol")

    # Everyone is free on exactly one date, so the only way to seat alice is
    # bob taking carol's hour and carol going to Tuesday. Weekly patterns would
    # recur and offer an easier answer; a wish would constrain nothing at all.
    world.set_weekly_availability(PROVIDER, [])
    world.set_exception(PROVIDER, monday(), time(9), time(11), available=True)
    world.set_exception(PROVIDER, monday() + timedelta(days=1), time(9), time(10),
                        available=True)
    for who, day, start, end in (
        ("alice", 0, time(9), time(10)),      # only bob's current hour will do
        ("bob", 0, time(9), time(11)),        # can shuffle within Monday
        ("carol", 1, time(9), time(10)),      # only Tuesday will do
    ):
        world.set_weekly_availability(who, [])
        world.set_exception(who, monday() + timedelta(days=day), start, end,
                            available=True)

    world.store.book_appointment("bob", "s60", at(0, 9), at(0, 10))
    world.store.book_appointment("carol", "s60", at(0, 10), at(0, 11))
    ask(world, "alice", 0, 9, 10)

    world.propose()
    plan = only_plan(world)
    assert len(plan.displacements) == 2, (
        "alice needs bob's hour, bob needs carol's, carol must go to Tuesday"
    )

    world.provider_approve(plan.id)
    approvals = {a.client_id: a for a in world.pending_approvals()}
    for approval in approvals.values():
        if approval.client_id != "carol":
            world.respond_to_approval(approval.id, "accept")

    world.respond_to_approval(approvals["carol"].id, "refuse")

    assert not world.store.appointments_for("alice", at(0, 0), at(1, 0)), (
        "alice's booking rested on bob moving, which rested on carol moving"
    )
    assert world.store.appointments_for("bob", at(0, 0), at(1, 0))[0].range.start == at(0, 9)


def test_dependencies_come_from_the_engine_not_from_guesswork(world):
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
    ask(world, "alice", 0, 9, 10)
    world.propose()

    plan = only_plan(world)
    (placement,) = plan.placements
    (displacement,) = plan.displacements

    assert placement["depends_on"] == [displacement["appointment_id"]]
    assert displacement["depends_on"] == []


# -- the planning horizon ---------------------------------------------


def test_the_horizon_crops_what_can_be_booked(world):
    """It bounds the problem, not just the answer: everyone's availability is
    cropped, so a wish beyond the horizon is met as near to it as possible
    rather than granted.
    """
    world.policy.horizon_days = 7
    beyond = datetime.now() + timedelta(days=20)
    world.submit_request("alice", "s60", beyond.isoformat())

    world.propose()

    (placed,) = only_plan(world).placements
    assert placed["start"] < datetime.now() + timedelta(days=7), (
        "placed outside the horizon the provider set"
    )


def test_the_provider_setting_is_what_a_run_uses(world):
    """The bug this pins: propose() had its own default, so the policy was
    never consulted and the setting did nothing at all.
    """
    world.policy.horizon_days = 3
    world.submit_request("alice", "s60",
                         (datetime.now() + timedelta(days=30)).isoformat())

    world.propose()

    plan = next((p for p in world.plans.values() if p.status == "draft"), None)
    if plan and plan.placements:
        assert plan.placements[0]["start"] < datetime.now() + timedelta(days=3)


def test_an_explicit_horizon_still_overrides_the_setting(world):
    world.policy.horizon_days = 30
    world.submit_request("alice", "s60",
                         (datetime.now() + timedelta(days=20)).isoformat())

    world.propose(horizon_days=5)

    plan = next((p for p in world.plans.values() if p.status == "draft"), None)
    if plan and plan.placements:
        assert plan.placements[0]["start"] < datetime.now() + timedelta(days=5)


# -- declining is not refusing ---------------------------------------


def a_move_is_on_the_table(world):
    """Bob holds the only hour alice can use, so the only plan is to move him."""
    world.set_weekly_availability(PROVIDER, [
        {"weekday": 0, "from": "09:00", "to": "10:00"},
        {"weekday": 1, "from": "09:00", "to": "17:00"},
    ])
    world.set_weekly_availability("alice", [])
    world.set_exception("alice", monday(), time(9), time(10), available=True)
    world.set_weekly_availability("bob", [
        {"weekday": d, "from": "09:00", "to": "17:00"} for d in range(5)
    ])
    world.store.book_appointment("bob", "s60", at(0, 9), at(0, 10))
    ask(world, "alice", 0, 9, 10)
    world.propose()
    plan = only_plan(world)
    assert plan.displacements, "this scenario needs bob moved"
    world.provider_approve(plan.id)
    return next(a for a in world.pending_approvals() if a.kind == "reschedule")


def test_declining_a_move_blocks_the_slot_but_not_the_appointment(world):
    """"Not that time" is the middle answer, and the reason it exists: the
    scheduler may come back with somewhere else."""
    move = a_move_is_on_the_table(world)
    offered = move.now_start

    world.respond_to_approval(move.id, "decline")

    assert move.appointment_id not in world.immovable, "still movable"
    same_day = world.availability_segments(
        "bob", offered.date(), offered.date() + timedelta(days=1))
    assert not any(s.start <= offered < s.end for s in same_day), (
        "the one thing a decline does say: not that slot, that day"
    )
    # And it says nothing beyond that day.
    next_week = offered + timedelta(days=7)
    later = world.availability_segments(
        "bob", next_week.date(), next_week.date() + timedelta(days=1))
    assert any(s.start <= next_week < s.end for s in later)


def test_refusing_a_move_takes_the_appointment_off_the_table_for_good(world):
    move = a_move_is_on_the_table(world)

    world.respond_to_approval(move.id, "refuse")

    assert move.appointment_id in world.immovable


def test_a_declined_move_can_be_proposed_somewhere_else(world):
    """The failure the third answer was added to prevent: one "not Tuesday at
    three" used to remove an appointment from consideration permanently."""
    move = a_move_is_on_the_table(world)
    world.respond_to_approval(move.id, "decline")

    world.propose()
    again = [p for p in world.plans.values() if p.status == "draft"]

    moves = [d for p in again for d in p.displacements]
    assert moves, "a declined client is still available to be moved elsewhere"
    assert all(d["now_start"] != move.now_start for d in moves), (
        "but never back to the slot they turned down"
    )


def test_a_refused_move_is_never_proposed_again(world):
    move = a_move_is_on_the_table(world)
    world.respond_to_approval(move.id, "refuse")

    world.propose()

    assert not [d for p in world.plans.values() if p.status == "draft"
                for d in p.displacements], "they said not at all"


def test_an_offer_can_be_declined_but_not_refused(world):
    """There is nothing to refuse to move — a booking has only two answers."""
    ask(world, "alice", 0, 9, 17)
    world.propose()
    world.provider_approve(only_plan(world).id)
    (offer,) = world.pending_approvals()
    assert offer.kind == "booking"

    with pytest.raises(ValueError):
        world.respond_to_approval(offer.id, "refuse")
    assert offer.status == "pending", "a rejected answer must not half-apply"


def test_an_unknown_answer_is_refused_rather_than_guessed(world):
    ask(world, "alice", 0, 9, 17)
    world.propose()
    world.provider_approve(only_plan(world).id)
    (approval,) = world.pending_approvals()

    with pytest.raises(ValueError):
        world.respond_to_approval(approval.id, "maybe")
    assert approval.status == "pending"


def test_an_offer_is_taken_back_when_the_move_it_needed_falls_through(world):
    """Nobody should be asked to confirm a slot that cannot be freed."""
    move = a_move_is_on_the_table(world)
    offer = next(a for a in world.pending_approvals() if a.kind == "booking")

    world.respond_to_approval(move.id, "decline")

    assert offer.status == "withdrawn"
    assert world.pending_approvals() == [], "nothing left hanging over the plan"
    assert world.requests[offer.request_id].status == "on_hold", (
        "and the request is back in the queue rather than lost"
    )


# -- the provider settles what the answers add up to -----------------


def two_asks_out(world):
    """Alice and Bob each offered a slot, neither answered yet."""
    ask(world, "alice", 0, 9, 17)
    ask(world, "bob", 0, 9, 17)
    world.propose()
    plan = only_plan(world)
    world.provider_approve(plan.id)
    first, second = sorted(world.pending_approvals(), key=lambda a: a.client_id)
    return plan, first, second


def test_applying_what_is_agreed_leaves_the_rest_outstanding(world):
    plan, alice, bob = two_asks_out(world)
    world.respond_to_approval(alice.id, "accept")

    result = world.settle_plan(plan.id, "agreed")

    assert result == {"applied": 1, "withdrawn": 0, "waiting": 1}
    assert world.store.appointments_for("alice", at(0, 0), at(1, 0))
    assert bob.status == "pending", "still his to answer"
    assert plan.status == "awaiting_clients"


def test_a_late_yes_is_picked_up_by_a_second_settlement(world):
    plan, alice, bob = two_asks_out(world)
    world.respond_to_approval(alice.id, "accept")
    world.settle_plan(plan.id, "agreed")

    world.respond_to_approval(bob.id, "accept")
    world.settle_plan(plan.id, "agreed")

    assert world.store.appointments_for("bob", at(0, 0), at(1, 0))
    assert plan.status == "applied"


def test_the_provider_can_stop_waiting_and_take_what_there_is(world):
    plan, alice, bob = two_asks_out(world)
    world.respond_to_approval(alice.id, "accept")

    result = world.settle_plan(plan.id, "agreed_only")

    assert result["applied"] == 1 and result["withdrawn"] == 1
    assert world.store.appointments_for("alice", at(0, 0), at(1, 0))
    assert bob.status == "withdrawn"
    assert world.requests[bob.request_id].status == "on_hold", (
        "a withdrawn ask puts the work back in the queue rather than losing it"
    )
    assert plan.status == "applied"


def test_reoptimising_writes_none_of_it_down_and_runs_again(world):
    plan, alice, bob = two_asks_out(world)
    world.respond_to_approval(alice.id, "accept")

    result = world.settle_plan(plan.id, "reoptimise")

    assert result["applied"] == 0
    assert not world.store.appointments_for("alice", at(0, 0), at(1, 0)), (
        "an accepted answer is not a booking; rejecting the plan drops it"
    )
    assert alice.status == bob.status == "withdrawn"
    assert plan.status == "rejected"
    assert [p for p in world.plans.values() if p.status == "draft"], "ran again"


def test_an_agreed_slot_is_not_offered_to_somebody_else_meanwhile(world):
    """The hold has to survive the answer. Between "yes" and the provider
    writing it down, that hour is neither free nor booked."""
    plan, alice, bob = two_asks_out(world)
    world.respond_to_approval(alice.id, "accept")
    agreed = alice.now_start

    ask(world, "bob", 0, 9, 17)
    world.propose()

    fresh = [p for p in world.plans.values() if p.status == "draft"]
    assert not any(x["start"] == agreed for p in fresh for x in p.placements), (
        "that hour is spoken for"
    )


def test_a_plan_nobody_has_seen_cannot_be_settled(world):
    ask(world, "alice", 0, 9, 17)
    world.propose()

    with pytest.raises(ValueError):
        world.settle_plan(only_plan(world).id, "agreed")


def test_an_unknown_settlement_is_refused_rather_than_guessed(world):
    plan, alice, _ = two_asks_out(world)

    with pytest.raises(ValueError):
        world.settle_plan(plan.id, "apply-everything")
    assert plan.status == "awaiting_clients"


def test_a_chain_is_applied_in_whatever_order_it_unblocks(world):
    """Applying is a loop rather than a sort: writing one move down can free
    the slot another was waiting for."""
    move = a_move_is_on_the_table(world)
    booking = next(a for a in world.pending_approvals() if a.kind == "booking")
    assert move.appointment_id in booking.depends_on

    world.respond_to_approval(booking.id, "accept")   # the dependent one first
    world.respond_to_approval(move.id, "accept")
    world.settle_plan(booking.plan_id, "agreed")

    assert world.store.appointments_for("alice", at(0, 0), at(1, 0))
    assert booking.applied and move.applied


def test_a_booking_shows_whether_it_is_settled_asked_about_or_agreed(world):
    move = a_move_is_on_the_table(world)

    def shown():
        return next(a for a in world.snapshot()["appointments"]
                    if a["id"] == move.appointment_id)["pending"]

    assert shown()["state"] == "asked"

    world.respond_to_approval(move.id, "accept")
    assert shown()["state"] == "agreed", "yes, but not yet written down"

    world.settle_plan(move.plan_id, "agreed")
    assert next(a for a in world.snapshot()["appointments"]
                if a["client_id"] == "bob" and a["status"] == "booked"
                )["pending"] is None


def test_a_client_cannot_start_their_own_move_while_answering_ours(world):
    """Both at once would leave the replacement trying to cancel an
    appointment the accepted move had already superseded."""
    move = a_move_is_on_the_table(world)

    with pytest.raises(ValueError):
        world.request_reschedule(
            move.appointment_id, at(3, 11).isoformat(), release_slot=False)

    # Once they have said no, it is theirs to move again.
    world.respond_to_approval(move.id, "decline")
    request = world.request_reschedule(
        move.appointment_id, at(3, 11).isoformat(), release_slot=False)
    assert request.replaces_appointment_id == move.appointment_id
