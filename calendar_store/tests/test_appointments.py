from datetime import datetime

import pytest

from calendar_store import (
    AppointmentStatus,
    AvailabilityStore,
    Origin,
    Party,
    TimeSegment,
)


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


def test_cancel_appointment_frees_the_time_but_keeps_the_record():
    store = AvailabilityStore()
    appointment = store.book_appointment("alice", "haircut", dt(2026, 5, 5, 15, 0), dt(2026, 5, 5, 16, 0))

    cancelled = store.cancel_appointment(appointment.id, by=Party.CLIENT)

    assert cancelled.status is AppointmentStatus.CANCELLED_BY_CLIENT
    assert cancelled.is_cancelled
    assert not cancelled.occupies_slot
    assert cancelled.id == appointment.id
    # The slot is free again...
    assert store.appointments_for("alice", dt(2026, 5, 5), dt(2026, 5, 6)) == []
    # ...but that the client once held it is not forgotten.
    assert store.appointment_history("alice", dt(2026, 5, 5), dt(2026, 5, 6)) == [cancelled]


def test_cancel_unknown_appointment_raises():
    store = AvailabilityStore()
    with pytest.raises(KeyError):
        store.cancel_appointment(999, by=Party.CLIENT)


def test_reschedule_appointment_writes_a_new_row_and_retires_the_old_one():
    store = AvailabilityStore()
    appointment = store.book_appointment(
        "alice", "haircut", dt(2026, 5, 5, 15, 0), dt(2026, 5, 5, 16, 0), notes="prefers quiet chat",
    )

    rescheduled = store.reschedule_appointment(appointment.id, dt(2026, 5, 12, 9, 0), dt(2026, 5, 12, 10, 0))

    assert rescheduled.id != appointment.id, "a move is a new row, not an edit"
    assert rescheduled.supersedes == appointment.id
    assert rescheduled.range == TimeSegment(dt(2026, 5, 12, 9, 0), dt(2026, 5, 12, 10, 0))
    assert rescheduled.notes == "prefers quiet chat"

    assert store.appointments_for("alice", dt(2026, 5, 5), dt(2026, 5, 6)) == []
    assert store.appointments_for("alice", dt(2026, 5, 12), dt(2026, 5, 13)) == [rescheduled]

    # Where it used to sit is still on record — that is the whole point.
    (was,) = store.appointment_history("alice", dt(2026, 5, 5), dt(2026, 5, 6))
    assert was.status is AppointmentStatus.SUPERSEDED
    assert was.range == TimeSegment(dt(2026, 5, 5, 15, 0), dt(2026, 5, 5, 16, 0))


def test_reschedule_records_who_wanted_the_move():
    """A slot the client asked for is evidence of preference; one they were
    moved to is evidence of disruption. Anything learning from history has to
    be able to tell them apart.
    """
    store = AvailabilityStore()
    first = store.book_appointment("alice", "haircut", dt(2026, 5, 5, 15, 0), dt(2026, 5, 5, 16, 0))
    assert first.origin is Origin.CLIENT

    chosen = store.reschedule_appointment(first.id, dt(2026, 5, 6, 15, 0), dt(2026, 5, 6, 16, 0))
    assert chosen.origin is Origin.CLIENT

    forced = store.reschedule_appointment(
        chosen.id, dt(2026, 5, 7, 9, 0), dt(2026, 5, 7, 10, 0), origin=Origin.DISPLACED,
    )
    assert forced.origin is Origin.DISPLACED


def test_a_chain_of_moves_stays_traceable():
    store = AvailabilityStore()
    first = store.book_appointment("alice", "haircut", dt(2026, 5, 5, 15, 0), dt(2026, 5, 5, 16, 0))
    second = store.reschedule_appointment(first.id, dt(2026, 5, 6, 15, 0), dt(2026, 5, 6, 16, 0))
    third = store.reschedule_appointment(second.id, dt(2026, 5, 7, 15, 0), dt(2026, 5, 7, 16, 0))

    assert third.supersedes == second.id
    assert second.supersedes == first.id

    history = store.appointment_history("alice", dt(2026, 5, 1), dt(2026, 5, 30))
    assert [a.status for a in sorted(history, key=lambda a: a.id)] == [
        AppointmentStatus.SUPERSEDED,
        AppointmentStatus.SUPERSEDED,
        AppointmentStatus.BOOKED,
    ]


def test_cancelling_a_rescheduled_appointment_leaves_the_whole_trail():
    store = AvailabilityStore()
    first = store.book_appointment("alice", "haircut", dt(2026, 5, 5, 15, 0), dt(2026, 5, 5, 16, 0))
    moved = store.reschedule_appointment(first.id, dt(2026, 5, 6, 15, 0), dt(2026, 5, 6, 16, 0))
    store.cancel_appointment(moved.id, by=Party.CLIENT)

    assert store.appointments_for("alice", dt(2026, 5, 1), dt(2026, 5, 30)) == []
    assert len(store.appointment_history("alice", dt(2026, 5, 1), dt(2026, 5, 30))) == 2


