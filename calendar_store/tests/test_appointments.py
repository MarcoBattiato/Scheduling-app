from datetime import datetime

import pytest

from calendar_store import AvailabilityStore, TimeSegment


def dt(*args) -> datetime:
    return datetime(*args)


def test_book_appointment_creates_and_returns_record():
    store = AvailabilityStore()
    appointment = store.book_appointment(
        "alice", "haircut", dt(2026, 5, 5, 15, 0), dt(2026, 5, 5, 16, 0), notes="prefers quiet chat",
    )

    assert appointment.client_id == "alice"
    assert appointment.service_type_id == "haircut"
    assert appointment.range == TimeSegment(dt(2026, 5, 5, 15, 0), dt(2026, 5, 5, 16, 0))
    assert appointment.locked is False
    assert appointment.notes == "prefers quiet chat"
    assert store.appointments_for("alice", dt(2026, 5, 5), dt(2026, 5, 6)) == [appointment]


def test_book_appointment_defaults_locked_false_and_notes_none():
    store = AvailabilityStore()
    appointment = store.book_appointment("alice", "haircut", dt(2026, 5, 5, 15, 0), dt(2026, 5, 5, 16, 0))

    assert appointment.locked is False
    assert appointment.notes is None


def test_appointments_for_filters_by_client():
    store = AvailabilityStore()
    store.book_appointment("alice", "haircut", dt(2026, 5, 5, 15, 0), dt(2026, 5, 5, 16, 0))
    store.book_appointment("bob", "haircut", dt(2026, 5, 5, 15, 0), dt(2026, 5, 5, 16, 0))

    results = store.appointments_for("alice", dt(2026, 5, 5), dt(2026, 5, 6))

    assert len(results) == 1
    assert results[0].client_id == "alice"


def test_appointments_for_filters_by_window_overlap():
    store = AvailabilityStore()
    inside = store.book_appointment("alice", "haircut", dt(2026, 5, 5, 15, 0), dt(2026, 5, 5, 16, 0))
    store.book_appointment("alice", "haircut", dt(2026, 5, 10, 15, 0), dt(2026, 5, 10, 16, 0))

    results = store.appointments_for("alice", dt(2026, 5, 5), dt(2026, 5, 6))

    assert results == [inside]


def test_appointments_for_includes_appointments_that_only_partially_overlap_the_window():
    store = AvailabilityStore()
    spanning = store.book_appointment(
        "alice", "haircut", dt(2026, 5, 5, 23, 0), dt(2026, 5, 6, 1, 0),
    )

    results = store.appointments_for("alice", dt(2026, 5, 5), dt(2026, 5, 6))

    assert results == [spanning]


def test_cancel_appointment_removes_it():
    store = AvailabilityStore()
    appointment = store.book_appointment("alice", "haircut", dt(2026, 5, 5, 15, 0), dt(2026, 5, 5, 16, 0))

    cancelled = store.cancel_appointment(appointment.id)

    assert cancelled == appointment
    assert store.appointments_for("alice", dt(2026, 5, 5), dt(2026, 5, 6)) == []


def test_cancel_unknown_appointment_raises():
    store = AvailabilityStore()
    with pytest.raises(KeyError):
        store.cancel_appointment(999)


def test_reschedule_appointment_updates_range_and_preserves_the_rest():
    store = AvailabilityStore()
    appointment = store.book_appointment(
        "alice", "haircut", dt(2026, 5, 5, 15, 0), dt(2026, 5, 5, 16, 0), notes="prefers quiet chat",
    )

    rescheduled = store.reschedule_appointment(appointment.id, dt(2026, 5, 12, 9, 0), dt(2026, 5, 12, 10, 0))

    assert rescheduled.id == appointment.id
    assert rescheduled.range == TimeSegment(dt(2026, 5, 12, 9, 0), dt(2026, 5, 12, 10, 0))
    assert rescheduled.notes == "prefers quiet chat"
    assert store.appointments_for("alice", dt(2026, 5, 5), dt(2026, 5, 6)) == []
    assert store.appointments_for("alice", dt(2026, 5, 12), dt(2026, 5, 13)) == [rescheduled]


def test_reschedule_unknown_appointment_raises():
    store = AvailabilityStore()
    with pytest.raises(KeyError):
        store.reschedule_appointment(999, dt(2026, 5, 12, 9, 0), dt(2026, 5, 12, 10, 0))
