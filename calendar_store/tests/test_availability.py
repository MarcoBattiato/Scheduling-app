from datetime import date, datetime, time

import portion as P

from calendar_store import AvailabilityStore, crop, intersect, negate, union

TUE, WED = 1, 2


def dt(d: date, t: time) -> datetime:
    return datetime.combine(d, t)


def slot(d: date, start: time, end: time) -> P.Interval:
    return P.closedopen(dt(d, start), dt(d, end))


def test_add_to_empty_store_creates_one_rule():
    store = AvailabilityStore()
    store.add_recurring_availability(
        "alice", TUE, time(15, 0), time(19, 0), effective_from=date(2026, 1, 1),
    )

    rules = store.rules_for("alice", TUE)
    assert len(rules) == 1
    r = rules[0]
    assert (r.start_time, r.end_time) == (time(15, 0), time(19, 0))
    assert (r.effective_from, r.effective_until) == (date(2026, 1, 1), None)


def test_add_adjacent_date_ranges_with_same_time_range_merges_into_one_rule():
    store = AvailabilityStore()
    store.add_recurring_availability(
        "alice", TUE, time(15, 0), time(19, 0),
        effective_from=date(2026, 1, 1), effective_until=date(2026, 6, 1),
    )
    store.add_recurring_availability(
        "alice", TUE, time(15, 0), time(19, 0),
        effective_from=date(2026, 6, 1),
    )

    rules = store.rules_for("alice", TUE)
    assert len(rules) == 1
    r = rules[0]
    assert (r.start_time, r.end_time) == (time(15, 0), time(19, 0))
    assert (r.effective_from, r.effective_until) == (date(2026, 1, 1), None)


def test_add_overlapping_time_and_date_range_merges_to_one_covering_rule():
    store = AvailabilityStore()
    store.add_recurring_availability(
        "alice", TUE, time(15, 0), time(18, 0), effective_from=date(2026, 1, 1),
    )
    store.add_recurring_availability(
        "alice", TUE, time(17, 0), time(19, 0), effective_from=date(2026, 1, 1),
    )

    rules = store.rules_for("alice", TUE)
    assert len(rules) == 1
    r = rules[0]
    assert (r.start_time, r.end_time) == (time(15, 0), time(19, 0))


def test_remove_exact_match_leaves_nothing():
    store = AvailabilityStore()
    store.add_recurring_availability(
        "alice", TUE, time(15, 0), time(19, 0), effective_from=date(2026, 1, 1),
    )
    store.remove_recurring_availability(
        "alice", TUE, time(15, 0), time(19, 0), effective_from=date(2026, 1, 1),
    )

    assert store.rules_for("alice", TUE) == []


def test_remove_middle_slice_splits_into_two_rules():
    store = AvailabilityStore()
    store.add_recurring_availability(
        "alice", TUE, time(15, 0), time(19, 0), effective_from=date(2026, 1, 1),
    )
    store.remove_recurring_availability(
        "alice", TUE, time(17, 0), time(18, 0), effective_from=date(2026, 1, 1),
    )

    rules = sorted(store.rules_for("alice", TUE), key=lambda r: r.start_time)
    assert [(r.start_time, r.end_time) for r in rules] == [
        (time(15, 0), time(17, 0)),
        (time(18, 0), time(19, 0)),
    ]
    assert all((r.effective_from, r.effective_until) == (date(2026, 1, 1), None) for r in rules)


def test_remove_partial_date_range_splits_into_before_during_after():
    """The worked example from SPEC.md."""
    store = AvailabilityStore()
    store.add_recurring_availability(
        "alice", WED, time(15, 0), time(20, 0), effective_from=date(2025, 1, 1),
    )
    store.remove_recurring_availability(
        "alice", WED, time(17, 0), time(18, 0),
        effective_from=date(2025, 12, 1), effective_until=date(2026, 3, 1),
    )

    rules = sorted(
        store.rules_for("alice", WED), key=lambda r: (r.effective_from, r.start_time)
    )
    actual = [
        (r.effective_from, r.effective_until, r.start_time, r.end_time) for r in rules
    ]
    assert actual == [
        (date(2025, 1, 1), date(2025, 12, 1), time(15, 0), time(20, 0)),
        (date(2025, 12, 1), date(2026, 3, 1), time(15, 0), time(17, 0)),
        (date(2025, 12, 1), date(2026, 3, 1), time(18, 0), time(20, 0)),
        (date(2026, 3, 1), None, time(15, 0), time(20, 0)),
    ]


