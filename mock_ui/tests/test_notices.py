"""Nothing about a client's bookings changes behind their back.

Every way one of their bookings, offers or requests can change without them
doing it leaves a notice on their Home until they press OK — worded for them,
and never saying why in terms of anybody else. And a client's screens are sent
only their own things: what is hidden on screen is still readable in the
browser's developer tools.
"""
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from mock_ui import app as app_module
from mock_ui import scenarios
from mock_ui.state import World


@pytest.fixture
def world():
    """Carol's offer only works if Dan moves; Bob's stands alone."""
    w = World()
    scenarios.load(w, "simple")
    plan_id = w.propose()["plan_id"]
    w.provider_approve(plan_id)
    w.plan_id = plan_id
    return w


def ask(world, client_id):
    return next(a for a in world.approvals.values() if a.client_id == client_id)


def told(world, client_id):
    return world.notices_for(client_id)


def test_an_offer_agreed_to_and_then_dropped_is_not_silent(world):
    world.respond_to_approval(ask(world, "bob").id, "accept")

    world.settle_plan(world.plan_id, "reoptimise")

    notes = told(world, "bob")
    assert [n["kind"] for n in notes] == ["withdrawn"]
    assert "you agreed to" in notes[0]["text"] and "another time" in notes[0]["text"]


def test_an_offer_taken_back_never_says_who_was_in_the_way(world):
    world.respond_to_approval(ask(world, "dan").id, "refuse")

    notes = told(world, "carol")
    assert [n["kind"] for n in notes] == ["withdrawn"]
    assert not {"Dan", "dan", "refuse"} & set(notes[0]["text"].replace(",", " ").split())


def test_a_move_no_longer_needed_says_the_appointment_stays(world):
    world.withdraw_request(ask(world, "carol").request_id)    # the move was only for her

    notes = told(world, "dan")
    assert [n["kind"] for n in notes] == ["withdrawn"] and "stays on" in notes[0]["text"]


def test_a_booking_left_in_time_the_clinic_is_closed_is_told_once(world):
    dans = next(a for a in world.store.appointments_for(
        "dan", datetime.now(), datetime.now() + timedelta(days=28)))
    day = dans.range.start.replace(hour=0, minute=0)

    world.provider_away(day, day + timedelta(hours=23), action="flag")
    world.tick()
    world.tick()

    assert [n["kind"] for n in told(world, "dan")] == ["closed"], "told, and only once"


def test_a_booking_made_by_hand_is_told(world):
    world.settle_plan(world.plan_id, "reoptimise")
    request = next(r for r in world.requests.values()
                   if r.client_id == "bob" and r.status in ("pending", "on_hold"))
    world.place_manually(request.id, request.preferred_start)

    assert "booked" in [n["kind"] for n in told(world, "bob")]


def test_a_request_without_a_time_yet_is_told_once():
    w = World()
    scenarios.load(w, "simple")
    w.set_weekly_availability("bob", [])            # nowhere he can be placed

    w.propose()
    w.propose()

    assert [n["kind"] for n in w.notices_for("bob")] == ["unplaced"]


def test_only_the_client_can_clear_their_notice(world):
    world.respond_to_approval(ask(world, "dan").id, "refuse")
    notice = told(world, "carol")[0]

    with pytest.raises(KeyError):
        world.acknowledge_notice(notice["id"], "bob")
    world.acknowledge_notice(notice["id"], "carol")

    assert told(world, "carol") == []


# -- what a client's screens are sent ---------------------------------------


def test_a_client_is_sent_only_their_own_things(world):
    payload = world.snapshot_for("carol")
    text = repr(payload)

    leaked = [w for w in ("Dan", "Bob", "Alice", "'dan'", "'bob'", "'alice'") if w in text]
    assert not leaked, leaked
    assert set(payload["availability"]) == {"carol"}, "never the provider's hours"
    assert "plans" not in payload and "log" not in payload and "scheduler" not in payload


def test_nothing_anybody_else_does_changes_what_a_client_is_sent(world):
    """Not just no names: nothing a client receives may depend on other
    people's bookings, or the diary could be worked out from it."""
    before = world.snapshot_for("carol")

    bob = next(a for a in world.approvals.values() if a.client_id == "bob")
    world.respond_to_approval(bob.id, "accept")
    when = datetime.now() + timedelta(days=3)
    world.store.book_appointment("alice", "session-60", when.replace(hour=20, minute=0),
                                 when.replace(hour=21, minute=0))

    assert world.snapshot_for("carol") == before


def test_the_api_scopes_state_and_notices_to_the_person():
    client = TestClient(app_module.app)
    client.post("/api/reset")
    everyone = client.get("/api/state").json()
    carol = client.get("/api/state?as=carol").json()

    assert len(everyone["clients"]) > 1 and [c["id"] for c in carol["clients"]] == ["carol"]
    assert client.get("/api/state?as=nobody").status_code == 404
    assert client.post("/api/notices/987654/seen?as=carol").status_code == 404
    names = [p["id"] for p in client.get("/api/people").json()]
    assert names[0] == "provider" and "carol" in names


def test_declining_an_offer_stops_asking_whoever_was_moving_for_it(world):
    world.respond_to_approval(ask(world, "carol").id, "decline")

    assert ask(world, "dan").status == "withdrawn", "Dan was only moving to make room for Carol"
    assert [n["kind"] for n in told(world, "dan")] == ["withdrawn"]
    assert ask(world, "bob").status == "pending", "Bob's offer did not depend on it"
