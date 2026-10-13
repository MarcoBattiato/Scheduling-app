"""Per-client limits: how many sessions one client may have in a day or week.

Found in use rather than in design. A client with wide availability, a
weekly repeating booking and a one-off request all landing in the same week
was offered two new sessions at once and asked to move two more — six
appointments in five days, two on one Thursday, and nothing in the solver
able to see that they were all the same person. Fixed bookings reach the
solver already subtracted from free time, anonymous, so it could not count
them even if it tried; `ClientLimit.fixed` is how it is told.
"""
from datetime import date, datetime, time

import pytest

from scheduling_engine import (
    BookingRequest,
    ClientLimit,
    CostConfig,
    MovableAppointment,
    TimeRange,
    TimeSegment,
    solve_placements,
)

MON, TUE, WED = date(2026, 5, 4), date(2026, 5, 5), date(2026, 5, 6)
NEXT_MON = date(2026, 5, 11)
CFG = CostConfig(alpha=0.0)          # earliest wins, so stacking is the default


def at(day, hour):
    return datetime.combine(day, time(hour))


def rng(day, a, b):
    return TimeRange(at(day, a), at(day, b))


def seg(day, a, b):
    return TimeSegment(at(day, a), at(day, b))


OPEN = [seg(MON, 9, 13), seg(TUE, 9, 13), seg(WED, 9, 13), seg(NEXT_MON, 9, 13)]
ANY_DAY = [rng(MON, 9, 13), rng(TUE, 9, 13), rng(WED, 9, 13), rng(NEXT_MON, 9, 13)]


def ask(id_, client="erin", windows=ANY_DAY, wish=None):
    return BookingRequest(id_, client, 60, list(windows), preferred_start=wish)


def days(result):
    return sorted(p.range.start.date() for p in result.placements)


def test_without_a_limit_one_client_can_be_stacked_on_one_day():
    """The behaviour being fixed — kept so the other tests mean something."""
    result = solve_placements([ask("a"), ask("b")], OPEN, CFG)

    assert days(result) == [MON, MON]


def test_one_a_day_spreads_two_requests_from_one_client():
    result = solve_placements([ask("a"), ask("b")], OPEN, CFG,
                              client_limits={"erin": ClientLimit(per_day=1)})

    assert days(result) == [MON, TUE]


def test_a_limit_is_per_client():
    """Somebody else's limit says nothing about Erin."""
    result = solve_placements([ask("a"), ask("b")], OPEN, CFG,
                              client_limits={"iris": ClientLimit(per_day=1)})

    assert days(result) == [MON, MON]


def test_a_fixed_booking_counts():
    """Erin already has Monday at nine. The solver never sees whose that is
    unless it is told."""
    result = solve_placements(
        [ask("a", wish=at(MON, 10))], OPEN, CFG,
        client_limits={"erin": ClientLimit(per_day=1, fixed=[at(MON, 9)])})

    assert days(result) == [TUE]


def test_a_request_with_nowhere_left_is_unplaced_rather_than_stacked():
    """A partial solution is a valid answer; breaking the limit is not."""
    result = solve_placements(
        [ask("a", windows=[rng(MON, 9, 13)])], OPEN, CFG,
        client_limits={"erin": ClientLimit(per_day=1, fixed=[at(MON, 9)])})

    assert result.unplaced == ("a",)


def test_once_a_week_pushes_the_second_into_the_next_week():
    result = solve_placements([ask("a"), ask("b")], OPEN, CFG,
                              client_limits={"erin": ClientLimit(per_week=1)})

    assert days(result) == [MON, NEXT_MON]


def test_day_and_week_limits_apply_together():
    result = solve_placements(
        [ask("a"), ask("b"), ask("c")], OPEN, CFG,
        client_limits={"erin": ClientLimit(per_day=1, per_week=2)})

    assert days(result) == [MON, TUE, NEXT_MON]


def test_a_move_cannot_stack_a_clients_day():
    """Erin has Monday 09:00 and Tuesday 09:00, both movable. Iris needs
    Monday 09:00. Moving Erin later on Monday is fine; moving her onto
    Tuesday, where she already is, is not."""
    erin_mon = MovableAppointment("m1", "erin", rng(MON, 9, 10),
                                  [rng(TUE, 10, 11)])        # only a stacking move offered
    erin_tue = MovableAppointment("m2", "erin", rng(TUE, 9, 10), [])
    free = [seg(MON, 10, 13), seg(TUE, 10, 13)]
    iris = BookingRequest("r", "iris", 60, [rng(MON, 9, 10)])

    unlimited = solve_placements([iris], free, CFG, movable=[erin_mon, erin_tue],
                                 max_displacements=1)
    limited = solve_placements([iris], free, CFG, movable=[erin_mon, erin_tue],
                               max_displacements=1,
                               client_limits={"erin": ClientLimit(per_day=1)})

    assert [d.now.start for d in unlimited.displacements] == [at(TUE, 10)]
    assert limited.displacements == () and limited.unplaced == ("r",)


def test_a_day_already_over_the_limit_is_left_alone():
    """Two bookings on Monday from before the limit existed. Nobody is moved
    to satisfy it, and the solve does not fail."""
    first = MovableAppointment("m1", "erin", rng(MON, 9, 10), [rng(TUE, 9, 10)])
    second = MovableAppointment("m2", "erin", rng(MON, 10, 11), [rng(WED, 9, 10)])
    result = solve_placements([ask("x", client="iris", windows=[rng(WED, 11, 12)])],
                              [seg(MON, 11, 13), seg(TUE, 9, 13), seg(WED, 9, 13)], CFG,
                              movable=[first, second], max_displacements=2,
                              client_limits={"erin": ClientLimit(per_day=1)})

    assert result.displacements == ()
    assert result.all_placed


def test_nor_can_anything_more_be_added_to_it():
    result = solve_placements(
        [ask("a", windows=[rng(MON, 9, 13)])], OPEN, CFG,
        client_limits={"erin": ClientLimit(per_day=1, fixed=[at(MON, 9), at(MON, 10)])})

    assert result.unplaced == ("a",)


@pytest.mark.parametrize("limit", [ClientLimit(per_day=0), ClientLimit(per_week=0)])
def test_a_limit_of_zero_is_refused(limit):
    """Zero would mean "never book this client", which is not a limit — it is
    a decision that belongs somewhere else."""
    with pytest.raises(ValueError):
        solve_placements([ask("a")], OPEN, CFG, client_limits={"erin": limit})
