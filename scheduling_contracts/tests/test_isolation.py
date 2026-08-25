"""This package must never acquire a dependency.

It is the fixed point the others conform to, so anything imported here is
inherited by all of them. The check is deliberately a whitelist of the few
standard-library modules the contract legitimately needs, rather than "is it on
PyPI" — the failure being guarded against is a well-meaning import of
`calendar_store` or `pydantic`, and both would sail past a looser rule.
"""
from __future__ import annotations

import ast
import pathlib

import scheduling_contracts

PERMITTED = {"__future__", "dataclasses", "datetime", "enum", "typing"}

ROOT = pathlib.Path(scheduling_contracts.__file__).parent


def imported_roots():
    found = {}
    for path in sorted(ROOT.glob("*.py")):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    found.setdefault(alias.name.split(".")[0], path.name)
            elif isinstance(node, ast.ImportFrom):
                if node.level:        # relative, i.e. our own modules
                    continue
                found.setdefault((node.module or "").split(".")[0], path.name)
    return found


def test_only_the_standard_library_is_imported():
    offenders = {m: f for m, f in imported_roots().items() if m not in PERMITTED}
    assert not offenders, f"contracts grew a dependency: {offenders}"


def test_pyproject_declares_no_runtime_dependencies():
    text = (ROOT.parents[1] / "pyproject.toml").read_text()
    assert "dependencies = []" in text


def test_it_imports_with_nothing_else_installed():
    """Sanity: importing the package pulls in no sibling package."""
    import sys
    for forbidden in ("calendar_store", "scheduling_engine", "mock_ui", "ortools"):
        assert forbidden not in sys.modules, f"{forbidden} was imported"
