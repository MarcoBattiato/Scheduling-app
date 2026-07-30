"""Availability rows. See DESIGN_NOTES.md for the rationale: rules always store
positive, normalized availability (add/remove are store operations, not row
properties) — only exceptions still carry `kind`, pending the same treatment.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, time
from enum import Enum
from typing import Optional


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
