"""Building one of every command, so the suites below can be exhaustive.

Exhaustive matters here: a permission table is only trustworthy if the tests
cover every entry, and a hand-written list of examples would drift the moment
somebody adds a command.
"""
from __future__ import annotations

import dataclasses
from datetime import date, datetime, time, timedelta

import pytest

from scheduling_contracts import BudgetBounds, TimeSpan, all_commands

NOW = datetime(2026, 9, 1, 9, 0)
SPAN = TimeSpan(NOW, NOW + timedelta(hours=1))


def _dummy(annotation: str):
    """A plausible value for a field, chosen from its annotation text.

    Annotations are strings here (`from __future__ import annotations`), which
    for once is convenient: no imports need resolving to inspect them.
    """
    a = annotation
    if "BudgetBounds" in a:
        return BudgetBounds()
    if "TimeSpan" in a:
        return SPAN
    if "Sequence" in a or "Tuple" in a:
        return ()
    if "datetime" in a:
        return NOW
    if "date" in a:
        return date(2026, 9, 1)
    if "time" in a:
        return time(9, 0)
    if "bool" in a:
        return True
    if "float" in a:
        return 0.5
    if "int" in a:
        return 1
    return "alice"


def build(cls):
    """An instance of `cls` with every mandatory field filled."""
    kwargs = {}
    for f in dataclasses.fields(cls):
        has_default = (
            f.default is not dataclasses.MISSING
            or f.default_factory is not dataclasses.MISSING  # type: ignore[misc]
        )
        if not has_default:
            kwargs[f.name] = _dummy(str(f.type))
    return cls(**kwargs)


@pytest.fixture(scope="session")
def every_command():
    return tuple(build(cls) for cls in all_commands())
