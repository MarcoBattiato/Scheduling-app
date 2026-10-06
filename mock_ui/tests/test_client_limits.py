"""How often one client may be booked, end to end.

The engine enforces the cap; this checks the mock tells it everything it has
to count. The solver only ever sees fixed bookings as time subtracted from the
provider's day, with no name on them, so a limit is only as good as the list
of sessions it is handed: bookings it cannot move, offers still out, and the
earlier part of the week the planning window does not reach.

The regression it exists for: Erin, free all week and already booked four
days, was offered two more sessions and asked to move two — six in five days,
two on one Thursday.
"""
from collections import Counter
from datetime import date, datetime, time, timedelta

import pytest

from mock_ui import persistence, scenarios
from mock_ui.state import PROVIDER, World


def day(n: int) -> date:
    """Days from the coming Monday, so the tests never straddle a weekend."""
    return scenarios.next_monday() + timedelta(days=n)


def at(n: int, hour: int) -> datetime:
    return datetime.combine(day(n), time(hour))


@pytest.fixture
def world() -> World:
    """Mornings, Monday to Friday, for the provider and for Erin."""
    w = World()
    w.catalogue.add_service("s60", "Session", 60, 8000)
    mornings = scenarios._hours(range(5), "09:00", "13:00")
    w.set_weekly_availability(PROVIDER, mornings)
    w.add_client("erin", "Erin")
    w.set_weekly_availability("erin", mornings)
    w.policy.horizon_days = scenarios._horizon_reaching(scenarios.next_monday())
    w.policy.auto_run = False
    scenarios._close_the_run_up(w, scenarios.next_monday())
    return w


def placed_days(world):
    plan = next(p for p in world.plans.values() if p.status == "draft")
    return sorted(x["start"].date() for x in plan.placements)


def test_by_default_a_client_is_booked_once_a_day(world):
    world.submit_request("erin", "s60", at(1, 9).isoformat())
    world.submit_request("erin", "s60", at(1, 11).isoformat())

    world.propose(alpha=0.0)

    assert len(set(placed_days(world))) == 2, "two requests, two different days"


def test_without_a_limit_the_same_day_is_allowed(world):
    world.set_client_limits("erin", per_day=None, per_week=None)
    world.submit_request("erin", "s60", at(1, 9).isoformat())
    world.submit_request("erin", "s60", at(1, 11).isoformat())

    world.propose(alpha=0.0)

    assert placed_days(world) == [day(1), day(1)]


def test_an_existing_booking_counts(world):
    world.store.book_appointment("erin", "s60", at(1, 9), at(1, 10))
    world.submit_request("erin", "s60", at(1, 11).isoformat())

    world.propose(alpha=0.0)

    assert day(1) not in placed_days(world)


def test_an_offer_still_out_counts(world):
    """Not booked yet, but if she says yes it will be — so the slot counts."""
    first = world.submit_request("erin", "s60", at(1, 9).isoformat())
    world.provider_approve(world.propose(request_ids=[first.id])["plan_id"])

    world.submit_request("erin", "s60", at(1, 11).isoformat())
    world.propose(alpha=0.0)

    assert day(1) not in placed_days(world)


def test_a_weekly_limit_counts_the_start_of_the_week(world):
    """Run on Wednesday: Monday's session is before the planning window, but
    it is in the same week."""
    world.set_client_limits("erin", per_day=1, per_week=1)
    world.store.book_appointment("erin", "s60", at(0, 9), at(0, 10))
    world.submit_request("erin", "s60", at(3, 9).isoformat())

    world.propose(now=at(2, 8), alpha=0.0)

    plan = next(p for p in world.plans.values() if p.status == "draft") \
        if world.plans else None
    landed = [x["start"].date() for x in plan.placements] if plan else []
    assert all(d >= day(7) for d in landed), landed


def test_a_repeating_session_is_skipped_in_a_full_week(world):
    world.set_client_limits("erin", per_day=1, per_week=1)
    world.store.book_appointment("erin", "s60", at(0, 9), at(0, 10))

    series = world.start_series("erin", "s60", at(2, 10).isoformat(), every_weeks=1)

    assert not [r for r in world.requests.values() if r.series_id == series.id]
    assert day(2) in series.raised, "recorded, so it is not tried again"
    assert any("week is already full" in line for line in world.log)


def test_a_limit_of_zero_is_refused(world):
    with pytest.raises(ValueError):
        world.set_client_limits("erin", per_day=0, per_week=None)


def test_limits_survive_a_restart(world, tmp_path):
    world.set_client_limits("erin", per_day=2, per_week=3)
    path = tmp_path / "session.json"
    persistence.save(world, path)

    erin = persistence.load(path).clients["erin"]

    assert (erin.per_day, erin.per_week) == (2, 3)


def test_a_session_saved_before_limits_existed_gets_the_default(tmp_path):
    import json
    w = World()
    w.add_client("erin", "Erin")
    path = tmp_path / "session.json"
    persistence.save(w, path)
    payload = json.loads(path.read_text())
    for client in payload["clients"]:
        client.pop("per_day"), client.pop("per_week")
    path.write_text(json.dumps(payload))

    erin = persistence.load(path).clients["erin"]

    assert (erin.per_day, erin.per_week) == (1, None)


def test_erin_is_never_offered_a_second_session_on_a_day():
    """The case that found it: the realistic example, plus a weekly booking
    for the client who is free all week."""
    world = World()
    scenarios.load(world, "busy")
    friday = scenarios._at(scenarios.next_monday(), 4, 12)
    world.start_series("erin", "session-60", friday.isoformat(), every_weeks=1)

    world.propose(max_displacements=2)
    plan = next(p for p in world.plans.values() if p.status == "draft")

    moving = {d["appointment_id"]: d for d in plan.displacements}
    erin_days = Counter()
    for a in world.store.appointment_history("erin", at(0, 0), at(7, 0)):
        if a.status.value != "booked":
            continue
        where = moving[a.id]["now_start"] if a.id in moving else a.range.start
        erin_days[where.date()] += 1
    for x in plan.placements:
        if x["client_id"] == "erin":
            erin_days[x["start"].date()] += 1

    assert max(erin_days.values()) == 1, erin_days


# -- over HTTP ------------------------------------------------------------


@pytest.fixture
def client():
    from fastapi.testclient import TestClient
    from mock_ui import app as app_module
    c = TestClient(app_module.app)
    c.post("/api/reset?scenario=simple")
    return c


def test_the_api_sets_and_reports_limits(client):
    response = client.post("/api/clients/alice/limits", json={"per_day": 2, "per_week": None})
    alice = next(c for c in client.get("/api/state").json()["clients"] if c["id"] == "alice")

    assert response.status_code == 200
    assert (alice["per_day"], alice["per_week"]) == (2, None)
    assert client.post("/api/clients/nobody/limits", json={}).status_code == 404
    assert client.post("/api/clients/alice/limits", json={"per_day": 0}).status_code == 400
