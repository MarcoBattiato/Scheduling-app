from datetime import date, datetime, time

import pytest
from calendar_store import TimeSegment

from scheduling_engine import BookingRequest, CostConfig, TimeRange, solve_placements

DAY = date(2026, 5, 5)


def at(hour: int, minute: int = 0) -> datetime:
    return datetime.combine(DAY, time(hour, minute))


def free(start, end):
    return TimeSegment(start, end)


def request(id_, duration, windows, client="alice"):
    return BookingRequest(
        id=id_,
        client_id=client,
        duration_minutes=duration,
        desired=[TimeRange(s, e) for s, e in windows],
    )


PACK = CostConfig(alpha=0.9)     # fragmentation-dominant
EARLY = CostConfig(alpha=0.0)    # earliness-only


def test_packing_prefers_a_slot_that_leaves_reusable_gaps():
    # 09:30 is the earliest option but strands 30 unusable minutes ahead of it;
    # 10:00 leaves two clean 60-minute gaps instead.
    result = solve_placements(
        [request("r1", 60, [(at(9, 30), at(12))])],
        [free(at(9), at(12))],
        PACK,
    )

    assert result.all_placed
    assert result.placements[0].range == TimeRange(at(10), at(11))
    assert result.fragmentation_minutes == 0


def test_earliness_weighting_takes_the_first_available_slot_instead():
    result = solve_placements(
        [request("r1", 60, [(at(9, 30), at(12))])],
        [free(at(9), at(12))],
        EARLY,
    )

    assert result.placements[0].range == TimeRange(at(9, 30), at(10, 30))
    assert result.preference_gap_minutes == 0


def test_touching_free_segments_count_as_one_gap():
    # Split input, identical answer: an adjacent pair is one usable block, and
    # scoring them apart would invent fragmentation that isn't there.
    result = solve_placements(
        [request("r1", 60, [(at(9, 30), at(12))])],
        [free(at(9), at(10)), free(at(10), at(12))],
        PACK,
    )

    assert result.placements[0].range == TimeRange(at(10), at(11))


def test_queue_is_solved_jointly_not_one_at_a_time():
    result = solve_placements(
        [
            request("short", 60, [(at(9), at(12))], client="alice"),
            request("long", 90, [(at(9), at(12))], client="bob"),
        ],
        [free(at(9), at(12))],
        CostConfig(alpha=0.5),
    )

    assert result.all_placed
    by_id = {p.request_id: p.range for p in result.placements}
    # Both orderings strand the same 30 unusable minutes, so earliness decides:
    # the 60 goes first, because putting the 90 first delays the other by more.
    assert by_id["short"] == TimeRange(at(9), at(10))
    assert by_id["long"] == TimeRange(at(10), at(11, 30))
    assert result.fragmentation_minutes == 30


def test_placements_never_overlap():
    result = solve_placements(
        [request(f"r{i}", 60, [(at(9), at(12))], client=f"c{i}") for i in range(3)],
        [free(at(9), at(12))],
        PACK,
    )

    assert result.all_placed
    ranges = sorted((p.range for p in result.placements), key=lambda r: r.start)
    for earlier, later in zip(ranges, ranges[1:]):
        assert earlier.end <= later.start


def test_partial_solution_when_the_queue_does_not_fit():
    result = solve_placements(
        [request(f"r{i}", 90, [(at(9), at(12))], client=f"c{i}") for i in range(3)],
        [free(at(9), at(12))],
        PACK,
    )

    assert len(result.placements) == 2
    assert len(result.unplaced) == 1
    assert not result.all_placed


def test_placement_stays_inside_the_requested_windows():
    result = solve_placements(
        [request("r1", 60, [(at(14), at(15, 30))])],
        [free(at(9), at(17))],
        PACK,
    )

    placed = result.placements[0].range
    assert at(14) <= placed.start and placed.end <= at(15, 30)


def test_appointment_may_not_bridge_two_separate_free_blocks():
    # 30 free minutes either side of a booked hour: contiguous on paper, but
    # no 60-minute appointment can span the break.
    result = solve_placements(
        [request("r1", 60, [(at(9), at(11))])],
        [free(at(9), at(9, 30)), free(at(10, 30), at(11))],
        PACK,
    )

    assert result.unplaced == ("r1",)


def test_request_outside_provider_availability_is_unplaced():
    result = solve_placements(
        [request("r1", 60, [(at(18), at(20))])],
        [free(at(9), at(12))],
        PACK,
    )

    assert result.unplaced == ("r1",)
    assert result.placements == ()


def test_empty_queue_is_not_an_error():
    assert solve_placements([], [free(at(9), at(12))], PACK).placements == ()


# A calendar carrying 30 unusable minutes whatever anyone does with it: too
# short for the shortest service, so it is waste in every possible outcome.
STRANDED = [free(at(9), at(9, 30)), free(at(13), at(15))]


def test_reported_fragmentation_means_the_same_thing_on_every_path():
    # Whether a request happens to be placeable changes what the solver does,
    # not what the calendar wastes. Reporting 0 when nothing is placeable would
    # make the figure incomparable between passes.
    placeable = solve_placements([request("r1", 60, [(at(13), at(15))])], STRANDED, PACK)
    unplaceable = solve_placements([request("r1", 60, [(at(20), at(23))])], STRANDED, PACK)
    idle = solve_placements([], STRANDED, PACK)

    assert placeable.fragmentation_minutes == 30
    assert unplaceable.fragmentation_minutes == 30
    assert idle.fragmentation_minutes == 30
    assert unplaceable.unplaced == ("r1",)


