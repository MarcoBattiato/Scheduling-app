"""The public boundary type. See SPEC.md ("Boundary with scheduling_engine") for
why this stays a plain, logic-free data shape rather than exposing
`portion.Interval` (or the store itself) outside this package.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

import portion as P


@dataclass(frozen=True)
class TimeSegment:
    start: datetime
    end: datetime


def to_segments(calendar: P.Interval) -> list[TimeSegment]:
    """Flatten a portion.Interval into a sorted list of concrete (start, end)
    segments — the only availability representation consumers outside this
    package ever see.
    """
    return [TimeSegment(start=atomic.lower, end=atomic.upper) for atomic in calendar]
