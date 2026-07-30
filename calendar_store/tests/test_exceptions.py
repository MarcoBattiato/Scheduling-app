from datetime import date, datetime, time

import portion as P

from calendar_store import AvailabilityStore, Kind

THU = 3


def dt(d: date, t: time) -> datetime:
    return datetime.combine(d, t)


def slot(d: date, start: time, end: time) -> P.Interval:
    return P.closedopen(dt(d, start), dt(d, end))


def test_positive_exception_on_a_day_with_no_rule_is_kept():
    store = AvailabilityStore()
    store.add_exception_availability("alice", date(2026, 5, 15), time(16, 0), time(18, 0))

    exceptions = store.exceptions_for("alice", date(2026, 5, 15))
    assert len(exceptions) == 1
    e = exceptions[0]
    assert e.kind is Kind.ADD
    assert (e.start_time, e.end_time) == (time(16, 0), time(18, 0))


def test_negative_exception_with_no_overlapping_rule_is_discarded():
    store = AvailabilityStore()
    # No rule at all for this client/date.
    store.remove_exception_availability("alice", date(2026, 5, 15), time(16, 0), time(18, 0))

    assert store.exceptions_for("alice", date(2026, 5, 15)) == []


def test_positive_exception_fully_inside_rule_coverage_is_discarded():
    store = AvailabilityStore()
    store.add_recurring_availability(
        "alice", THU, time(14, 0), time(18, 0), effective_from=date(2026, 1, 1),
    )
    # 2026-05-14 is a Thursday, already covered 14:00-18:00 by the rule.
    store.add_exception_availability("alice", date(2026, 5, 14), time(15, 0), time(16, 0))

    assert store.exceptions_for("alice", date(2026, 5, 14)) == []


def test_positive_exception_partially_overlapping_rule_keeps_only_the_new_part():
    store = AvailabilityStore()
    store.add_recurring_availability(
        "alice", THU, time(14, 0), time(18, 0), effective_from=date(2026, 1, 1),
    )
    store.add_exception_availability("alice", date(2026, 5, 14), time(16, 0), time(20, 0))

    exceptions = store.exceptions_for("alice", date(2026, 5, 14))
    assert len(exceptions) == 1
    e = exceptions[0]
    assert e.kind is Kind.ADD
    assert (e.start_time, e.end_time) == (time(18, 0), time(20, 0))


def test_negative_exception_partially_overlapping_rule_keeps_only_the_removable_part():
    store = AvailabilityStore()
    store.add_recurring_availability(
        "alice", THU, time(14, 0), time(18, 0), effective_from=date(2026, 1, 1),
    )
    store.remove_exception_availability("alice", date(2026, 5, 14), time(16, 0), time(20, 0))

    exceptions = store.exceptions_for("alice", date(2026, 5, 14))
    assert len(exceptions) == 1
    e = exceptions[0]
    assert e.kind is Kind.REMOVE
    assert (e.start_time, e.end_time) == (time(16, 0), time(18, 0))


def test_only_available_narrower_window_on_specific_date_via_remove_then_add():
    """The scenario from the very first design discussion: normally
    Thursday 2pm-6pm, but on 2026-05-14 only available 4pm-6pm."""
    store = AvailabilityStore()
    store.add_recurring_availability(
        "alice", THU, time(14, 0), time(18, 0), effective_from=date(2026, 1, 1),
    )
    store.remove_exception_availability("alice", date(2026, 5, 14), time(14, 0), time(18, 0))
    store.add_exception_availability("alice", date(2026, 5, 14), time(16, 0), time(18, 0))

    calendar = store.get_availability("alice", date(2026, 5, 7), date(2026, 5, 15))
    expected = slot(date(2026, 5, 7), time(14, 0), time(18, 0)) | slot(
        date(2026, 5, 14), time(16, 0), time(18, 0)
    )
    assert calendar == expected


def test_exception_cancels_a_previous_exception_even_without_a_rule():
    store = AvailabilityStore()
    store.add_exception_availability("alice", date(2026, 5, 15), time(8, 0), time(9, 0))
    store.remove_exception_availability("alice", date(2026, 5, 15), time(8, 0), time(9, 0))

    assert store.exceptions_for("alice", date(2026, 5, 15)) == []


def test_readd_after_remove_restores_exception_availability():
    store = AvailabilityStore()
    store.add_exception_availability("alice", date(2026, 5, 15), time(8, 0), time(9, 0))
    store.remove_exception_availability("alice", date(2026, 5, 15), time(8, 0), time(9, 0))
    store.add_exception_availability("alice", date(2026, 5, 15), time(8, 0), time(9, 0))

    exceptions = store.exceptions_for("alice", date(2026, 5, 15))
    assert len(exceptions) == 1
    assert exceptions[0].kind is Kind.ADD

    calendar = store.get_availability("alice", date(2026, 5, 15), date(2026, 5, 16))
    assert calendar == slot(date(2026, 5, 15), time(8, 0), time(9, 0))


def test_removing_a_middle_slice_of_a_prior_exception_splits_it():
    store = AvailabilityStore()
    store.add_exception_availability("alice", date(2026, 5, 15), time(8, 0), time(12, 0))
    store.remove_exception_availability("alice", date(2026, 5, 15), time(9, 0), time(10, 0))

    exceptions = sorted(store.exceptions_for("alice", date(2026, 5, 15)), key=lambda e: e.start_time)
    assert [(e.kind, e.start_time, e.end_time) for e in exceptions] == [
        (Kind.ADD, time(8, 0), time(9, 0)),
        (Kind.ADD, time(10, 0), time(12, 0)),
    ]


def test_exceptions_on_different_dates_dont_interact():
    store = AvailabilityStore()
    store.add_exception_availability("alice", date(2026, 5, 14), time(8, 0), time(9, 0))
    store.remove_exception_availability("alice", date(2026, 5, 15), time(8, 0), time(9, 0))

    assert len(store.exceptions_for("alice", date(2026, 5, 14))) == 1
    assert store.exceptions_for("alice", date(2026, 5, 15)) == []
