import json
from html.parser import HTMLParser
from pathlib import Path

import pytest

from scheduling_engine import CostConfig, solve_placements
from scheduling_engine.playground import (
    ScenarioError,
    _diagnose,
    build_page,
    load,
    parse,
)

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


def test_equal_durations_are_drawn_equal_width_on_every_day():
    """Each day shares one clock axis. Scaling days independently made an hour
    on a short day wider than an hour on a long one, so a booking moved to a
    quieter day appeared to have been shortened.
    """
    import re

    page = build_page(parse(DISPLACEMENT), [0.5])

    # dana's 60 minutes: once where it was (Mon), once where it lands (Tue).
    widths = {
        round(float(w), 2)
        for w in re.findall(
            r"class='bar (?:vacated|arrived)' style='left:[\d.]+%;width:([\d.]+)%'",
            page,
        )
    }
    assert len(widths) == 1, f"same duration drawn at different widths: {widths}"

    # The placement is the same 60 minutes, so it must draw the same width too.
    (hour,) = widths
    placed = {
        round(float(w), 2)
        for w in re.findall(r"class='bar placed' style='left:[\d.]+%;width:([\d.]+)%'", page)
    }
    assert placed == {hour}, f"60 minutes drawn as {placed}, expected {hour}"


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
             "provider", "booked", "clients", "requests", "max_displacements"}
    assert set(raw) <= known, f"undocumented top-level keys: {set(raw) - known}"


# Built inline rather than read from examples/: those files exist to be edited
# by whoever is trying a scenario out, so pinning tests to their contents makes
# ordinary experimentation look like a broken build.
DISPLACEMENT = {
    "max_displacements": 1,
    "provider": {"availability": [
        {"date": "2026-05-04", "from": "09:00", "to": "10:00"},
        {"date": "2026-05-05", "from": "09:00", "to": "17:00"}]},
    "booked": [
        {"date": "2026-05-04", "from": "09:00", "to": "10:00",
         "client": "dana", "movable": True},
        {"date": "2026-05-05", "from": "09:00", "to": "10:00",
         "client": "frank", "locked": True, "movable": True}],
    "clients": {
        "dana": {"availability": [{"date": "2026-05-05", "from": "10:00", "to": "17:00"}],
                 "reschedule_bounds": {"earlier": 0, "later": 3}},
        "frank": {"availability": [{"date": "2026-05-05", "from": "09:00", "to": "17:00"}]}},
    "requests": [{"id": "urgent", "client": "alice", "duration": 60,
                  "windows": [{"date": "2026-05-04", "from": "09:00", "to": "10:00"}]}],
}


def test_movable_bookings_are_wired_through_from_json():
    scenario = parse(DISPLACEMENT)

    assert scenario.max_displacements == 1
    # frank is flagged movable but locked, so must never be offered up.
    assert {m.client_id for m in scenario.movable} == {"dana"}
    assert all(m.allowed for m in scenario.movable), "each needs somewhere to go"


def test_reschedule_bounds_from_json_limit_where_a_booking_may_go():
    scenario = parse(DISPLACEMENT)
    (dana,) = scenario.movable

    # {earlier: 0} forbids moving to an earlier day than the one booked.
    assert all(w.start.date() >= dana.range.start.date() for w in dana.allowed)


def test_displacement_from_json_changes_the_outcome():
    scenario = parse(DISPLACEMENT)
    config = CostConfig(alpha=scenario.config.alpha)

    without = solve_placements(scenario.requests, scenario.provider_free, config)
    assert without.unplaced == ("urgent",)

    with_moves = solve_placements(
        scenario.requests, scenario.provider_free, config,
        movable=scenario.movable, max_displacements=scenario.max_displacements,
    )
    assert with_moves.all_placed
    assert len(with_moves.displacements) == 1


def test_page_shows_rebookings():
    page = build_page(parse(DISPLACEMENT), [0.5])

    assert_well_formed(page)
    for fragment in ("Movable bookings", "Rebooked", "Already-agreed bookings"):
        assert fragment in page, f"missing: {fragment}"


