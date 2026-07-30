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
    """
    id: int
    client_id: str  # "provider-self" is a valid pseudo-client for e.g. lunch breaks
    service_type_id: str
    range: TimeSegment
    locked: bool  # staff-pinned, immovable regardless of notice
    notes: Optional[str] = None  # free-text for end users; never read by any solver logic
