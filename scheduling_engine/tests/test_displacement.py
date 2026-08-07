from datetime import date, datetime, time

import pytest
from calendar_store import TimeSegment

from scheduling_engine import (
    BookingRequest,
    CostConfig,
    MovableAppointment,
    RescheduleBounds,
    TimeRange,
    free_time,
    reschedule_windows,
    solve_placements,
)

MON, TUE, WED = date(2026, 5, 4), date(2026, 5, 5), date(2026, 5, 6)
CFG = CostConfig(alpha=0.5)


def at(day, hour, minute=0):
    return datetime.combine(day, time(hour, minute))


def seg(day, from_hour, to_hour):
    return TimeSegment(at(day, from_hour), at(day, to_hour))


def rng(day, from_hour, to_hour):
    return TimeRange(at(day, from_hour), at(day, to_hour))


def request(id_, duration, windows, client="alice"):
    return BookingRequest(id_, client, duration, list(windows))


def booking(id_, client, current, allowed):
    return MovableAppointment(id_, client, current, list(allowed))


# Monday holds one booking and one spare hour; Tuesday is wide open. A
# 90-minute request cannot fit on Monday unless the booking moves.
DANA = rng(MON, 9, 10)
AVAILABILITY = [seg(MON, 9, 11), seg(TUE, 9, 12)]
FREE = free_time(AVAILABILITY, [DANA])
MOVABLE = booking("appt-dana", "dana", DANA, [rng(TUE, 9, 12)])
NEEDS_ROOM = [request("r1", 90, [rng(MON, 9, 11)])]


def test_displacement_is_off_unless_asked_for():
    result = solve_placements(NEEDS_ROOM, FREE, CFG, movable=[MOVABLE])

    assert result.unplaced == ("r1",)
    assert result.displacements == ()


def test_displacement_rescues_a_request_that_otherwise_will_not_fit():
    result = solve_placements(NEEDS_ROOM, FREE, CFG, movable=[MOVABLE], max_displacements=1)

    assert result.all_placed
    assert result.placements[0].range == rng(MON, 9, 10).__class__(at(MON, 9), at(MON, 10, 30))

    (moved,) = result.displacements
    assert moved.appointment_id == "appt-dana"
    assert moved.client_id == "dana"
    assert moved.was == DANA
    assert moved.now.start.date() == TUE


def test_nobody_is_disturbed_when_the_request_fits_anyway():
    """Rescheduling is a last resort, not merely another option: an
    arrangement needing no displacement must beat one that does.
    """
    result = solve_placements(
        [request("r1", 60, [rng(TUE, 9, 12)])], FREE, CFG,
        movable=[MOVABLE], max_displacements=2,
    )

    assert result.all_placed
    assert result.displacements == ()


def test_a_displaced_booking_keeps_its_full_duration():
    result = solve_placements(NEEDS_ROOM, FREE, CFG, movable=[MOVABLE], max_displacements=1)

    (moved,) = result.displacements
    assert moved.now.end - moved.now.start == moved.was.end - moved.was.start


def test_the_cap_is_a_hard_ceiling():
    # Monday is booked solid by two clients, and the request needs the whole of
    # it — so both must move or neither placement is possible.
    first, second = rng(MON, 9, 10), rng(MON, 10, 11)
    free = free_time([seg(MON, 9, 11), seg(TUE, 9, 17)], [first, second])
    movable = [
        booking("a1", "dana", first, [rng(TUE, 9, 17)]),
        booking("a2", "erik", second, [rng(TUE, 9, 17)]),
    ]
    requests = [request("r1", 120, [rng(MON, 9, 11)])]

    capped = solve_placements(requests, free, CFG, movable=movable, max_displacements=1)
    assert capped.unplaced == ("r1",)
    assert len(capped.displacements) <= 1

    allowed = solve_placements(requests, free, CFG, movable=movable, max_displacements=2)
    assert allowed.all_placed
    assert len(allowed.displacements) == 2