def _notes(raw, alpha=0.5):
    scenario = parse(raw)
    config = CostConfig(alpha=alpha, grid_minutes=scenario.config.grid_minutes)
    result = solve_placements(
        scenario.requests, scenario.provider_free, config,
        movable=scenario.movable, max_displacements=scenario.max_displacements,
    )
    return " ".join(_diagnose(scenario, result))


# Monday is full; the request needs the hour dana occupies.
BLOCKED = {
    "provider": {"availability": [{"date": "2026-05-04", "from": "09:00", "to": "10:00"}]},
    "booked": [{"date": "2026-05-04", "from": "09:00", "to": "10:00", "client": "dana"}],
    "clients": {"dana": {"availability": [
        {"date": "2026-05-05", "from": "09:00", "to": "17:00"}]}},
    "requests": [{"id": "r1", "client": "alice", "duration": 60,
                  "windows": [{"date": "2026-05-04", "from": "09:00", "to": "10:00"}]}],
}


def test_a_request_that_did_not_fit_names_the_switch_that_is_missing():
    """Displacement needs two independent switches. Setting neither, or only
    one, produces no effect and no error — which reads as the feature being
    broken unless the tool says otherwise.
    """
    neither = _notes(BLOCKED)
    assert '"max_displacements": 1' in neither
    assert '"movable": true' in neither

    only_flagged = json.loads(json.dumps(BLOCKED))
    only_flagged["booked"][0]["movable"] = True
    note = _notes(only_flagged)
    assert '"max_displacements": 1' in note
    assert '"movable": true' not in note, "should not ask for what is already set"

    only_capped = json.loads(json.dumps(BLOCKED))
    only_capped["max_displacements"] = 1
    note = _notes(only_capped)
    assert '"movable": true' in note
    assert '"max_displacements": 1' not in note


def test_half_configured_displacement_is_called_out_even_when_nothing_failed():
    """No request failed, so there is nothing to fix — but a switch set to no
    effect is still worth saying out loud rather than leaving to be discovered.
    """
    fits = json.loads(json.dumps(BLOCKED))
    fits["provider"]["availability"] = [
        {"date": "2026-05-04", "from": "09:00", "to": "11:00"}]
    # The request must be able to reach the hour dana is not sitting in.
    fits["requests"][0]["windows"] = [
        {"date": "2026-05-04", "from": "09:00", "to": "11:00"}]

    only_flagged = json.loads(json.dumps(fits))
    only_flagged["booked"][0]["movable"] = True
    assert "max_displacements is 0" in _notes(only_flagged)

    only_capped = json.loads(json.dumps(fits))
    only_capped["max_displacements"] = 1
    assert "no booking is flagged" in _notes(only_capped)


def test_no_note_when_displacement_did_its_job():
    working = json.loads(json.dumps(BLOCKED))
    working["max_displacements"] = 1
    working["booked"][0]["movable"] = True
    working["provider"]["availability"].append(
        {"date": "2026-05-05", "from": "09:00", "to": "17:00"})

    assert _notes(working) == ""


def test_note_when_rescheduling_was_available_but_could_not_help():
    stuck = json.loads(json.dumps(BLOCKED))
    stuck["max_displacements"] = 1
    stuck["booked"][0]["movable"] = True
    # dana is only free on the 5th, and the provider is not open then.
    assert "nowhere to go" in _notes(stuck)


def test_readme_lists_every_example():
    """A scenario nobody is told about may as well not exist.

    `*_template.json` is exempt: those are pristine copies of a scenario that
    is already documented, kept so an edited one can be restored. Requiring a
    README entry for each would turn making a backup into a failing build.
    """
    readme = (EXAMPLES / "README.md").read_text()
    for path in EXAMPLES.glob("*.json"):
        if path.stem.endswith("_template"):
            continue
        assert path.name in readme, f"{path.name} is not mentioned in examples/README.md"