def test_remove_with_no_overlap_is_noop():
    store = AvailabilityStore()
    store.add_recurring_availability(
        "alice", TUE, time(15, 0), time(19, 0), effective_from=date(2026, 1, 1),
    )
    store.remove_recurring_availability(
        "alice", TUE, time(9, 0), time(10, 0), effective_from=date(2026, 1, 1),
    )
    store.remove_recurring_availability(
        "alice", WED, time(15, 0), time(19, 0), effective_from=date(2026, 1, 1),
    )

    rules = store.rules_for("alice", TUE)
    assert len(rules) == 1
    assert (rules[0].start_time, rules[0].end_time) == (time(15, 0), time(19, 0))


def test_readd_after_remove_restores_availability():
    """The bug that started this redesign: add, remove, re-add must not
    leave the slot permanently masked."""
    store = AvailabilityStore()
    store.add_recurring_availability(
        "alice", TUE, time(14, 0), time(15, 0), effective_from=date(2026, 1, 1),
    )
    store.remove_recurring_availability(
        "alice", TUE, time(14, 0), time(15, 0), effective_from=date(2026, 1, 1),
    )
    store.add_recurring_availability(
        "alice", TUE, time(14, 0), time(15, 0), effective_from=date(2026, 1, 1),
    )

    rules = store.rules_for("alice", TUE)
    assert len(rules) == 1
    assert (rules[0].start_time, rules[0].end_time) == (time(14, 0), time(15, 0))

    calendar = store.get_availability("alice", date(2026, 5, 1), date(2026, 5, 6))
    assert calendar == slot(date(2026, 5, 5), time(14, 0), time(15, 0))


def test_different_weekdays_dont_interact():
    store = AvailabilityStore()
    store.add_recurring_availability(
        "alice", TUE, time(15, 0), time(19, 0), effective_from=date(2026, 1, 1),
    )
    store.remove_recurring_availability(
        "alice", WED, time(15, 0), time(19, 0), effective_from=date(2026, 1, 1),
    )

    assert len(store.rules_for("alice", TUE)) == 1
    assert store.rules_for("alice", WED) == []


def test_get_availability_expands_rule_across_matching_weekdays_in_window():
    store = AvailabilityStore()
    store.add_recurring_availability(
        "alice", TUE, time(15, 0), time(19, 0),
        effective_from=date(2026, 1, 1), effective_until=date(2027, 1, 1),
    )

    calendar = store.get_availability("alice", date(2026, 5, 1), date(2026, 5, 15))

    expected = slot(date(2026, 5, 5), time(15, 0), time(19, 0)) | slot(
        date(2026, 5, 12), time(15, 0), time(19, 0)
    )
    assert calendar == expected


def test_crop_restricts_to_a_sub_window():
    store = AvailabilityStore()
    store.add_recurring_availability(
        "alice", TUE, time(15, 0), time(19, 0), effective_from=date(2026, 1, 1),
    )
    wide = store.get_availability("alice", date(2026, 5, 1), date(2026, 5, 20))

    cropped = crop(wide, date(2026, 5, 1), date(2026, 5, 8))

    assert cropped == slot(date(2026, 5, 5), time(15, 0), time(19, 0))


def test_intersect_finds_overlap_between_two_calendars():
    store = AvailabilityStore()
    store.add_recurring_availability(
        "alice", TUE, time(15, 0), time(19, 0), effective_from=date(2026, 1, 1),
    )
    store.add_recurring_availability(
        "provider", TUE, time(17, 0), time(20, 0), effective_from=date(2026, 1, 1),
    )

    window = (date(2026, 5, 5), date(2026, 5, 6))
    alice_cal = store.get_availability("alice", *window)
    provider_cal = store.get_availability("provider", *window)

    assert intersect(alice_cal, provider_cal) == slot(date(2026, 5, 5), time(17, 0), time(19, 0))


def test_negate_gives_the_gaps_within_the_window():
    store = AvailabilityStore()
    store.add_recurring_availability(
        "alice", TUE, time(15, 0), time(19, 0), effective_from=date(2026, 1, 1),
    )
    window = (date(2026, 5, 5), date(2026, 5, 6))
    calendar = store.get_availability("alice", *window)

    gaps = negate(calendar, *window)
    full_window = P.closedopen(dt(date(2026, 5, 5), time.min), dt(date(2026, 5, 6), time.min))

    assert intersect(gaps, calendar).empty
    assert union(gaps, calendar) == full_window
    assert dt(date(2026, 5, 5), time(10, 0)) in gaps
    assert dt(date(2026, 5, 5), time(20, 0)) in gaps
    assert dt(date(2026, 5, 5), time(17, 0)) not in gaps