def test_alpha_one_takes_the_earliest_of_the_equally_packed_slots():
    # 09:00, 10:00 and 11:00 all leave nothing but reusable gaps, so packing
    # alone cannot separate them. The slider being at "packing" should not mean
    # the tie is settled at random.
    result = solve_placements(
        [request("r1", 60, [(at(9), at(12))])], [free(at(9), at(12))],
        CostConfig(alpha=1.0),
    )

    assert result.placements[0].range == TimeRange(at(9), at(10))
    assert result.fragmentation_minutes == 0


@pytest.mark.parametrize("alpha", [0.0, 1.0])
def test_the_extremes_of_the_slider_are_deterministic(alpha):
    """At alpha 0 and 1 one cost term has no weight, so a great many
    arrangements tie. Identical input must still give an identical answer —
    otherwise the same scenario reports different numbers on each run.
    """
    requests = [
        request(f"r{i}", 60, [(at(9), at(17))], client=f"c{i}") for i in range(3)
    ]
    answers = {
        tuple((p.request_id, p.range.start) for p in
              solve_placements(requests, [free(at(9), at(17))],
                               CostConfig(alpha=alpha)).placements)
        for _ in range(5)
    }

    assert len(answers) == 1, f"{len(answers)} different answers across 5 runs"


def test_duplicate_request_ids_are_rejected():
    # Results are keyed by id, so duplicates would come back indistinguishable.
    with pytest.raises(ValueError, match="duplicate request id"):
        solve_placements(
            [request("r1", 60, [(at(9), at(12))]), request("r1", 60, [(at(9), at(12))])],
            [free(at(9), at(12))],
            PACK,
        )


def test_duration_off_the_grid_is_rejected():
    with pytest.raises(ValueError, match="multiple of grid_minutes"):
        solve_placements(
            [request("r1", 50, [(at(9), at(12))])],
            [free(at(9), at(12))],
            PACK,
        )


def test_alpha_outside_the_slider_range_is_rejected():
    with pytest.raises(ValueError, match="alpha"):
        CostConfig(alpha=1.5)


# --------------------------------------------------------------------------
# Preferred slots
# --------------------------------------------------------------------------


def wants(id_, duration, windows, preferred, client="alice"):
    return BookingRequest(
        id=id_, client_id=client, duration_minutes=duration,
        desired=[TimeRange(s, e) for s, e in windows],
        preferred_start=preferred,
    )


def test_a_request_lands_on_its_preferred_slot_when_it_can():
    result = solve_placements(
        [wants("r1", 60, [(at(9), at(17))], preferred=at(14))],
        [free(at(9), at(17))], CostConfig(alpha=0.0),
    )

    assert result.placements[0].range.start == at(14)
    assert result.preference_gap_minutes == 0


def test_a_preference_is_a_wish_not_a_constraint():
    """The whole point of the change: a client naming a slot must not become
    unplaceable elsewhere, or a narrow ask turns into a claim on that hour.
    """
    result = solve_placements(
        [wants("r1", 60, [(at(9), at(17))], preferred=at(14)),
         wants("r2", 60, [(at(9), at(17))], preferred=at(14), client="bob")],
        [free(at(9), at(17))], CostConfig(alpha=0.0),
    )

    assert result.all_placed, "both are placed; only one gets the hour it wanted"
    assert result.preference_gap_minutes == 60


def test_with_no_preference_the_earliest_slot_is_the_preference():
    """Which is exactly the old behaviour, now as a special case."""
    stated = solve_placements(
        [wants("r1", 60, [(at(9), at(17))], preferred=at(9))],
        [free(at(9), at(17))], CostConfig(alpha=0.0),
    )
    silent = solve_placements(
        [request("r1", 60, [(at(9), at(17))])],
        [free(at(9), at(17))], CostConfig(alpha=0.0),
    )

    assert stated.placements[0].range == silent.placements[0].range
    assert stated.preference_gap_minutes == silent.preference_gap_minutes == 0


def test_a_narrow_ask_no_longer_unseats_a_settled_booking_for_free():
    """The case that prompted this. Both clients can be booked anywhere in the
    day; both want 14:00. Whoever is already there should not be evicted
    merely because the newcomer described a narrower wish.
    """
    from scheduling_engine import MovableAppointment

    settled = MovableAppointment(
        id="a1", client_id="alice", range=TimeRange(at(14), at(15)),
        allowed=[TimeRange(at(9), at(17))], preferred_start=at(14),
    )
    newcomer = wants("r1", 60, [(at(9), at(17))], preferred=at(14), client="bob")

    result = solve_placements(
        [newcomer], [free(at(9), at(14)), free(at(15), at(17))],
        CostConfig(alpha=0.0), movable=[settled], max_displacements=1,
    )

    assert result.all_placed
    assert not result.displacements, "alice keeps the hour she asked for"


def test_a_displaced_booking_can_be_moved_back_towards_its_wish_for_free():
    """Distance is measured from what the client asked for, not from wherever
    they were last put — so returning someone to their slot is not a cost.
    """
    from scheduling_engine import MovableAppointment

    pushed_away = MovableAppointment(
        id="a1", client_id="alice", range=TimeRange(at(16), at(17)),
        allowed=[TimeRange(at(9), at(17))], preferred_start=at(9),
    )

    result = solve_placements(
        [request("r1", 60, [(at(16), at(17))], client="bob")],
        [free(at(9), at(10))],
        CostConfig(alpha=0.0), movable=[pushed_away], max_displacements=1,
    )

    assert result.all_placed
    (moved,) = result.displacements
    assert moved.now.start == at(9), "moved back to where alice actually wanted"
    assert result.preference_gap_minutes == 0
