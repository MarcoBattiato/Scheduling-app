"""Repeating bookings: weekly therapy without asking every week.

A series is a standing *request*, not a block of appointments: each session is
raised as an ordinary request when it comes inside the planning window, and
goes through the scheduler and the client's agreement like any other. These
tests pin that, plus the things that make it safe to leave running — each
session raised exactly once, a skipped week staying skipped, and stopping
leaving what is already booked alone.
"""
from datetime import date, datetime, time, timedelta

import pytest
from calendar_store import Origin, Party

from mock_ui import persistence
from mock_ui.state import PROVIDER, World

HORIZON = 7


def at(days: int, hour: int = 10) -> datetime:
    return datetime.combine(date.today() + timedelta(days=days), time(hour))


def later(days: int) -> datetime:
    """The clock, moved forward — what the tick sees a week on."""
    return datetime.now() + timedelta(days=days)


@pytest.fixture
def world() -> World:
    w = World()
    w.catalogue.add_service("s60", "Session", 60, 8000)
    every_day = [{"weekday": d, "from": "09:00", "to": "13:00"} for d in range(7)]
    w.set_weekly_availability(PROVIDER, every_day)
    w.add_client("carol", "Carol")
    w.set_weekly_availability("carol", every_day)
    w.policy.horizon_days = HORIZON
    w.policy.auto_run = False
    return w


def sessions(world, series):
    return sorted((r for r in world.requests.values() if r.series_id == series.id),
                  key=lambda r: r.preferred_start)


# -- raising sessions ---------------------------------------------------


def test_setting_one_up_raises_only_what_the_window_reaches(world):
    series = world.start_series("carol", "s60", at(3).isoformat(), every_weeks=1)

    raised = sessions(world, series)
    assert [r.preferred_start for r in raised] == [at(3)], (
        "next week's session is a request; the one after is not yet"
    )
    assert raised[0].status == "pending"
    assert raised[0].note == "every week"


def test_each_session_is_raised_as_the_window_reaches_it(world):
    series = world.start_series("carol", "s60", at(3).isoformat(), every_weeks=1)

    world.tick(now=later(7))
    world.tick(now=later(14))

    assert [r.preferred_start for r in sessions(world, series)] == [at(3), at(10), at(17)]


def test_ticking_again_raises_nothing_twice(world):
    """Tick runs on every poll — every 2.5 seconds per open tab."""
    series = world.start_series("carol", "s60", at(3).isoformat(), every_weeks=1)

    for _ in range(5):
        world.tick(now=later(7))

    assert len(sessions(world, series)) == 2


def test_every_two_weeks_means_every_two_weeks(world):
    series = world.start_series("carol", "s60", at(3).isoformat(), every_weeks=2)

    world.tick(now=later(7))
    assert len(sessions(world, series)) == 1, "the fortnight is not up yet"
    world.tick(now=later(14))

    assert [r.preferred_start for r in sessions(world, series)] == [at(3), at(17)]


def test_a_skipped_week_stays_skipped(world):
    """Withdrawing one session is "not that week", not "ask me again"."""
    series = world.start_series("carol", "s60", at(3).isoformat(), every_weeks=1)
    world.withdraw_request(sessions(world, series)[0].id)

    world.tick(now=later(1))

    assert [r.status for r in sessions(world, series)] == ["withdrawn"]
    assert series.status == "active", "one week off does not end the series"


def test_it_ends_on_the_last_date(world):
    last = at(10).date()
    series = world.start_series("carol", "s60", at(3).isoformat(), every_weeks=1,
                                until=last.isoformat())

    for week in range(1, 5):
        world.tick(now=later(7 * week))

    assert [r.preferred_start for r in sessions(world, series)] == [at(3), at(10)]
    assert series.status == "finished"


def test_a_series_started_in_the_past_raises_nothing_already_gone(world):
    series = world.start_series("carol", "s60", at(-7).isoformat(), every_weeks=1)

    # Sessions fall at -7, 0 and +7 days. The one a week ago is gone; today's
    # counts only if 10:00 has not passed; next week's is beyond the window.
    expected = [at(0)] if at(0) > datetime.now() else []
    assert [r.preferred_start for r in sessions(world, series)] == expected


def test_setting_one_up_does_not_run_the_scheduler(world):
    """Like any request: arriving is not a reason to re-plan the week."""
    world.start_series("carol", "s60", at(3).isoformat(), every_weeks=1)

    assert not world.plans


# -- each session is an ordinary request -----------------------------------


def test_a_session_is_placed_by_the_scheduler_like_any_request(world):
    series = world.start_series("carol", "s60", at(3).isoformat(), every_weeks=1)

    outcome = world.propose()
    plan = world.plans[outcome["plan_id"]]

    assert [x["start"] for x in plan.placements] == [at(3)], (
        "free at the series' own time, so that is where it goes"
    )


