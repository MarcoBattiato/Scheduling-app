"""A booking that needs more than one person to move, and a request
withdrawn while its offer is still out.

Both were found by exercising the negotiation with scenarios the example
calendars never produce. The examples only ever need one move per booking, and
with one move "a move this rests on has happened" and "every move this rests on
has happened" are the same statement — which is how writing a booking down
after the *first* of two moves went unnoticed. It booked two people into the
same hour in three of the six orders the answers can arrive in.
"""
from datetime import datetime, timedelta
from itertools import permutations

import pytest

from mock_ui import scenarios as sc
from mock_ui.state import PROVIDER, World


def two_moves_for_one_booking():
    """Mondays and Tuesdays 09:00-11:00. Lena has Monday 09:00 and Sam Monday
    10:00; both could do Tuesday. Tara can only do Monday morning and wants two
    hours — so she fits only if *both* of them move."""
    world = World()
    world.catalogue.add_service("s60", "Session", 60, 8000)
    world.catalogue.add_service("s120", "Double session", 120, 15000)
    world.set_weekly_availability(PROVIDER, sc._hours((0, 1), "09:00", "11:00"))
    for client_id in ("lena", "sam", "tara"):
        world.add_client(client_id, client_id.title())
    world.set_weekly_availability("lena", sc._hours((0, 1), "09:00", "11:00"))
    world.set_weekly_availability("sam", sc._hours((0, 1), "09:00", "11:00"))
    world.set_weekly_availability("tara", sc._hours((0,), "09:00", "11:00"))

    monday = sc.next_monday()
    world.policy.horizon_days = sc._horizon_reaching(monday)
    world.policy.auto_run = False
    sc._close_the_run_up(world, monday)
    sc._book(world, monday, [("lena", 0, 9), ("sam", 0, 10)], service="s60")
    request = world.submit_request("tara", "s120", sc._at(monday, 0, 9).isoformat())

    plan_id = world.propose(max_displacements=2)["plan_id"]
    plan = world.plans[plan_id]
    tara = next(x for x in plan.placements if x["client_id"] == "tara")
    assert len(tara["depends_on"]) == 2, "the scenario must need both moves"
    world.provider_approve(plan_id)
    asks = {a.client_id: a for a in world.approvals.values() if a.plan_id == plan_id}
    return world, plan_id, asks, request, monday


def live(world, monday):
    window = (datetime.combine(monday - timedelta(days=1), datetime.min.time()),
              datetime.combine(monday + timedelta(days=7), datetime.min.time()))
    return [a for c in ("lena", "sam", "tara")
            for a in world.store.appointment_history(c, *window)
            if a.status.value == "booked"]


def overlaps(appointments):
    clashes = []
    for i, a in enumerate(appointments):
        for b in appointments[i + 1:]:
            if a.range.start < b.range.end and b.range.start < a.range.end:
                clashes.append((a.client_id, b.client_id, a.range.start))
    return clashes


ANSWERS = ["accept", "decline", "pending"]


@pytest.mark.parametrize("lena", ANSWERS)
@pytest.mark.parametrize("sam", ANSWERS)
@pytest.mark.parametrize("order", list(permutations(["lena", "sam", "tara"])))
def test_nobody_is_ever_double_booked(order, lena, sam):
    """Every order the answers can arrive in, every mix of answers, settling
    after each one. The diary must never hold two people in one hour."""
    world, plan_id, asks, _, monday = two_moves_for_one_booking()
    answers = {"lena": lena, "sam": sam, "tara": "accept"}

    for who in order:
        if answers[who] != "pending":
            world.respond_to_approval(asks[who].id, answers[who])
        if world.plans[plan_id].status in ("awaiting_clients", "answered"):
            world.settle_plan(plan_id, "agreed")
        assert not overlaps(live(world, monday)), (order, answers, who)


def test_the_booking_waits_for_the_second_move():
    world, plan_id, asks, _, monday = two_moves_for_one_booking()
    world.respond_to_approval(asks["tara"].id, "accept")
    world.respond_to_approval(asks["sam"].id, "accept")

    outcome = world.settle_plan(plan_id, "agreed")

    assert not asks["tara"].applied, "Lena has not moved; her hour is still hers"
    assert asks["sam"].applied
    assert outcome["applied"] == 1

    world.respond_to_approval(asks["lena"].id, "accept")
    outcome = world.settle_plan(plan_id, "agreed")

    assert asks["tara"].applied and asks["lena"].applied
    assert outcome["applied"] == 2, "Lena's move and the booking it unblocked both count"
    assert not overlaps(live(world, monday))


def test_a_booking_that_falls_through_is_not_revived_by_a_later_settlement():
    """Its "yes" is final once what it needed is refused, or the next settle
    would pick it back up."""
    world, plan_id, asks, request, monday = two_moves_for_one_booking()
    world.respond_to_approval(asks["tara"].id, "accept")
    world.respond_to_approval(asks["lena"].id, "refuse")

    assert asks["tara"].status == "withdrawn"
    assert request.status == "on_hold", "back in the queue, not lost"

    world.respond_to_approval(asks["sam"].id, "accept")
    world.settle_plan(plan_id, "agreed")

    assert not asks["tara"].applied
    assert [a.client_id for a in live(world, monday) if a.client_id == "tara"] == []


def test_nobody_is_moved_for_a_booking_that_is_not_coming():
    """Lena will not move, so Tara is not coming, so Sam's move is pointless.
    He is no longer asked."""
    world, plan_id, asks, _, _ = two_moves_for_one_booking()
    world.respond_to_approval(asks["lena"].id, "refuse")

    assert asks["sam"].status == "withdrawn"
    world.respond_to_approval(asks["sam"].id, "accept")
    assert not asks["sam"].applied


# -- withdrawing a request while its offer is out ---------------------------


def test_withdrawing_takes_the_offer_back():
    world, plan_id, asks, request, monday = two_moves_for_one_booking()

    world.withdraw_request(request.id)

    assert asks["tara"].status == "withdrawn"
    assert not world._outstanding(asks["tara"]), "and the slot is no longer held"


def test_an_offer_accepted_after_withdrawing_books_nothing():
    world, plan_id, asks, request, monday = two_moves_for_one_booking()
    world.withdraw_request(request.id)

    world.respond_to_approval(asks["tara"].id, "accept")
    world.respond_to_approval(asks["lena"].id, "accept")
    world.respond_to_approval(asks["sam"].id, "accept")
    if world.plans[plan_id].status in ("awaiting_clients", "answered"):
        world.settle_plan(plan_id, "agreed")

    assert request.status == "withdrawn", "a withdrawal does not undo itself"
    assert [a for a in live(world, monday) if a.client_id == "tara"] == []


def test_withdrawing_stops_asking_others_to_make_room():
    """Lena and Sam were only being moved for Tara."""
    world, plan_id, asks, request, monday = two_moves_for_one_booking()

    world.withdraw_request(request.id)

    assert asks["lena"].status == "withdrawn"
    assert asks["sam"].status == "withdrawn"
    assert world.plans[plan_id].status == "answered", "nothing left to wait for"
    starts = {a.client_id: a.range.start for a in live(world, monday)}
    assert starts == {"lena": sc._at(monday, 0, 9), "sam": sc._at(monday, 0, 10)}


def test_withdrawing_after_the_moves_are_agreed_still_drops_them():
    """Agreed is not written: until settled, the move can still be called off."""
    world, plan_id, asks, request, _ = two_moves_for_one_booking()
    world.respond_to_approval(asks["lena"].id, "accept")

    world.withdraw_request(request.id)

    assert asks["lena"].status == "withdrawn" and not asks["lena"].applied
