"""Availability rows, plus booked appointments. See SPEC.md for the rationale:
rules always store positive, normalized availability (add/remove are store
operations, not row properties); exceptions carry `kind` since they can't
resolve away into rules; appointments are a separate, non-normalized concern.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, time
from enum import Enum
from typing import Optional

from .segments import TimeSegment


class Kind(str, Enum):
    ADD = "add"
    REMOVE = "remove"


class AppointmentStatus(str, Enum):
    """Appointments are never deleted, so that history survives.

    A booking that was cancelled, or moved elsewhere, is evidence about a
    client's habits — and evidence you cannot recover once the row is gone.

    Cancellation carries *who* cancelled in the status itself rather than in a
    separate field, so a cancellation cannot be recorded without saying whose
    it was. A client dropping their slot says something about that slot; the
    provider closing the day says nothing about the client at all.
    """
    BOOKED = "booked"
    COMPLETED = "completed"                     # the client attended
    NO_SHOW = "no_show"                         # the slot was held and wasted
    CANCELLED_BY_CLIENT = "cancelled_by_client"
    CANCELLED_BY_PROVIDER = "cancelled_by_provider"
    SUPERSEDED = "superseded"   # rescheduled; `supersedes` on the newer row points back


class Party(str, Enum):
    """Who acted. Used when cancelling; `Origin` covers who chose a slot."""
    CLIENT = "client"
    PROVIDER = "provider"


_CANCELLED = {
    AppointmentStatus.CANCELLED_BY_CLIENT,
    AppointmentStatus.CANCELLED_BY_PROVIDER,
}
# An appointment that happened, or is still going to, held its slot. Only
# cancelling or moving it gives the time back.
_HELD = {
    AppointmentStatus.BOOKED,
    AppointmentStatus.COMPLETED,
    AppointmentStatus.NO_SHOW,
}


class Origin(str, Enum):
    """Who put the appointment where it is.

    The distinction matters for anything that learns from history: a slot the
    client asked for is evidence of preference, whereas one they were moved to
    is evidence of disruption. Recording both alike would let the scheduler
    launder its own rescheduling into a client's apparent habits.
    """
    CLIENT = "client"          # the client's own choice of slot
    DISPLACED = "displaced"    # moved by the scheduler to make room for someone else


@dataclass(frozen=True)
class ClientAvailabilityRule:
    id: int
    client_id: str
    weekday: int  # Monday=0 .. Sunday=6, matches date.weekday()
    start_time: time
    end_time: time
    effective_from: date
    effective_until: Optional[date]  # None = indefinitely active


@dataclass(frozen=True)
class AvailabilityException:
    id: int
    client_id: str
    date: date
    start_time: time
    end_time: time
    kind: Kind


@dataclass(frozen=True)
class Appointment:
    """A concrete booked slot — distinct from rules/exceptions, which say when
    a client *could* be booked at all. Not part of the availability algebra:
    "no double-booking" and "availability" are separate hard constraints (see
    SPEC.md), so this never feeds into `get_availability`.

    Discrete and individually identified, unlike rules/exceptions: two
    back-to-back appointments are never merged into one, even though they're
    contiguous — they're separate bookings, not a span of availability.

    Rows accumulate rather than change: cancelling sets `status`, and
    rescheduling writes a new row pointing back at the old one through
    `supersedes`. So a client's history is the chain, not the surviving row.
    """
    id: int
    client_id: str  # "provider-self" is a valid pseudo-client for e.g. lunch breaks
    service_type_id: str
    range: TimeSegment
    locked: bool  # staff-pinned, immovable regardless of notice
    # Booking-process notes only — "prefers the quiet room", "parking code 4821".
    # Clinical or otherwise sensitive content is deliberately out of scope for
    # this system; letting it in here would make a scheduling app a holder of
    # medical records, with everything that implies. Never read by solver logic.
    notes: Optional[str] = None
    status: AppointmentStatus = AppointmentStatus.BOOKED
    origin: Origin = Origin.CLIENT
    supersedes: Optional[int] = None  # the appointment this one replaces, if any

    @property
    def occupies_slot(self) -> bool:
        """Whether this row holds its time on the calendar.

        A completed appointment, and even one the client failed to turn up to,
        held that slot as surely as an upcoming booking does — only cancelling
        or rescheduling gives the time back.
        """
        return self.status in _HELD

    @property
    def is_cancelled(self) -> bool:
        return self.status in _CANCELLED