def test_a_session_counts_as_chosen_by_the_client(world):
    """The client set the series up; its slot is a real preference, and is
    exactly the habitual slot the anchoring work wants to learn."""
    series = world.start_series("carol", "s60", at(3).isoformat(), every_weeks=1)
    plan_id = world.propose()["plan_id"]
    world.provider_approve(plan_id)
    approval = next(iter(world.approvals.values()))
    world.respond_to_approval(approval.id, "accept")
    world.settle_plan(plan_id, "agreed")

    booked = world.store.appointment_history("carol", at(0), at(30))
    assert [(a.origin, a.preferred_start) for a in booked] == [(Origin.CLIENT, at(3))]


# -- stopping -----------------------------------------------------------


def test_stopping_withdraws_what_is_not_booked_and_keeps_what_is(world):
    series = world.start_series("carol", "s60", at(3).isoformat(), every_weeks=1)
    plan_id = world.propose()["plan_id"]
    world.provider_approve(plan_id)
    world.respond_to_approval(next(iter(world.approvals.values())).id, "accept")
    world.settle_plan(plan_id, "agreed")
    world.tick(now=later(7))                     # next week's session, not placed

    withdrawn = world.stop_series(series.id)

    assert withdrawn == 1
    assert [r.status for r in sessions(world, series)] == ["placed", "withdrawn"]
    assert len([a for a in world.store.appointment_history("carol", at(0), at(30))
                if a.status.value == "booked"]) == 1


def test_a_stopped_series_raises_nothing_more(world):
    series = world.start_series("carol", "s60", at(3).isoformat(), every_weeks=1)
    world.stop_series(series.id)

    world.tick(now=later(14))

    assert len(sessions(world, series)) == 1
    assert world.stop_series(series.id) == 0, "stopping twice is harmless"


def test_stopping_takes_back_an_offer_already_made(world):
    series = world.start_series("carol", "s60", at(3).isoformat(), every_weeks=1)
    world.provider_approve(world.propose()["plan_id"])
    offer = next(iter(world.approvals.values()))

    world.stop_series(series.id)

    assert offer.status == "withdrawn"


# -- refusals -----------------------------------------------------------


@pytest.mark.parametrize("kwargs, error", [
    ({"every_weeks": 0}, ValueError),
    ({"every_weeks": 13}, ValueError),
    ({"until": (date.today()).isoformat()}, ValueError),     # before the first
])
def test_nonsense_is_refused(world, kwargs, error):
    with pytest.raises(error):
        world.start_series("carol", "s60", at(3).isoformat(), **kwargs)
    assert not world.series


def test_an_unknown_client_or_service_is_refused(world):
    with pytest.raises(KeyError):
        world.start_series("nobody", "s60", at(3).isoformat())
    with pytest.raises(KeyError):
        world.start_series("carol", "nothing", at(3).isoformat())


# -- surviving a restart ---------------------------------------------------


def test_a_series_survives_a_restart_without_raising_twice(world, tmp_path):
    series = world.start_series("carol", "s60", at(3).isoformat(), every_weeks=1)
    path = tmp_path / "session.json"
    persistence.save(world, path)

    restored = persistence.load(path)
    restored.tick(now=later(1))

    again = restored.series[series.id]
    assert again.raised == series.raised
    assert len([r for r in restored.requests.values() if r.series_id == series.id]) == 1
    fresh = restored.start_series("carol", "s60", at(4).isoformat())
    assert fresh.id not in world.series, "ids restart above everything restored"


# -- over HTTP ------------------------------------------------------------


@pytest.fixture
def client():
    from fastapi.testclient import TestClient
    from mock_ui import app as app_module
    client = TestClient(app_module.app)
    client.post("/api/reset?scenario=simple")
    return client


def test_the_api_starts_and_stops_a_series(client):
    from mock_ui import scenarios
    # A day Carol has not already asked for — that request would cover it.
    tuesday = scenarios._at(scenarios.next_monday(), 1, 10).isoformat()
    for r in client.get("/api/state").json()["requests"]:
        if r["client_id"] == "carol":
            client.post(f"/api/requests/{r['id']}/withdraw")

    started = client.post("/api/series", json={
        "client_id": "carol", "service_id": "session-60",
        "first_start": tuesday, "every_weeks": 1}).json()
    state = client.get("/api/state").json()

    mine = [x for x in state["series"] if x["id"] == started["series_id"]]
    assert mine and mine[0]["every_weeks"] == 1 and mine[0]["status"] == "active"
    assert any(r["series_id"] == started["series_id"] for r in state["requests"])

    stopped = client.post(f"/api/series/{started['series_id']}/stop")
    assert stopped.status_code == 200
    assert client.post("/api/series/99999/stop").status_code == 404


