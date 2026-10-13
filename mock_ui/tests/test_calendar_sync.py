"""Appointments in people's own calendar apps.

What is checked is what a calendar would get wrong in front of a client: an
offer showing up before anyone agreed, a move arriving as a cancellation plus
a stranger, another client's name, the reason for the appointment, or a link
that changes on restart and silently stops every subscribed calendar.
"""
from datetime import datetime, timedelta

import pytest

from mock_ui import calendar_sync, persistence, scenarios
from mock_ui.calendar_sync import changes, events_for, to_ics
from mock_ui.state import World


@pytest.fixture
def world() -> World:
    w = World()
    scenarios.load(w, "simple")
    w.practice_name = "Harbour Psychology"
    return w


def uids(events):
    return {e.uid for e in events}


# -- what goes in whose calendar -------------------------------------------


def test_a_client_sees_only_their_own_appointments(world):
    dan = events_for(world, "dan")
    booked = [a for a in world.store.appointment_history(
                  "dan", datetime(2000, 1, 1), datetime(2100, 1, 1))
              if a.status.value == "booked"]

    assert len(dan) == len(booked) > 0


def test_a_clients_event_says_nothing_about_what_it_is_for(world):
    """Client calendars are shared with partners, families and employers."""
    for event in events_for(world, "dan"):
        assert event.summary == "Appointment — Harbour Psychology"
        assert "session" not in event.summary.lower()
        assert event.description == ""


def test_the_provider_sees_everyone_by_name_and_their_own_blocks(world):
    diary = events_for(world, "provider")
    summaries = {e.summary for e in diary}

    assert "Dan — Standard session" in summaries
    assert "Notes and admin" in summaries, "the provider's own time too"
    assert len(diary) > len(events_for(world, "dan"))


def test_an_offer_is_not_an_event_until_it_is_confirmed(world):
    before = uids(events_for(world, "carol"))
    plan_id = world.propose()["plan_id"]
    world.provider_approve(plan_id)
    carol = next(a for a in world.approvals.values() if a.client_id == "carol")
    world.respond_to_approval(carol.id, "accept")

    assert uids(events_for(world, "carol")) == before, "agreed is not confirmed"

    dan = next(a for a in world.approvals.values() if a.client_id == "dan")
    world.respond_to_approval(dan.id, "accept")
    world.settle_plan(plan_id, "agreed")

    assert len(events_for(world, "carol")) == len(before) + 1


# -- moves and cancellations -----------------------------------------------


def _first_booking(world, client):
    return min((a for a in world.store.appointment_history(
                    client, datetime(2000, 1, 1), datetime(2100, 1, 1))
                if a.status.value == "booked"), key=lambda a: a.range.start)


def test_a_move_is_the_same_event_at_a_new_time(world):
    booking = _first_booking(world, "dan")
    before = events_for(world, "dan")
    old = next(e for e in before if e.start == booking.range.start)

    new_start = booking.range.start + timedelta(hours=1)
    world.move_appointment(booking.id, new_start, new_start + timedelta(hours=1))
    after = events_for(world, "dan")
    moved = next(e for e in after if e.uid == old.uid)

    assert moved.start == new_start
    assert moved.sequence == old.sequence + 1, "calendars use this to take the newer version"
    assert changes(before, after) == [("updated", moved)]


def test_moving_twice_is_still_one_event(world):
    booking = _first_booking(world, "dan")
    uid = next(e.uid for e in events_for(world, "dan") if e.start == booking.range.start)
    first = world.move_appointment(booking.id, booking.range.start + timedelta(hours=1),
                                   booking.range.end + timedelta(hours=1))
    world.move_appointment(first.id, first.range.start + timedelta(hours=1),
                           first.range.end + timedelta(hours=1))

    same = [e for e in events_for(world, "dan") if e.uid == uid]
    assert len(same) == 1 and same[0].sequence == 2


def test_a_cancellation_removes_the_event(world):
    booking = _first_booking(world, "dan")
    before = events_for(world, "dan")
    gone = next(e for e in before if e.start == booking.range.start)

    world.cancel_appointment(booking.id)

    assert gone.uid not in uids(events_for(world, "dan"))
    assert changes(before, events_for(world, "dan")) == [("cancelled", gone)]


def test_nothing_changed_means_nothing_to_send(world):
    assert changes(events_for(world, "provider"), events_for(world, "provider")) == []


# -- the feed itself -------------------------------------------------------


def test_the_feed_is_valid_icalendar(world):
    text = to_ics(events_for(world, "dan"), "Appointments")

    assert text.startswith("BEGIN:VCALENDAR\r\n") and text.endswith("END:VCALENDAR\r\n")
    assert text.count("BEGIN:VEVENT") == text.count("END:VEVENT") == len(events_for(world, "dan"))
    assert "TZID:Asia/Singapore" in text
    assert "\n" not in text.replace("\r\n", ""), "lines end in CRLF and nothing else"


def test_long_lines_are_folded_and_special_characters_escaped(world):
    world.practice_name = "Harbour Psychology, Counselling; & Wellbeing — " + "é" * 40
    text = to_ics(events_for(world, "dan"), "x")

    for line in text.split("\r\n"):
        assert len(line.encode("utf-8")) <= 75, line
    unfolded = text.replace("\r\n ", "")
    assert "Harbour Psychology\\, Counselling\\; & Wellbeing" in unfolded


def test_the_feed_reaches_back_and_forward_but_not_forever(world):
    far = datetime.now() + calendar_sync.LOOK_AHEAD + timedelta(days=30)
    world.store.book_appointment("dan", "session-60", far, far + timedelta(hours=1))

    assert all(e.start < far for e in events_for(world, "dan"))


# -- links ----------------------------------------------------------------


def test_a_link_survives_a_restart(world, tmp_path):
    token = world.feed_token("dan")
    path = tmp_path / "s.json"
    persistence.save(world, path)

    restored = persistence.load(path)

    assert restored.person_for_feed(token) == "dan"


def test_replacing_a_link_stops_the_old_one(world):
    old = world.feed_token("dan")
    new = world.reset_feed_token("dan")

    assert old != new
    assert world.person_for_feed(old) is None and world.person_for_feed(new) == "dan"


def test_no_link_for_somebody_who_does_not_exist(world):
    with pytest.raises(KeyError):
        world.feed_token("nobody")


@pytest.fixture
def client():
    from fastapi.testclient import TestClient
    from mock_ui import app as app_module
    c = TestClient(app_module.app)
    c.post("/api/reset?scenario=simple")
    return c


def test_reading_the_state_never_makes_a_link(client):
    """A link made during a read would not be saved, and a restart would hand
    out a different one — breaking every calendar already subscribed."""
    client.get("/api/state")
    client.get("/api/state")

    assert client.get("/api/state").json()["feeds"] == {}


def test_the_feed_is_served_by_its_secret_and_nothing_else(client):
    token = client.post("/api/calendar/dan").json()["token"]

    feed = client.get(f"/calendar/{token}.ics")
    assert feed.status_code == 200
    assert feed.headers["content-type"].startswith("text/calendar")
    assert "BEGIN:VEVENT" in feed.text
    assert client.get("/calendar/guess.ics").status_code == 404
    assert client.post("/api/calendar/nobody").status_code == 404


def test_the_practice_name_is_a_setting(client):
    client.post("/api/settings", json={"practice_name": "  Harbour Psychology  "})

    assert client.get("/api/state").json()["practice_name"] == "Harbour Psychology"
    assert client.post("/api/settings", json={"practice_name": "  "}).status_code == 400
