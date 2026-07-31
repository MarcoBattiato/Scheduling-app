from datetime import date, datetime, time

import pytest
from calendar_store import TimeSegment

from scheduling_engine import (
    BookingRequest,
    CostConfig,
    Track,
    TimeRange,
    gaps_left,
    render,
    solve_placements,
)

DAY = date(2026, 5, 5)
CFG = CostConfig(alpha=0.5)


def at(hour, minute=0):
    return datetime.combine(DAY, time(hour, minute))


def request(id_, duration, windows, client="alice"):
    return BookingRequest(id_, client, duration, [TimeRange(s, e) for s, e in windows])


SCENARIOS = [
    ([segment := TimeSegment(at(9), at(12))], [request("r1", 60, [(at(9, 30), at(12))])]),
    ([TimeSegment(at(9), at(12))], [request("r1", 60, [(at(9), at(12))]),
                                    request("r2", 90, [(at(9), at(12))], client="bob")]),
    ([TimeSegment(at(9), at(11)), TimeSegment(at(12), at(17))],
     [request("r1", 90, [(at(9), at(17))]), request("r2", 60, [(at(9), at(17))], "bob")]),
    ([TimeSegment(at(9), at(9, 30)), TimeSegment(at(13), at(15))],
     [request("r1", 60, [(at(13), at(15))])]),
]


@pytest.mark.parametrize("provider_free,requests", SCENARIOS)
@pytest.mark.parametrize("alpha", [0.0, 0.5, 1.0])
def test_gap_report_totals_agree_with_what_the_solver_optimised(
    provider_free, requests, alpha
):
    """The whole point of the gap report is to explain the fragmentation
    figure. If the two ever disagree, the visualisation is lying about the
    thing it exists to show.
    """
    config = CostConfig(alpha=alpha)
    result = solve_placements(requests, provider_free, config)

    gaps = gaps_left(provider_free, result.placements, config)
    assert sum(gap.wasted_minutes for gap in gaps) == result.fragmentation_minutes


def test_gaps_are_reported_in_order_and_do_not_overlap_placements():
    provider_free = [TimeSegment(at(9), at(12))]
    result = solve_placements([request("r1", 60, [(at(10), at(11))])], provider_free, CFG)

    gaps = gaps_left(provider_free, result.placements, CFG)
    assert [(g.start, g.end) for g in gaps] == [(at(9), at(10)), (at(11), at(12))]
    assert [g.usable for g in gaps] == [True, True]


def test_short_gap_is_flagged_unusable():
    (gap,) = gaps_left([TimeSegment(at(9), at(9, 30))], [], CFG)
    assert gap.minutes == 30
    assert not gap.usable
    assert gap.wasted_minutes == 30


def test_render_shows_placements_gaps_and_totals():
    provider_free = [TimeSegment(at(9), at(12))]
    result = solve_placements([request("r1", 60, [(at(9, 30), at(12))])], provider_free, CFG)

    chart = render(provider_free, result)

    assert "Tue 2026-05-05" in chart
    assert "r1 (alice)" in chart
    assert "placed 1/1" in chart
    assert "fragmentation" in chart
    assert "gap " in chart


def test_render_labels_never_collide_with_their_row():
    result = solve_placements(
        [request("r1", 60, [(at(9), at(12))])], [TimeSegment(at(9), at(12))], CFG
    )
    chart = render([TimeSegment(at(9), at(12))], result,
                   tracks=[Track("a-very-long-track-name", [TimeSegment(at(9), at(10))])])

    # Track rows are the ones whose label starts immediately after the indent;
    # the hour axis is indented further and has no label.
    rows = [ln for ln in chart.splitlines() if ln.startswith("  ") and ln[2:3].strip()]
    assert rows, "expected at least the free/placed/track rows"
    for row in rows:
        name = row.strip().split()[0]
        assert row.startswith(f"  {name} "), f"label ran into its row: {row!r}"


def test_render_of_an_empty_calendar_is_not_an_error():
    assert render([], None) == "(nothing to show)"


def test_render_without_a_result_still_shows_the_calendar():
    chart = render([TimeSegment(at(9), at(12))])
    assert "free" in chart
    assert "placed" not in chart
