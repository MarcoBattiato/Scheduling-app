"""The example calendars, checked for the one property that makes them worth
having.

A demo runs on whatever the seed left behind. The seed this project started
with gave the provider forty hours a week against three bookings, so ten
waiting requests produced ten bookings and *zero* displacements — the
negotiation, the three-way answer, the arrows and the settlement choice were
all unreachable without hand-building scarcity live in front of somebody.

So the property under test is not "the scenario loads". It is "the first run
has something to negotiate about", plus the fact that this holds whichever day
of the week it is set up on — a scenario that works on a Thursday and quietly
does nothing on a Monday is worse than one that never worked.
"""
from __future__ import annotations

import datetime as dt

import pytest
from calendar_store import AppointmentStatus, Origin

from mock_ui import scenarios
from mock_ui.state import PROVIDER, World


EVERY_WEEKDAY = [pytest.param(n, id=f"+{n}d") for n in range(7)]


def loaded(name: str, days_from_now: int = 0):
    """Load a scenario as though today were `days_from_now` away.

    Both `next_monday` and the horizon read `date.today()`, so faking one and
    not the other produces results that look fine and mean nothing — which is
    exactly the mistake this helper exists to stop anyone repeating.
    """
    pretend = dt.date.today() + dt.timedelta(days=days_from_now)
    real = scenarios.date

    class FakeDate(real):
        @classmethod
        def today(cls):
            return pretend

    scenarios.date = FakeDate
    try:
        world = World()
        scenarios.load(world, name)
    finally:
        scenarios.date = real
    return world, dt.datetime.combine(pretend, dt.time(8))


@pytest.mark.parametrize("name", sorted(scenarios.SCENARIOS))
@pytest.mark.parametrize("offset", EVERY_WEEKDAY)
def test_the_first_run_has_something_to_negotiate_about(name, offset):
    """The whole point. Without a displacement the demo shows requests going in
    and bookings coming out, which is the least interesting thing here."""
    world, now = loaded(name, offset)

    outcome = world.propose(now=now)

    assert outcome["ran"], outcome
    assert outcome["displacements"] >= 1, (
        f"{name} set up {offset} day(s) from now proposes no move at all"
    )
    assert outcome["placements"] >= 1, "and something ordinary alongside it"


@pytest.mark.parametrize("name", sorted(scenarios.SCENARIOS))
@pytest.mark.parametrize("offset", EVERY_WEEKDAY)
def test_nothing_falls_outside_the_window_it_set_itself(name, offset):
    """A scenario chooses its own horizon precisely so its whole week is in
    play. Anything out of scope means it is describing a week it cannot act
    on."""
    world, now = loaded(name, offset)

    assert world.propose(now=now)["out_of_scope"] == 0


@pytest.mark.parametrize("name", sorted(scenarios.SCENARIOS))
def test_a_scenario_leaves_no_booking_the_provider_cannot_keep(name):
    """Closing the run-up must not strand the very bookings it is setting up."""
    world, _ = loaded(name)

    assert world.orphaned_appointments() == []


@pytest.mark.parametrize("name", sorted(scenarios.SCENARIOS))
@pytest.mark.parametrize("offset", EVERY_WEEKDAY)
def test_history_is_in_the_past_whichever_day_it_is_set_up(name, offset):
    """An attended, missed or cancelled session has already happened. Placed
    relative to next Monday rather than to today, "last week" overlapped the
    current one when set up early in the week — and those sessions then sat in
    the closed run-up as bookings the provider could not keep."""
    world, now = loaded(name, offset)
    window = (now - dt.timedelta(days=60), now + dt.timedelta(days=60))
    settled = [a for c in world.clients
               for a in world.store.appointment_history(c, *window)
               if a.status is not AppointmentStatus.BOOKED]

    assert all(a.range.end <= now for a in settled), [
        (a.client_id, a.range.start, a.status.value)
        for a in settled if a.range.end > now
    ]


def test_the_small_one_stays_small_enough_to_follow(name="simple"):
    world, _ = loaded(name)

    assert len(world.clients) <= 5, "a demo nobody can follow is not a demo"
    hours = world.availability_segments(
        PROVIDER, dt.date.today(), dt.date.today() + dt.timedelta(days=14))
    longest = max((s.end - s.start for s in hours), default=dt.timedelta())
    assert longest <= dt.timedelta(hours=4), (
        "scarcity comes from a short day, not from a long list of clients"
    )


def test_the_realistic_one_has_a_history_behind_it():
    """Attendance, a no-show, a cancellation and a client moved before. On a
    blank calendar every client summary reads as zero, and the hover cards —
    which exist to show exactly this — say nothing at all."""
    world, _ = loaded("busy")
    everything = [a for c in world.clients
                  for a in world.store.appointment_history(
                      c, dt.datetime.now() - dt.timedelta(days=21),
                      dt.datetime.now() + dt.timedelta(days=21))]
    statuses = {a.status for a in everything}

    assert AppointmentStatus.COMPLETED in statuses
    assert AppointmentStatus.NO_SHOW in statuses
    assert AppointmentStatus.CANCELLED_BY_CLIENT in statuses
    assert any(a.origin is Origin.DISPLACED for a in everything), (
        "somebody the clinic has moved before, so origin is visible"
    )
    assert any(world.client_summary(c)["completed"] for c in world.clients)


def test_an_unknown_scenario_is_refused_rather_than_silently_empty():
    with pytest.raises(ValueError):
        scenarios.load(World(), "whatever")


def test_the_slider_visibly_changes_the_answer_in_the_small_one():
    """The demonstration turns on this, and it broke twice while being built.

    A trade-off has to be constructed at the scale the cost model works at.
    `CostConfig` prices an hour of waste against a *day* of delay, so an
    example weighing thirty minutes of dead time against thirty minutes of
    delay is settled by the normalisers before alpha gets a say — three
    identical proposals, and a slider that looks broken in front of a customer.

    Here the choice is half an hour the provider can never sell against a
    couple of days of Bob's delay, which turns over inside the usable range.
    """
    def bob_at(alpha):
        world, _ = loaded("simple")
        world.propose(alpha=alpha)
        plan = next(p for p in world.plans.values() if p.status == "draft")
        return (next(x for x in plan.placements if x["client_id"] == "bob")["start"],
                plan.metrics["fragmentation_minutes"])

    wished, wasteful = bob_at(0.5)
    packed, tidy = bob_at(0.9)

    assert wished != packed, "alpha changes nothing, so there is nothing to show"
    assert wasteful > tidy == 0, (
        "and the difference is the one the slider claims to be about: "
        f"{wasteful}m of waste to honour the wish, {tidy}m if packed"
    )


@pytest.mark.parametrize("name", sorted(scenarios.SCENARIOS))
def test_a_scenario_does_not_start_scheduling_on_its_own(name):
    """The demonstrator says when it runs. A proposal appearing unbidden
    between loading and the first click is confusing to explain."""
    world, _ = loaded(name)

    assert world.policy.auto_run is False
    assert not world.plans
