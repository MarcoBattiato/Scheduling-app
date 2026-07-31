"""Run a scenario from a JSON file and look at the result in a browser.

    python -m scheduling_engine.playground examples/scenario.json --open
    python -m scheduling_engine.playground my.json --alpha 0,0.5,1

Everything goes through the real pipeline — calendar_store holds the
availability, bookings are subtracted from it, and the solver sees only what a
repository adapter would hand it — so what you look at is what the engine
actually did, not a mock-up of it.

See `examples/scenario.json` for the format; `SCENARIO_HELP` below documents
every field.
"""
from __future__ import annotations

import argparse
import json
import sys
import webbrowser
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Sequence

from calendar_store import Appointment, AvailabilityStore, TimeSegment

from . import report
from .availability import free_time
from .models import BookingRequest, CostConfig, TimeRange
from .placement import solve_placements
from .visualize import render

PROVIDER = "provider-self"

SCENARIO_HELP = """\
{
  "title":    "free text",
  "alpha":    0.5,                     // 0 = earliest-first, 1 = packing-first
  "grid_minutes": 30,                  // optional
  "service_durations": [60, 90],       // optional
  "window": {"from": "2026-05-04", "to": "2026-05-09"},   // needed by "recurring"

  "provider": {
    "availability": [{"date": "2026-05-04", "from": "09:00", "to": "17:00"}],
    "recurring":    [{"weekday": "tue", "from": "09:00", "to": "13:00"}]
  },

  "booked": [{"date": "2026-05-04", "from": "11:00", "to": "12:00",
              "client": "dana", "service": "consult"}],

  "clients": {
    "alice": {"availability": [{"date": "2026-05-04", "from": "10:00", "to": "14:00"}]}
  },

  "requests": [
    {"id": "r1", "client": "alice", "duration": 60,
     "windows": [{"date": "2026-05-04", "from": "09:00", "to": "17:00"}]}
  ]
}

A span is either {"date","from","to"} with clock times, or {"from","to"} with
full "YYYY-MM-DD HH:MM" stamps — use the latter for windows spanning days.
Client availability is displayed but NOT enforced: this pass honours only what
each request itself asked for.
"""

_WEEKDAYS = {
    "mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6,
}


@dataclass
class Scenario:
    title: str
    config: CostConfig
    provider_availability: List[TimeSegment] = field(default_factory=list)
    provider_free: List[TimeSegment] = field(default_factory=list)
    booked: List[Appointment] = field(default_factory=list)
    client_availability: Dict[str, List[TimeSegment]] = field(default_factory=dict)
    requests: List[BookingRequest] = field(default_factory=list)


class ScenarioError(ValueError):
    """A problem with the scenario file, phrased for whoever wrote it."""


# --------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------


def _stamp(text: str, where: str) -> datetime:
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    raise ScenarioError(f"{where}: expected 'YYYY-MM-DD HH:MM', got {text!r}")


def _span(entry: dict, where: str):
    """A span is either date + clock times, or two full stamps."""
    try:
        if "date" in entry:
            day = date.fromisoformat(entry["date"])
            start = datetime.combine(day, time.fromisoformat(entry["from"]))
            end = datetime.combine(day, time.fromisoformat(entry["to"]))
        else:
            start = _stamp(entry["from"], where)
            end = _stamp(entry["to"], where)
    except KeyError as exc:
        raise ScenarioError(f"{where}: missing {exc.args[0]!r}") from None
    except ValueError as exc:
        raise ScenarioError(f"{where}: {exc}") from None
    if end <= start:
        raise ScenarioError(f"{where}: ends at or before it starts ({start} → {end})")
    return start, end


def _window(raw: dict, spans: Sequence) -> tuple:
    """The date range to materialise recurring rules over."""
    if "window" in raw:
        try:
            start = date.fromisoformat(raw["window"]["from"])
            end = date.fromisoformat(raw["window"]["to"])
        except (KeyError, ValueError) as exc:
            raise ScenarioError(f"window: {exc}") from None
        return start, end + timedelta(days=1)
    if not spans:
        raise ScenarioError(
            "no dates anywhere in the scenario — add explicit availability, or a "
            '"window" if you are using "recurring"'
        )
    return (
        min(s for s, _ in spans).date(),
        max(e for _, e in spans).date() + timedelta(days=1),
    )


def _add_availability(store: AvailabilityStore, client: str, block: dict, where: str):
    for entry in block.get("availability", ()):
        start, end = _span(entry, f"{where}.availability")
        if start.date() != end.date():
            raise ScenarioError(
                f"{where}.availability: a segment may not span midnight ({start} → {end})"
            )
        store.add_exception_availability(client, start.date(), start.time(), end.time())
    for entry in block.get("recurring", ()):
        try:
            weekday = _WEEKDAYS[str(entry["weekday"]).lower()[:3]]
        except KeyError:
            raise ScenarioError(
                f"{where}.recurring: weekday must be one of {sorted(_WEEKDAYS)}"
            ) from None
        store.add_recurring_availability(
            client, weekday,
            time.fromisoformat(entry["from"]), time.fromisoformat(entry["to"]),
            effective_from=date.fromisoformat(
                entry.get("from_date", "1970-01-01")
            ),
            effective_until=(
                date.fromisoformat(entry["until"]) if "until" in entry else None
            ),
        )


