"""What the policy answers, and what the gate returns.

Three outcomes, not two. `NeedsConfirmation` is what makes an assistant useful
without making it dangerous: it may compose a command and explain it, but a
human turns it into an act.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Union


@dataclass(frozen=True)
class Allow:
    pass


@dataclass(frozen=True)
class Deny:
    """`reason` is for the audit log and the developer, never for the caller.

    Telling a client that appointment 41 exists but is not theirs confirms
    another client's booking, so the gate reports a not-found for an ownership
    failure and keeps the real reason here.
    """
    reason: str
    # True when the target exists but belongs to someone else, i.e. the caller
    # must be told "no such thing" rather than "not allowed".
    disguise_as_missing: bool = False


@dataclass(frozen=True)
class NeedsConfirmation:
    """Permitted, but a human must confirm before it takes effect.

    `summary` is what that human will be shown. It is produced from the command
    rather than by whatever asked for it, so a model cannot describe one action
    and perform another.
    """
    summary: str


Decision = Union[Allow, Deny, NeedsConfirmation]

ALLOW = Allow()
