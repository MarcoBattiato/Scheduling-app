"""When the scheduler is allowed to run.

Kept apart from `World` and expressed as a pure function of (policy, clock,
queue) so it can be tested without waiting for real time to pass, and so the
question "why did it run?" always has an answer.

This is usage policy, not algorithm: the engine itself is indifferent to when
it is called. Restricting *when* and over *what window* is how a provider gets
"solve the whole of next week on Monday morning, and otherwise leave it alone".
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from typing import List, Optional, Sequence, Tuple


@dataclass
class SchedulingPolicy:
    """A provider's rules for firing the scheduler.

    Deliberately never fires on submission. A request arriving is not a reason
    to re-plan the week — it is a reason to consider re-planning at the next
    point the provider has said they want to look.
    """
    auto_run: bool = True
    # (weekday, time-of-day) — Monday 08:00 by default, i.e. plan the week ahead.
    weekly_runs: List[Tuple[int, time]] = field(
        default_factory=lambda: [(0, time(8, 0))]
    )
    # A request whose window opens sooner than this cannot wait for the weekly
    # run, so it earns one of its own.
    urgency_hours: int = 24
    # How long before unsatisfied work is retried. Without this, a queue that
    # cannot be solved would either spin or sit forever.
    retry_after_minutes: int = 60


@dataclass(frozen=True)
class Trigger:
    reason: str
    detail: str = ""


def previous_occurrence(now: datetime, weekday: int, at: time) -> datetime:
    """The most recent moment matching (weekday, time) at or before `now`."""
    days_back = (now.weekday() - weekday) % 7
    candidate = datetime.combine(now.date() - timedelta(days=days_back), at)
    if candidate > now:
        candidate -= timedelta(days=7)
    return candidate


def due(
    policy: SchedulingPolicy,
    now: datetime,
    last_run: Optional[datetime],
    waiting_window_starts: Sequence[datetime],
    has_unsatisfied: bool,
) -> Optional[Trigger]:
    """Why the scheduler should run now, or None.

    `waiting_window_starts` are the earliest wanted times of requests still
    eligible to trigger — requests already tried and parked (ON_HOLD) are not
    among them, which is what stops an unplaceable request whose date is
    approaching from demanding a run on every tick, forever.
    """
    if not policy.auto_run:
        return None

    for weekday, at in policy.weekly_runs:
        occurrence = previous_occurrence(now, weekday, at)
        if last_run is None or last_run < occurrence:
            return Trigger("weekly", f"scheduled run for {occurrence:%a %d %b %H:%M}")

    horizon = now + timedelta(hours=policy.urgency_hours)
    imminent = [w for w in waiting_window_starts if w <= horizon]
    if imminent:
        return Trigger(
            "urgent",
            f"{len(imminent)} request(s) wanted within {policy.urgency_hours}h",
        )

    if has_unsatisfied and last_run is not None:
        if now - last_run >= timedelta(minutes=policy.retry_after_minutes):
            return Trigger(
                "retry",
                f"unsatisfied work, {policy.retry_after_minutes} min since the last run",
            )

    return None