def test_fewest_displacements_wins_among_equal_placements():
    """One booking in the way can be sidestepped by moving one client; the
    solver must not move two just because it is permitted to.
    """
    free = free_time([seg(MON, 9, 12), seg(TUE, 9, 17)], [rng(MON, 9, 10), rng(MON, 11, 12)])
    movable = [
        booking("a1", "dana", rng(MON, 9, 10), [rng(TUE, 9, 17)]),
        booking("a2", "erik", rng(MON, 11, 12), [rng(TUE, 9, 17)]),
    ]

    result = solve_placements(
        [request("r1", 60, [rng(MON, 9, 12)])], free, CFG,
        movable=movable, max_displacements=2,
    )

    assert result.all_placed
    # 10:00-11:00 was free all along.
    assert result.displacements == ()


def test_a_booking_may_only_move_where_its_client_allows():
    result = solve_placements(NEEDS_ROOM, FREE, CFG, movable=[MOVABLE], max_displacements=1)

    (moved,) = result.displacements
    assert any(w.start <= moved.now.start and moved.now.end <= w.end
               for w in MOVABLE.allowed)


def test_a_booking_with_nowhere_to_go_simply_stays():
    stuck = booking("appt-dana", "dana", DANA, [])  # no availability anywhere

    result = solve_placements(NEEDS_ROOM, FREE, CFG, movable=[stuck], max_displacements=2)

    assert result.unplaced == ("r1",)
    assert result.displacements == ()


def test_bookings_are_never_cancelled_to_make_room():
    """A displaced booking must land somewhere; the solver has no power to
    simply delete a third party's appointment (SPEC.md §7.2).
    """
    result = solve_placements(NEEDS_ROOM, FREE, CFG, movable=[MOVABLE], max_displacements=1)

    moved_ids = {d.appointment_id for d in result.displacements}
    assert moved_ids == {"appt-dana"}, "the booking is accounted for, not dropped"


def test_placements_never_collide_with_a_booking_that_stayed():
    free = free_time([seg(MON, 9, 17)], [rng(MON, 12, 13)])
    stays = booking("lunch", "provider-self", rng(MON, 12, 13), [])

    result = solve_placements(
        [request(f"r{i}", 60, [rng(MON, 9, 17)], client=f"c{i}") for i in range(4)],
        free, CFG, movable=[stays], max_displacements=1,
    )

    for placement in result.placements:
        assert not (placement.range.start < at(MON, 13)
                    and at(MON, 12) < placement.range.end), "overlaps the block that stayed"


def test_chains_are_forbidden_by_default_and_available_on_request():
    """A chain is one displaced booking taking the slot another is vacating.
    Off by default: every new placement should hang on one client's consent.
    """
    first, second = rng(MON, 9, 10), rng(MON, 10, 11)
    free = free_time([seg(MON, 9, 11), seg(TUE, 9, 10)], [first, second])
    movable = [
        booking("a1", "dana", first, [rng(MON, 10, 11)]),   # only into a2's slot
        booking("a2", "erik", second, [rng(TUE, 9, 10)]),
    ]
    requests = [request("r1", 60, [rng(MON, 9, 10)])]

    assert solve_placements(requests, free, CFG, movable=movable,
                            max_displacements=2).unplaced == ("r1",)

    chained = solve_placements(requests, free, CFG, movable=movable,
                               max_displacements=2, allow_chains=True)
    assert chained.all_placed
    assert len(chained.displacements) == 2


@pytest.mark.parametrize("alpha", [0.0, 0.5, 1.0])
def test_displacement_results_are_deterministic(alpha):
    free = free_time([seg(MON, 9, 17), seg(TUE, 9, 17)], [rng(MON, 9, 10), rng(MON, 10, 11)])
    movable = [
        booking("a1", "dana", rng(MON, 9, 10), [rng(TUE, 9, 17)]),
        booking("a2", "erik", rng(MON, 10, 11), [rng(TUE, 9, 17)]),
    ]
    requests = [request(f"r{i}", 90, [rng(MON, 9, 12)], client=f"c{i}") for i in range(3)]

    answers = {
        tuple(sorted((d.appointment_id, d.now.start) for d in solve_placements(
            requests, free, CostConfig(alpha=alpha), movable=movable,
            max_displacements=2).displacements))
        for _ in range(5)
    }
    assert len(answers) == 1