def parse(raw: dict) -> Scenario:
    config = CostConfig(
        alpha=float(raw.get("alpha", 0.5)),
        grid_minutes=int(raw.get("grid_minutes", CostConfig().grid_minutes)),
        service_durations=tuple(
            raw.get("service_durations", CostConfig().service_durations)
        ),
    )

    dated = [_span(e, "provider.availability")
             for e in raw.get("provider", {}).get("availability", ())]
    dated += [_span(e, "booked") for e in raw.get("booked", ())]
    for name, block in raw.get("clients", {}).items():
        dated += [_span(e, f"clients.{name}.availability")
                  for e in block.get("availability", ())]
    for entry in raw.get("requests", ()):
        dated += [_span(w, f"requests[{entry.get('id', '?')}].windows")
                  for w in entry.get("windows", ())]
    window_start, window_end = _window(raw, dated)

    store = AvailabilityStore()
    _add_availability(store, PROVIDER, raw.get("provider", {}), "provider")
    for name, block in raw.get("clients", {}).items():
        _add_availability(store, name, block, f"clients.{name}")

    booked = []
    for entry in raw.get("booked", ()):
        start, end = _span(entry, "booked")
        booked.append(store.book_appointment(
            entry.get("client", "unknown"), entry.get("service", "appointment"),
            start, end, locked=bool(entry.get("locked", False)),
        ))

    provider_availability = store.get_availability_segments(
        PROVIDER, window_start, window_end
    )
    if not provider_availability:
        raise ScenarioError(
            "the provider has no availability in the window — check "
            '"provider.availability" / "provider.recurring" and "window"'
        )

    requests = []
    for index, entry in enumerate(raw.get("requests", ())):
        request_id = str(entry.get("id", f"r{index + 1}"))
        windows = [
            TimeRange(*_span(w, f"requests[{request_id}].windows"))
            for w in entry.get("windows", ())
        ]
        if not windows:
            raise ScenarioError(f"requests[{request_id}]: needs at least one window")
        requests.append(BookingRequest(
            id=request_id,
            client_id=str(entry.get("client", "unknown")),
            duration_minutes=int(entry["duration"]),
            desired=windows,
        ))

    return Scenario(
        title=raw.get("title", "Placement scenario"),
        config=config,
        provider_availability=provider_availability,
        provider_free=free_time(provider_availability, booked),
        booked=booked,
        client_availability={
            name: store.get_availability_segments(name, window_start, window_end)
            for name in raw.get("clients", {})
        },
        requests=requests,
    )


def load(path: Path) -> Scenario:
    try:
        raw = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        raise ScenarioError(f"{path}: invalid JSON — {exc}") from None
    return parse(raw)


# --------------------------------------------------------------------------
# Running
# --------------------------------------------------------------------------


def build_page(scenario: Scenario, alphas: Sequence[float]) -> str:
    sections = []
    for alpha in alphas:
        config = CostConfig(
            alpha=alpha,
            grid_minutes=scenario.config.grid_minutes,
            service_durations=scenario.config.service_durations,
        )
        result = solve_placements(scenario.requests, scenario.provider_free, config)
        heading = (
            scenario.title if len(alphas) == 1
            else f"{scenario.title} — alpha {alpha:g}"
        )
        sections.append(report.section(
            scenario.provider_free, result,
            heading=heading,
            config=config,
            provider_availability=scenario.provider_availability,
            booked=scenario.booked,
            requests=scenario.requests,
            client_availability=scenario.client_availability,
        ))
    return report.page(scenario.title, sections)


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Solve a scenario file and render it as HTML.",
        epilog=SCENARIO_HELP,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("scenario", type=Path, help="path to a scenario JSON file")
    parser.add_argument("-o", "--out", type=Path, help="output .html (default: alongside)")
    parser.add_argument(
        "--alpha",
        help="override alpha; comma-separated values render one run each "
             "(e.g. 0,0.5,1)",
    )
    parser.add_argument("--open", action="store_true", help="open in a browser")
    parser.add_argument("--text", action="store_true", help="also print an ASCII chart")
    args = parser.parse_args(argv)

    try:
        scenario = load(args.scenario)
        if args.alpha:
            alphas = [float(a) for a in args.alpha.split(",")]
        else:
            alphas = [scenario.config.alpha]
        page_html = build_page(scenario, alphas)
    except ScenarioError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    out = args.out or args.scenario.with_suffix(".html")
    out.write_text(page_html)

    for alpha in alphas:
        config = CostConfig(
            alpha=alpha,
            grid_minutes=scenario.config.grid_minutes,
            service_durations=scenario.config.service_durations,
        )
        result = solve_placements(scenario.requests, scenario.provider_free, config)
        print(f"alpha {alpha:g}: placed {len(result.placements)}/"
              f"{len(scenario.requests)} · fragmentation "
              f"{result.fragmentation_minutes}m · earliness {result.earliness_minutes}m")
        if args.text:
            print(render(scenario.provider_free, result))

    print(f"\nwrote {out}")
    if args.open:
        webbrowser.open(out.resolve().as_uri())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