def test_the_api_refuses_a_bad_series(client):
    response = client.post("/api/series", json={
        "client_id": "carol", "service_id": "session-60",
        "first_start": "2030-01-01T09:00", "every_weeks": 0})

    assert response.status_code == 400


# -- weeks the provider is away, and sessions that were never placed ---------


def test_a_session_in_time_the_provider_has_off_is_skipped_not_raised(world):
    world.provider_away(at(10, 0), at(11, 0))      # all of day 10, before it is raised
    series = world.start_series("carol", "s60", at(3).isoformat(), every_weeks=1)

    world.tick(now=later(7))
    world.tick(now=later(14))

    away = next(r for r in sessions(world, series) if r.preferred_start == at(10))
    assert [r.preferred_start for r in sessions(world, series)] == [at(3), at(10), at(17)]
    assert away.status == "lapsed" and away.lapsed_reason == World.SKIP_AWAY, \
        "kept on record with the reason, never just left out"
    assert at(10).date() in series.raised, "and it is not tried again"


def test_time_off_taken_after_a_session_was_raised_lapses_it(world):
    series = world.start_series("carol", "s60", at(3).isoformat(), every_weeks=1)
    one_off = world.submit_request("carol", "s60", at(3, 12).isoformat())

    world.provider_away(at(3, 0), at(4, 0))

    assert sessions(world, series)[0].status == "lapsed"
    assert one_off.status == "pending", "a one-off request is still wanted"


def test_a_session_not_placed_before_its_day_is_not_made_up_later(world):
    """Last Friday's session placed next Tuesday is a second session that week."""
    series = world.start_series("carol", "s60", at(3).isoformat(), every_weeks=1)
    missed = sessions(world, series)[0]

    world.tick(now=later(5))
    world.propose(now=later(5), max_displacements=0)

    assert missed.status == "lapsed"
    assert not any(int(p["request_id"]) == missed.id
                   for plan in world.plans.values() for p in plan.placements)
    assert [r.status for r in sessions(world, series)][1:] == ["pending"], \
        "the next session is raised as usual"


def test_a_preview_leaves_a_missed_session_alone(world):
    series = world.start_series("carol", "s60", at(3).isoformat(), every_weeks=1)
    missed = sessions(world, series)[0]

    world.propose(now=later(5), dry_run=True)

    assert missed.status == "pending", "a preview changes nothing"


# -- never silently ------------------------------------------------------------


def test_a_session_that_will_not_happen_is_shown_to_both_until_seen(world, tmp_path):
    series = world.start_series("carol", "s60", at(3).isoformat(), every_weeks=1)
    result = world.provider_away(at(3, 0), at(4, 0))
    skipped = sessions(world, series)[0]

    assert result["sessions_skipped"] == [skipped.id], "the provider is told at once"
    shown = next(r for r in world.snapshot()["requests"] if r["id"] == skipped.id)
    assert shown["status"] == "lapsed" and shown["lapsed_reason"] == "away"
    assert "provider" not in shown["seen_by"], "the provider has still to see it"
    told = world.notices_for("carol")
    assert [n["kind"] for n in told] == ["skipped"] and "closed that day" in told[0]["text"]

    world.acknowledge_notice(told[0]["id"], "carol")
    path = tmp_path / "s.json"
    persistence.save(world, path)
    again = persistence.load(path)
    assert again.notices_for("carol") == [], "seen stays seen across a restart"
    assert "provider" not in again.requests[skipped.id].seen_by


def test_a_booking_the_practice_cancels_is_shown_to_the_client_until_seen(world):
    appointment = world.store.book_appointment("carol", "s60", at(2), at(2, 11))

    world.cancel_appointment(appointment.id, by=Party.PROVIDER)
    told = world.notices_for("carol")
    assert [n["kind"] for n in told] == ["cancelled"]

    world.acknowledge_notice(told[0]["id"], "carol")
    assert world.notices_for("carol") == []


def test_the_client_cancelling_or_a_move_needs_no_notice(world):
    mine = world.store.book_appointment("carol", "s60", at(2), at(2, 11))
    moving = world.store.book_appointment("carol", "s60", at(4), at(4, 11))

    world.cancel_appointment(mine.id, by=Party.CLIENT)
    world.cancel_appointment(moving.id, by=Party.PROVIDER, as_move=True)

    assert world.notices_for("carol") == []


def test_a_series_from_a_slot_already_booked_does_not_book_it_twice(world):
    """"Keep my Tuesday slot going": the session they have is the first one."""
    world.store.book_appointment("carol", "s60", at(3), at(3, 11))

    series = world.start_series("carol", "s60", at(3).isoformat(), every_weeks=1)
    world.tick(now=later(7))

    assert [r.preferred_start for r in sessions(world, series)] == [at(10)]