# --------------------------------------------------------------------------
# reschedule_windows
# --------------------------------------------------------------------------


def test_reschedule_windows_clip_availability_to_the_allowed_days():
    availability = [seg(MON, 9, 17), seg(TUE, 9, 17), seg(WED, 9, 17)]

    windows = reschedule_windows(rng(TUE, 10, 11), availability, RescheduleBounds(0, 0))
    assert [w.start.date() for w in windows] == [TUE]

    windows = reschedule_windows(rng(TUE, 10, 11), availability, RescheduleBounds(1, 1))
    assert [w.start.date() for w in windows] == [MON, TUE, WED]

    windows = reschedule_windows(rng(TUE, 10, 11), availability, RescheduleBounds(0, 1))
    assert [w.start.date() for w in windows] == [TUE, WED]


def test_zero_bounds_confine_a_provider_block_to_its_own_day():
    """`{0, 0}` is what stops a lunch break being pushed to another date
    (SPEC.md §4) — a hard constraint, not an expensive option.
    """
    availability = [seg(MON, 9, 17), seg(TUE, 9, 17)]
    windows = reschedule_windows(rng(MON, 12, 13), availability, RescheduleBounds(0, 0))

    assert len(windows) == 1
    assert windows[0].start.date() == MON


def test_reschedule_windows_are_empty_when_the_client_is_never_free():
    assert reschedule_windows(rng(MON, 9, 10), [], RescheduleBounds(3, 3)) == []


# --------------------------------------------------------------------------
# Reported dependencies
# --------------------------------------------------------------------------


def test_a_placement_reports_the_move_it_is_waiting_on():
    result = solve_placements(NEEDS_ROOM, FREE, CFG, movable=[MOVABLE], max_displacements=1)

    (placement,) = result.placements
    assert placement.depends_on == ("appt-dana",)


def test_a_placement_that_needed_nobody_depends_on_nobody():
    result = solve_placements(
        [request("r1", 60, [rng(TUE, 9, 12)])], FREE, CFG,
        movable=[MOVABLE], max_displacements=2,
    )

    (placement,) = result.placements
    assert placement.depends_on == ()
    assert result.displacements == ()


def test_a_chained_move_reports_what_it_is_waiting_on():
    """The case overlap alone cannot answer. With chains permitted, a1 lands in
    the slot a2 is vacating — a dependency between two *moves*, which comparing
    a placement against displacement ranges would never reveal.
    """
    first, second = rng(MON, 9, 10), rng(MON, 10, 11)
    free = free_time([seg(MON, 9, 11), seg(TUE, 9, 10)], [first, second])
    movable = [
        booking("a1", "dana", first, [rng(MON, 10, 11)]),   # only into a2's slot
        booking("a2", "erik", second, [rng(TUE, 9, 10)]),
    ]

    result = solve_placements(
        [request("r1", 60, [rng(MON, 9, 10)])], free, CFG,
        movable=movable, max_displacements=2, allow_chains=True,
    )

    assert result.all_placed
    moves = {d.appointment_id: d for d in result.displacements}
    assert moves["a1"].depends_on == ("a2",), "a1 cannot move until a2 has"
    assert moves["a2"].depends_on == (), "a2 moves into space already free"
    (placement,) = result.placements
    assert placement.depends_on == ("a1",)


def test_a_move_never_reports_itself_as_its_own_dependency():
    result = solve_placements(NEEDS_ROOM, FREE, CFG, movable=[MOVABLE], max_displacements=1)

    (moved,) = result.displacements
    assert moved.appointment_id not in moved.depends_on
