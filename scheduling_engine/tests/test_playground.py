import json
from html.parser import HTMLParser
from pathlib import Path

import pytest

from scheduling_engine.playground import ScenarioError, build_page, load, parse

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"
EXAMPLE = EXAMPLES / "scenario.json"


MINIMAL = {
    "provider": {"availability": [{"date": "2026-05-04", "from": "09:00", "to": "17:00"}]},
    "requests": [
        {"id": "r1", "client": "alice", "duration": 60,
         "windows": [{"date": "2026-05-04", "from": "09:00", "to": "17:00"}]}
    ],
}


class _Balance(HTMLParser):
    VOID = {"meta", "br", "img", "link", "input", "hr"}

    def __init__(self):
        super().__init__()
        self.stack, self.errors = [], []

    def handle_starttag(self, tag, attrs):
        if tag not in self.VOID:
            self.stack.append(tag)

    def handle_endtag(self, tag):
        if not self.stack or self.stack[-1] != tag:
            self.errors.append(f"</{tag}> closes {self.stack[-1:] or ['nothing']}")
        else:
            self.stack.pop()


def assert_well_formed(markup):
    parser = _Balance()
    parser.feed(markup)
    assert not parser.errors, parser.errors
    assert not parser.stack, f"unclosed: {parser.stack}"


def test_the_shipped_example_parses_and_solves():
    scenario = load(EXAMPLE)

    assert scenario.requests
    assert scenario.provider_availability
    # Two bookings are cut out of three open days, so free time is not the same
    # as availability.
    assert scenario.provider_free != scenario.provider_availability
    assert set(scenario.client_availability) >= {"alice", "bob", "carol"}


def test_bookings_are_subtracted_from_what_the_solver_sees():
    scenario = parse({
        "provider": {"availability": [{"date": "2026-05-04", "from": "09:00", "to": "17:00"}]},
        "booked": [{"date": "2026-05-04", "from": "11:00", "to": "12:00", "client": "dana"}],
        "requests": MINIMAL["requests"],
    })

    free = [(s.start.hour, s.end.hour) for s in scenario.provider_free]
    assert free == [(9, 11), (12, 17)]


def test_a_window_may_span_several_days():
    scenario = parse({
        "provider": {"availability": [
            {"date": "2026-05-04", "from": "09:00", "to": "17:00"},
            {"date": "2026-05-05", "from": "09:00", "to": "17:00"},
        ]},
        "requests": [{"id": "r1", "client": "alice", "duration": 60, "windows": [
            {"from": "2026-05-04 15:00", "to": "2026-05-05 12:00"}
        ]}],
    })

    (window,) = scenario.requests[0].desired
    assert window.start.day == 4 and window.end.day == 5


def test_recurring_availability_is_materialised_over_the_window():
    scenario = parse({
        "window": {"from": "2026-05-04", "to": "2026-05-10"},
        "provider": {"recurring": [{"weekday": "tue", "from": "09:00", "to": "13:00"}]},
        "requests": [{"id": "r1", "client": "alice", "duration": 60, "windows": [
            {"from": "2026-05-04 00:00", "to": "2026-05-10 00:00"}
        ]}],
    })

    days = {s.start.date().isoformat() for s in scenario.provider_availability}
    assert days == {"2026-05-05"}  # the only Tuesday in the window


@pytest.mark.parametrize("raw,message", [
    ({"provider": {"availability": [{"date": "2026-05-04", "from": "09:00", "to": "08:00"}]}},
     "ends at or before it starts"),
    ({"provider": {"availability": [{"date": "2026-05-04", "from": "09:00"}]}},
     "missing 'to'"),
    ({"window": {"from": "2026-05-04", "to": "2026-05-10"},
      "provider": {"recurring": [{"weekday": "funday", "from": "09:00", "to": "17:00"}]}},
     "weekday must be one of"),
    ({"window": {"from": "2026-05-04", "to": "2026-05-05"}, "provider": {}},
     "no availability in the window"),
    ({"provider": {"availability": [{"date": "2026-05-04", "from": "09:00", "to": "17:00"}]},
      "requests": [{"id": "r1", "client": "a", "duration": 60, "windows": []}]},
     "needs at least one window"),
])
def test_bad_scenarios_explain_themselves(raw, message):
    with pytest.raises(ScenarioError, match=message):
        parse(raw)


def test_invalid_json_names_the_file():
    with pytest.raises(ScenarioError, match="invalid JSON"):
        load(Path(__file__))


def test_page_is_well_formed_html_with_one_run_per_alpha():
    page = build_page(load(EXAMPLE), [0.0, 1.0])

    assert_well_formed(page)
    assert page.count("<article class='run'>") == 2
    assert "alpha 0" in page and "alpha 1" in page


def test_page_shows_every_object_involved():
    page = build_page(parse({
        "provider": {"availability": [{"date": "2026-05-04", "from": "09:00", "to": "17:00"}]},
        "booked": [{"date": "2026-05-04", "from": "11:00", "to": "12:00", "client": "dana"}],
        "clients": {"alice": {"availability": [
            {"date": "2026-05-04", "from": "09:00", "to": "13:00"}]}},
        "requests": MINIMAL["requests"],
    }), [0.5])

    for lane in ("Availability", "Already booked", "Free", "Placed", "Gaps left",
                 "requested windows", "their availability"):
        assert lane in page, f"missing lane: {lane}"


def test_unplaceable_requests_are_called_out():
    page = build_page(parse({
        "provider": {"availability": [{"date": "2026-05-04", "from": "09:00", "to": "10:00"}]},
        "requests": [
            {"id": "r1", "client": "alice", "duration": 60,
             "windows": [{"date": "2026-05-04", "from": "09:00", "to": "10:00"}]},
            {"id": "r2", "client": "bob", "duration": 90,
             "windows": [{"date": "2026-05-04", "from": "09:00", "to": "10:00"}]},
        ],
    }), [0.5])

    assert "Not placed" in page
    assert "placed 1/2" in page


def test_bar_geometry_stays_inside_the_track():
    import re

    page = build_page(load(EXAMPLE), [0.0, 0.5, 1.0])
    for left, width in re.findall(r"left:([\d.]+)%;width:([\d.]+)%", page):
        assert 0 <= float(left) and float(left) + float(width) <= 100.01


@pytest.mark.parametrize(
    "path", sorted(EXAMPLES.glob("*.json")), ids=lambda p: p.name
)
def test_every_shipped_example_parses_solves_and_renders(path):
    """The examples are the documentation for the format — if one of them stops
    loading, the format has drifted from what people are told to copy.
    """
    scenario = load(path)
    page = build_page(scenario, [0.0, 1.0])

    assert_well_formed(page)
    assert scenario.requests, f"{path.name} has no requests to place"


@pytest.mark.parametrize(
    "path", sorted(EXAMPLES.glob("*.json")), ids=lambda p: p.name
)
def test_examples_use_documented_fields_only(path):
    raw = json.loads(path.read_text())
    known = {"title", "alpha", "grid_minutes", "service_durations", "window",
             "provider", "booked", "clients", "requests"}
    assert set(raw) <= known, f"undocumented top-level keys: {set(raw) - known}"


def test_readme_lists_every_example():
    """A scenario nobody is told about may as well not exist."""
    readme = (EXAMPLES / "README.md").read_text()
    for path in EXAMPLES.glob("*.json"):
        assert path.name in readme, f"{path.name} is not mentioned in examples/README.md"
