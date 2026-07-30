from datetime import date, datetime, time

from calendar_store import AvailabilityStore, TimeSegment, intersect, to_segments

TUE = 1


def dt(d: date, t: time) -> datetime:
    return datetime.combine(d, t)


def test_get_availability_segments_flattens_to_sorted_time_segments():
    store = AvailabilityStore()
    store.add_recurring_availability(
        "alice", TUE, time(15, 0), time(19, 0),
        effective_from=date(2026, 1, 1), effective_until=date(2027, 1, 1),
    )

    segments = store.get_availability_segments("alice", date(2026, 5, 1), date(2026, 5, 15))

    assert segments == [
        TimeSegment(dt(date(2026, 5, 5), time(15, 0)), dt(date(2026, 5, 5), time(19, 0))),
        TimeSegment(dt(date(2026, 5, 12), time(15, 0)), dt(date(2026, 5, 12), time(19, 0))),
    ]


def test_get_availability_segments_empty_when_nothing_available():
    store = AvailabilityStore()
    assert store.get_availability_segments("alice", date(2026, 5, 1), date(2026, 5, 15)) == []


def test_to_segments_reflects_calendar_algebra():
    store = AvailabilityStore()
    store.add_recurring_availability(
        "alice", TUE, time(15, 0), time(19, 0), effective_from=date(2026, 1, 1),
    )
    store.add_recurring_availability(
        "provider", TUE, time(17, 0), time(20, 0), effective_from=date(2026, 1, 1),
    )
    window = (date(2026, 5, 5), date(2026, 5, 6))
    overlap = intersect(
        store.get_availability("alice", *window), store.get_availability("provider", *window)
    )

    assert to_segments(overlap) == [
        TimeSegment(dt(date(2026, 5, 5), time(17, 0)), dt(date(2026, 5, 5), time(19, 0))),
    ]
