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
    Only `BOOKED` rows occupy time; the rest are the record of what happened.
    """
    BOOKED = "booked"
    CANCELLED = "cancelled"
    SUPERSEDED = "superseded"   # rescheduled; `supersedes` on the newer row points back


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
    notes: Optional[str] = None  # free-text for end users; never read by any solver logic
    status: AppointmentStatus = AppointmentStatus.BOOKED
    origin: Origin = Origin.CLIENT
    supersedes: Optional[int] = None  # the appointment this one replaces, if any

    @property
    def is_live(self) -> bool:
        """Whether this row actually occupies time on the calendar."""
        return self.status is AppointmentStatus.BOOKED
