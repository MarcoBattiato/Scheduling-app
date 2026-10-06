"""Starting calendars to demonstrate against.

A demo is only as good as the calendar it runs on. The first seed this project
had gave the provider forty hours a week with three bookings in it, which meant
the solver never had to move anybody: ten waiting requests produced ten
bookings and zero displacements. Everything hard about this system — the
negotiation, the three-way answer, the arrows, the settlement choice — is
invisible on a calendar with that much slack.

Scarcity is what makes it visible, and the cheap way to scarcity is a **short
working day** rather than a long list of clients. A provider seeing four hours
a day is busy at eight clients; one seeing eight hours needs sixteen before
anything interesting happens, and sixteen names is a demo nobody can follow.

Both scenarios here are built so that the very first run has something to
negotiate about. `tests/test_scenarios.py` pins that: a seed that quietly stops
forcing a displacement has stopped being a demo.
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta
from typing import Iterable, List, Sequence, Tuple

from calendar_store import Origin, Party

from .state import PROVIDER, World


def next_monday(today: date | None = None) -> date:
    """The Monday of the coming week — never today, so a scenario reads the
    same whichever day it is set up on."""
    today = today or date.today()
    return today + timedelta(days=(7 - today.weekday()) % 7 or 7)


def _at(monday: date, day: int, hour: int, minute: int = 0) -> datetime:
    return datetime.combine(monday + timedelta(days=day), time(hour, minute))


def _horizon_reaching(monday: date) -> int:
    """Days of planning window needed to cover the scenario's whole week.

    Without this a scenario behaves differently depending on the day it is
    loaded: next Monday can be seven days away, so with the default seven-day
    horizon a demo set up on a Thursday finds half its own week out of scope
    and quietly does nothing about it. The window is a provider setting, and a
    scenario about a particular week should have one that reaches it.
    """
    return max(7, (monday + timedelta(days=5) - date.today()).days)


def _close_the_run_up(world: World, monday: date) -> None:
    """Mark the days between today and the scenario's week as already done.

    A scenario describes one week, but the planning window starts *today*. Set
    up on a Monday, the empty days before the scenario's own week are fair game
    and the solver quietly uses them — so the request that was supposed to
    force a move gets an easy slot instead and the demo shows nothing. Closing
    the run-up says what is already true of the calendar being demonstrated:
    this week is spoken for, the interesting one is next.
    """
    day = date.today()
    while day < monday:
        world.set_exception(PROVIDER, day, time.min, time.max, available=False)
        day += timedelta(days=1)


def _hours(days: Iterable[int], start: str, end: str) -> List[dict]:
    return [{"weekday": d, "from": start, "to": end} for d in days]


def _book(world: World, monday: date, entries: Sequence[Tuple[str, int, int]],
          service: str = "session-60") -> List[int]:
    booked = []
    for client_id, day, hour in entries:
        start = _at(monday, day, hour)
        minutes = world.catalogue.get_service(service).duration_minutes
        booked.append(world.store.book_appointment(
            client_id, service, start, start + timedelta(minutes=minutes),
            preferred_start=start,
        ).id)
    return booked


# -- the small one ----------------------------------------------------


def simple(world: World) -> None:
    """Four clients, mornings only, and one request that cannot be met without
    moving somebody.

    Small enough to hold in your head while watching what the solver does. The
    provider works 09:00–13:00, so four appointments fill a day and the whole
    week is twenty slots — few enough that a person can see why a move is
    needed rather than taking the solver's word for it.

    The forcing structure is deliberately the kind that occurs by itself:
    Carol works, and can only ever manage Tuesday mornings. Both Tuesday
    morning slots are taken, and by people with the rest of the week open to
    them. So somebody has to move, and there is an obvious candidate.
    """
    world.catalogue.add_service("session-60", "Standard session", 60, 8000)
    world.catalogue.add_service("session-90", "Extended session", 90, 11000)
    world.catalogue.add_service("admin", "Notes and admin", 60, 0,
                                client_bookable=False)

    world.set_weekly_availability(PROVIDER, _hours(range(5), "09:00", "13:00"))

    for client_id, name in (("alice", "Alice"), ("bob", "Bob"),
                            ("carol", "Carol"), ("dan", "Dan")):
        world.add_client(client_id, name)

    # Alice and Bob are flexible; Dan has two days; Carol has one morning.
    world.set_weekly_availability("alice", _hours((0, 2, 4), "09:00", "13:00"))
    world.set_weekly_availability("bob", _hours((0, 2, 4), "09:00", "13:00"))
    world.set_weekly_availability("dan", _hours((1, 3), "09:00", "13:00"))
    world.set_weekly_availability("carol", _hours((1,), "09:00", "11:00"))

    monday = next_monday()
    world.policy.horizon_days = _horizon_reaching(monday)
    world.policy.auto_run = False        # the demonstrator decides when it runs
    _close_the_run_up(world, monday)
    _book(world, monday, [
        ("alice", 0, 9), ("bob", 0, 10), ("alice", 0, 11),
        ("dan", 1, 9), ("alice", 1, 10),        # both of Carol's slots, taken
        ("dan", 3, 10),
        ("bob", 4, 9), ("alice", 4, 10),
    ])
    # Wednesday is the day with the choice in it, and it is built to the scale
    # the cost model actually works at. A ninety-minute session to 10:30 and an
    # admin hour from 12:00 leave a ninety-minute stretch: any one-hour booking
    # in it strands half an hour that can never be sold. Bob's alternatives
    # without that waste are all on other days.
    #
    # So the choice is thirty minutes of dead time against a couple of days of
    # delay — which is where alpha bites, because a default hour of waste is
    # priced against a whole day of delay (CostConfig scales, 60 vs 1440). A
    # trade-off built at any smaller scale is decided by the normalisers before
    # the slider gets a say, and the slider looks broken.
    _book(world, monday, [("alice", 2, 9)], service="session-90")
    _book(world, monday, [(PROVIDER, 2, 12)], service="admin")

    # Carol can only be seen Tuesday morning, and both hours are occupied — by
    # Dan, who is also free Thursday, and Alice, who has the rest of the week.
    world.submit_request("carol", "session-60", _at(monday, 1, 9).isoformat())
    # Bob wants half past eleven, which splits Wednesday's remaining stretch
    # into an hour and a useless half hour. Giving him what he asked for costs
    # the provider thirty minutes they can never sell; moving him an hour costs
    # him. Neither is wrong, and the slider is where that gets decided.
    world.submit_request("bob", "session-60", _at(monday, 2, 11).isoformat())

    world._note("loaded the small example: four clients, mornings only")


# -- the plausible one ------------------------------------------------


def busy(world: World) -> None:
    """A practice that looks like one: nine clients, a full-ish book, and a
    history behind it.

    Everything the small scenario has, plus the things that only show up once
    a calendar has been lived in — attendance, a no-show, a cancellation, a
    client the clinic has moved before. Those are what the hover cards and the
    client summaries are for, and on a blank calendar they all read as zero.

    Still 09:00–13:00 and 14:00–17:00 rather than a full day: seven hours is
    enough to look like real work, and short enough that nine clients make it
    genuinely tight.
    """
    world.catalogue.add_service("session-60", "Standard session", 60, 8000)
    world.catalogue.add_service("session-90", "Extended session", 90, 11000)
    world.catalogue.add_service("intake", "First appointment", 90, 12000)
    world.catalogue.add_service("break", "Break", 60, 0, client_bookable=False)

    world.set_weekly_availability(PROVIDER, [
        *_hours(range(5), "09:00", "13:00"),
        *_hours(range(5), "14:00", "17:00"),
    ])

    # Constraints of the kind people actually have.
    people = [
        ("alice", "Alice", (0, 2, 4), "09:00", "13:00"),    # mornings, flexible
        ("bob", "Bob", (0, 1, 2, 3, 4), "16:00", "17:00"),  # works; end of day only
        ("carol", "Carol", (1,), "09:00", "11:00"),         # one morning a week
        ("dan", "Dan", (1, 3), "09:00", "13:00"),
        ("erin", "Erin", (0, 1, 2, 3, 4), "09:00", "17:00"),  # retired, wide open
        ("frank", "Frank", (2, 4), "14:00", "17:00"),
        ("gina", "Gina", (0, 3), "09:00", "12:00"),         # school hours
        ("hugo", "Hugo", (1, 3), "15:00", "17:00"),
        ("iris", "Iris", (4,), "09:00", "13:00"),           # Fridays only
    ]
    for client_id, name, days, start, end in people:
        world.add_client(client_id, name)
        world.set_weekly_availability(client_id, _hours(days, start, end))

    monday = next_monday()
    world.policy.horizon_days = _horizon_reaching(monday)
    world.policy.auto_run = False        # the demonstrator decides when it runs
    _close_the_run_up(world, monday)

    # -- what already happened, so the summaries are not all zeroes -----
    # Two weeks back, not one. Next Monday can be as little as a day away, so
    # "the week before" can be this week — history dated today or later, lying
    # in the run-up just closed, and a "completed" session nobody has had yet.
    # A fortnight back puts the whole history week at least three days behind
    # whichever day the scenario is set up on.
    last = monday - timedelta(days=14)
    attended = _book(world, last, [
        ("alice", 0, 9), ("erin", 0, 14), ("dan", 1, 10),
        ("carol", 1, 9), ("frank", 2, 15), ("gina", 3, 10),
    ])
    for appointment_id in attended[:-2]:
        world.store.mark_attendance(appointment_id, attended=True)
    world.store.mark_attendance(attended[-2], attended=False)          # a no-show
    world.cancel_appointment(attended[-1], by=Party.CLIENT)            # dropped it

    # Hugo was moved by the clinic once. Recorded as DISPLACED, so nothing
    # downstream mistakes a slot he was pushed into for one he likes.
    moved = world.store.book_appointment(
        "hugo", "session-60", _at(last, 1, 15), _at(last, 1, 16),
        preferred_start=_at(last, 3, 16), origin=Origin.DISPLACED,
    )
    world.store.mark_attendance(moved.id, attended=True)

    # -- the week in front of them --------------------------------------
    _book(world, monday, [
        ("alice", 0, 9), ("gina", 0, 10), ("erin", 0, 11), ("bob", 0, 16),
        ("dan", 1, 9), ("erin", 1, 10), ("hugo", 1, 15), ("bob", 1, 16),
        ("alice", 2, 9), ("erin", 2, 11), ("frank", 2, 15), ("bob", 2, 16),
        ("gina", 3, 9), ("dan", 3, 11), ("hugo", 3, 16),
        ("alice", 4, 9), ("iris", 4, 10), ("frank", 4, 14), ("bob", 4, 16),
    ])
    _book(world, monday, [("erin", 3, 14)], service="session-90")

    # A day with a hole in it, so the exception rendering has something to draw
    # and the calendar is not a perfect grid. Over free time on purpose: an
    # opening state that already shows a stranded-booking warning muddles the
    # "I am ill on Wednesday" demonstration, which is where that belongs.
    world.set_exception(PROVIDER, monday + timedelta(days=2),
                        time(12), time(13), available=False)

    # -- and what is waiting --------------------------------------------
    # Carol again: one morning a week, and it is taken.
    world.submit_request("carol", "session-60", _at(monday, 1, 9).isoformat())
    # Iris only does Fridays, and the morning is filling up.
    world.submit_request("iris", "session-60", _at(monday, 4, 11).isoformat())
    # Straightforward, for contrast.
    world.submit_request("erin", "session-60", _at(monday, 3, 10).isoformat())
    # A new client, wanting a longer first appointment.
    world.add_client("jo", "Jo")
    world.set_weekly_availability("jo", _hours((0, 2), "09:00", "13:00"))
    world.submit_request("jo", "intake", _at(monday, 2, 9).isoformat())

    world._note("loaded the realistic example: ten clients, a full-ish week")


SCENARIOS = {"simple": simple, "busy": busy}


def load(world: World, name: str) -> None:
    if name not in SCENARIOS:
        raise ValueError(f"no scenario called {name!r}; try {sorted(SCENARIOS)}")
    SCENARIOS[name](world)
