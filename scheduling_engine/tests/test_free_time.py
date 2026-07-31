from datetime import date, datetime, time

from calendar_store import AvailabilityStore, TimeSegment

from scheduling_engine import TimeRange, free_time

DAY = date(2026, 5, 5)


def at(hour, minute=0):
    return datetime.combine(DAY, time(hour, minute))


def seg(from_hour, to_hour):
    return TimeSegment(at(from_hour), at(to_hour))


def test_availability_is_unchanged_when_nothing_is_booked():
    assert free_time([seg(9, 17)]) == [seg(9, 17)]


def test_a_booking_in_the_middle_splits_the_segment():
    assert free_time([seg(9, 17)], [TimeRange(at(12), at(13))]) == [seg(9, 12), seg(13, 17)]


def test_bookings_at_the_edges_trim_rather_than_split():
    assert free_time([seg(9, 17)], [TimeRange(at(9), at(10))]) == [seg(10, 17)]
    assert free_time([seg(9, 17)], [TimeRange(at(16), at(17))]) == [seg(9, 16)]


def test_a_booking_covering_everything_leaves_nothing():
    assert free_time([seg(9, 17)], [TimeRange(at(8), at(18))]) == []


def test_bookings_outside_the_availability_are_ignored():
    assert free_time([seg(9, 12)], [TimeRange(at(14), at(15))]) == [seg(9, 12)]


def test_overlapping_and_unsorted_bookings_are_handled():
    # Deliberately out of order and overlapping — the caller should not have to
    # normalise before calling.
    booked = [
        TimeRange(at(14), at(15)),
        TimeRange(at(10), at(12)),
        TimeRange(at(11), at(13)),
    ]
    assert free_time([seg(9, 17)], booked) == [seg(9, 10), seg(13, 14), seg(15, 17)]


def test_back_to_back_bookings_do_not_leave_a_zero_length_sliver():
    booked = [TimeRange(at(10), at(11)), TimeRange(at(11), at(12))]
    assert free_time([seg(9, 13)], booked) == [seg(9, 10), seg(12, 13)]


def test_output_is_sorted_even_when_input_is_not():
    result = free_time([seg(14, 17), seg(9, 12)])
    assert result == [seg(9, 12), seg(14, 17)]


def test_accepts_calendar_store_appointments_directly():
    """Appointments carry their span on `.range`, unlike a bare segment."""
    store = AvailabilityStore()
    appointment = store.book_appointment("dana", "review", at(12), at(13))

    assert free_time([seg(9, 17)], [appointment]) == [seg(9, 12), seg(13, 17)]


def test_end_to_end_against_calendar_store():
    """The seam SPEC.md §3 describes: availability from the store, bookings
    subtracted by the caller, because the store never nets them itself.
    """
    store = AvailabilityStore()
    store.add_exception_availability("provider-self", DAY, time(9), time(17))
    booked = [store.book_appointment("dana", "review", at(11), at(12))]

    availability = store.get_availability_segments("provider-self", DAY, date(2026, 5, 6))
    assert availability == [seg(9, 17)]
    assert free_time(availability, booked) == [seg(9, 11), seg(12, 17)]