def test_history_is_per_client_and_windowed_like_the_live_query():
    store = AvailabilityStore()
    store.cancel_appointment(
        store.book_appointment("alice", "haircut", dt(2026, 5, 5, 15, 0), dt(2026, 5, 5, 16, 0)).id,
        by=Party.CLIENT,
    )
    store.book_appointment("bob", "haircut", dt(2026, 5, 5, 15, 0), dt(2026, 5, 5, 16, 0))

    assert len(store.appointment_history("alice", dt(2026, 5, 5), dt(2026, 5, 6))) == 1
    assert store.appointment_history("alice", dt(2026, 5, 6), dt(2026, 5, 7)) == []
    assert len(store.appointment_history("bob", dt(2026, 5, 5), dt(2026, 5, 6))) == 1


def test_reschedule_unknown_appointment_raises():
    store = AvailabilityStore()
    with pytest.raises(KeyError):
        store.reschedule_appointment(999, dt(2026, 5, 12, 9, 0), dt(2026, 5, 12, 10, 0))


def test_attendance_is_recorded_by_the_provider():
    store = AvailabilityStore()
    kept = store.book_appointment("alice", "haircut", dt(2026, 5, 5, 15, 0), dt(2026, 5, 5, 16, 0))
    missed = store.book_appointment("bob", "haircut", dt(2026, 5, 5, 16, 0), dt(2026, 5, 5, 17, 0))

    assert store.mark_attendance(kept.id, attended=True).status is AppointmentStatus.COMPLETED
    assert store.mark_attendance(missed.id, attended=False).status is AppointmentStatus.NO_SHOW


def test_an_attended_or_missed_appointment_still_held_its_slot():
    """Only cancelling or moving gives the time back. Someone who failed to
    turn up occupied that hour as surely as someone who did.
    """
    store = AvailabilityStore()
    appointment = store.book_appointment(
        "alice", "haircut", dt(2026, 5, 5, 15, 0), dt(2026, 5, 5, 16, 0)
    )
    store.mark_attendance(appointment.id, attended=False)

    assert store.appointments_for("alice", dt(2026, 5, 5), dt(2026, 5, 6))


def test_attendance_cannot_be_recorded_for_a_cancelled_appointment():
    store = AvailabilityStore()
    appointment = store.book_appointment(
        "alice", "haircut", dt(2026, 5, 5, 15, 0), dt(2026, 5, 5, 16, 0)
    )
    store.cancel_appointment(appointment.id, by=Party.CLIENT)

    with pytest.raises(ValueError, match="cancelled_by_client"):
        store.mark_attendance(appointment.id, attended=True)


def test_who_cancelled_is_part_of_the_record():
    store = AvailabilityStore()
    theirs = store.book_appointment("alice", "haircut", dt(2026, 5, 5, 15, 0), dt(2026, 5, 5, 16, 0))
    ours = store.book_appointment("bob", "haircut", dt(2026, 5, 5, 16, 0), dt(2026, 5, 5, 17, 0))

    assert (store.cancel_appointment(theirs.id, by=Party.CLIENT).status
            is AppointmentStatus.CANCELLED_BY_CLIENT)
    assert (store.cancel_appointment(ours.id, by=Party.PROVIDER).status
            is AppointmentStatus.CANCELLED_BY_PROVIDER)


def test_cancelling_will_not_let_you_forget_whose_cancellation_it_was():
    store = AvailabilityStore()
    appointment = store.book_appointment(
        "alice", "haircut", dt(2026, 5, 5, 15, 0), dt(2026, 5, 5, 16, 0)
    )

    with pytest.raises(TypeError):
        store.cancel_appointment(appointment.id)


def test_a_booking_remembers_the_slot_its_client_asked_for():
    """A wish, not a constraint, and not necessarily where it ended up — kept
    so a later move is judged against what they wanted rather than against
    wherever they were last put.
    """
    store = AvailabilityStore()
    wanted = dt(2026, 5, 5, 15, 0)

    appointment = store.book_appointment(
        "alice", "haircut", dt(2026, 5, 5, 16, 0), dt(2026, 5, 5, 17, 0),
        preferred_start=wanted,
    )

    assert appointment.preferred_start == wanted
    assert appointment.range.start != wanted, "the wish is not where it landed"


def test_the_wish_survives_a_rescheduling():
    store = AvailabilityStore()
    wanted = dt(2026, 5, 5, 15, 0)
    first = store.book_appointment(
        "alice", "haircut", dt(2026, 5, 5, 15, 0), dt(2026, 5, 5, 16, 0),
        preferred_start=wanted,
    )

    moved = store.reschedule_appointment(
        first.id, dt(2026, 5, 6, 9, 0), dt(2026, 5, 6, 10, 0), origin=Origin.DISPLACED,
    )

    assert moved.preferred_start == wanted, (
        "being displaced must not redefine what the client wanted"
    )


def test_a_booking_without_a_stated_wish_simply_has_none():
    store = AvailabilityStore()
    appointment = store.book_appointment(
        "alice", "haircut", dt(2026, 5, 5, 15, 0), dt(2026, 5, 5, 16, 0))

    assert appointment.preferred_start is None
