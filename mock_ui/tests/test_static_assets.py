"""The browser scripts, checked the way the browser loads them.

There is no build step, so nothing else would notice a syntax error or a name
declared in two files. Both are silent in a way that is badly misleading: the
page loads, the HTML is all there, and *nothing works*, because the second
script never executed.
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parent.parent / "src" / "mock_ui" / "static"
SCRIPTS = ["calendar.js", "app.js"]          # the order index.html loads them in

node = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")


@node
@pytest.mark.parametrize("name", SCRIPTS)
def test_each_script_parses(name):
    result = subprocess.run(
        ["node", "--check", str(STATIC / name)], capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr


@node
def test_the_scripts_do_not_collide_in_the_global_scope():
    """Classic scripts share one lexical scope, so `const x` in two of them is
    a SyntaxError — and it stops the *second* file executing entirely, which
    reads as the whole page being broken rather than a naming clash.
    """
    checker = r"""
      const vm = require("vm"), fs = require("fs");
      // slice(1), not slice(2): under `node -e` there is no script path, so
      // argv[1] is already the first argument. Getting this wrong drops the
      // first file and the check silently passes.
      const files = process.argv.slice(1);
      const noop = () => {};
      const ctx = vm.createContext({
        window: {}, console, CSS: {escape: (s) => s},
        setInterval: noop, setTimeout: noop,
        document: {addEventListener: noop, getElementById: () => null,
                   querySelectorAll: () => [], querySelector: () => null},
        location: {search: ""}, history: {replaceState: noop},
        URLSearchParams: class { get() { return null; } },
        fetch: () => new Promise(noop),
      });
      for (const f of files) {
        try {
          new vm.Script(fs.readFileSync(f, "utf8"), {filename: f}).runInContext(ctx);
        } catch (e) {
          // Runtime failures are expected without a real DOM; declaration
          // clashes are not.
          if (/already been declared|Identifier .* has already/.test(e.message)) {
            console.log(JSON.stringify({file: f, error: e.message}));
            process.exit(0);
          }
        }
      }
      console.log(JSON.stringify({}));
    """
    result = subprocess.run(
        ["node", "-e", checker, *[str(STATIC / s) for s in SCRIPTS]],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
    clash = json.loads(result.stdout.strip().splitlines()[-1])
    assert not clash, f"scripts collide: {clash}"


@node
def test_the_page_actually_renders_against_real_state():
    """Load both scripts the way the browser does, feed them a real snapshot,
    and check something got drawn.

    The two failures this catches are the ones nothing else would: a script
    that never executes, and a function that never reaches the global scope.
    Both leave a page that looks fine and does nothing.
    """
    from fastapi.testclient import TestClient
    from mock_ui import app as app_module

    # Reset first: without it this reads whatever session happens to be saved on
    # the machine, so the test would pass or fail depending on what someone was
    # last doing in the browser.
    client = TestClient(app_module.app)
    client.post("/api/reset")
    state = client.get("/api/state").json()

    harness = r"""
      const vm = require("vm"), fs = require("fs");
      const files = process.argv.slice(1, -1);
      const state = JSON.parse(fs.readFileSync(process.argv[process.argv.length - 1], "utf8"));
      // Only ids that are really in the page resolve. A stub that invents
      // elements cannot notice one that is missing, which is the whole point.
      const html = fs.readFileSync(files[0].replace(/[^/]+$/, "index.html"), "utf8");
      const realIds = new Set([...html.matchAll(/id="([^"]+)"/g)].map((m) => m[1]));
      // Only ids named as plain strings in the source are the page's promise to
      // keep; ones built from a template literal belong to elements the page
      // creates itself, and are legitimately absent from index.html.
      const app = fs.readFileSync(files[files.length - 1], "utf8");
      const promised = new Set([...app.matchAll(/\$\("([^"]+)"\)/g)].map((m) => m[1]));
      const noop = () => {}, els = {}, missing = [];
      const el = () => ({innerHTML: "", style: {}, textContent: "", value: "2",
                         options: {length: 0}, addEventListener: noop,
                         querySelector: () => null, getBoundingClientRect: () => (
                           {left: 0, top: 0, width: 800, height: 400, right: 800, bottom: 400})});
      const sandbox = {
        console, CSS: {escape: (s) => s}, setInterval: noop, setTimeout: noop,
        document: {addEventListener: noop,
                   getElementById: (id) => {
                     if (!realIds.has(id)) {
                       if (promised.has(id)) missing.push(id);
                       return null;
                     }
                     return els[id] = els[id] || el();
                   },
                   querySelectorAll: () => [], querySelector: () => null},
        location: {search: "?as=provider"}, history: {replaceState: noop},
        URLSearchParams: class { get() { return "provider"; } },
        fetch: async () => ({ok: true, json: async () => state}),
        alert: noop, confirm: () => false,
      };
      const ctx = vm.createContext(sandbox);
      sandbox.window = sandbox;   // in a browser these are the same object
      const problems = [];
      for (const f of files) {
        try { new vm.Script(fs.readFileSync(f, "utf8"), {filename: f}).runInContext(ctx); }
        catch (e) { problems.push(`${f}: ${e.message}`); }
      }
      setTimeout(() => {
        const drawn = (els["calendar"] || {}).innerHTML || "";
        console.log(JSON.stringify({
          problems, missing: [...new Set(missing)],
          calendar: drawn.includes("cal-week"),
          role: ((els["role"] || {}).innerHTML || "").includes("provider"),
        }));
      }, 50);
    """
    import tempfile
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
        json.dump(state, fh)
        state_path = fh.name

    result = subprocess.run(
        ["node", "-e", harness, *[str(STATIC / s) for s in SCRIPTS], state_path],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
    outcome = json.loads(result.stdout.strip().splitlines()[-1])

    assert not outcome["problems"], outcome["problems"]
    assert not outcome["missing"], f"app.js asked for ids not in the page: {outcome['missing']}"
    assert outcome["role"], "the role picker was never filled in"
    assert outcome["calendar"], "the calendar drew nothing"


def test_index_loads_every_script_it_needs():
    html = (STATIC / "index.html").read_text()
    for name in SCRIPTS:
        assert f"/static/{name}" in html, f"{name} is never loaded"
    # calendar.js defines what app.js calls, so order matters.
    assert html.index("calendar.js") < html.index("app.js")


def test_every_element_the_scripts_reach_for_exists():
    """A renamed id is another silent failure: the handler simply never fires."""
    html = (STATIC / "index.html").read_text()
    app = (STATIC / "app.js").read_text()

    import re
    wanted = set(re.findall(r'\$\("([a-z-]+)"\)', app))
    missing = {i for i in wanted if f'id="{i}"' not in html}
    assert not missing, f"app.js reaches for ids that are not in the page: {missing}"


def test_assets_are_served_fresh_and_versioned():
    """A cached copy of one script beside a fresh copy of another is a silent,
    baffling failure — the page loads and nothing works.
    """
    from fastapi.testclient import TestClient
    from mock_ui import app as app_module

    client = TestClient(app_module.app)
    page = client.get("/")

    for name in SCRIPTS:
        assert f"/static/{name}?v=" in page.text, f"{name} is not cache-busted"
    assert "no-store" in client.get("/static/app.js").headers.get("cache-control", "")


def test_the_page_says_so_when_a_script_fails():
    """Otherwise the only symptom is a page that does nothing, with the reason
    in a console nobody has open.
    """
    html = (STATIC / "index.html").read_text()

    assert 'id="boot-error"' in html
    assert html.index("boot-error") < html.index("calendar.js"), (
        "the handler must be registered before the scripts it reports on"
    )
    assert "unhandledrejection" in html, "a failed fetch must surface too"


@node
def test_the_calendar_marks_single_date_overrides():
    """The tint is the resolved availability, so an override only needs an
    outline: an added date is tinted inside the dashes, a removed one bare.
    Appointments must sit above both.
    """
    harness = r"""
      const vm = require("vm"), fs = require("fs");
      const files = process.argv.slice(1);
      const noop = () => {};
      let painted = "";
      const target = {innerHTML: "", querySelector: () => null,
                      getBoundingClientRect: () => ({left:0,top:0,width:800,height:400})};
      Object.defineProperty(target, "innerHTML",
        {set(v) { painted = v; }, get() { return painted; }});
      const sandbox = {console, CSS: {escape: (s) => s}, setInterval: noop,
        setTimeout: noop, document: {addEventListener: noop},
        location: {search: ""}, history: {replaceState: noop}};
      const ctx = vm.createContext(sandbox);
      sandbox.window = sandbox;
      new vm.Script(fs.readFileSync(files[0], "utf8")).runInContext(ctx);

      sandbox.window.renderCalendar(target, {
        weeks: 1, start: new Date("2026-05-04T00:00"),
        availability: [{start: "2026-05-04T09:00", end: "2026-05-04T10:00"},
                       {start: "2026-05-04T18:00", end: "2026-05-04T20:00"}],
        exceptions: [{start: "2026-05-04T10:00", end: "2026-05-04T11:00", kind: "remove"},
                     {start: "2026-05-04T18:00", end: "2026-05-04T20:00", kind: "add"}],
        blocks: [{id: "x", start: "2026-05-04T09:00", end: "2026-05-04T10:00",
                  label: "alice"}],
      });
      console.log(JSON.stringify({
        tint: (painted.match(/cal-avail/g) || []).length,
        added: painted.includes('cal-exc add'),
        removed: painted.includes('cal-exc remove'),
        block: painted.includes('cal-block'),
      }));
    """
    result = subprocess.run(
        ["node", "-e", harness, str(STATIC / "calendar.js")],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
    drawn = json.loads(result.stdout.strip().splitlines()[-1])

    assert drawn["tint"] == 2, "both available stretches are tinted"
    assert drawn["added"], "an added date is outlined"
    assert drawn["removed"], "a removed date is outlined too"
    assert drawn["block"], "appointments are still drawn"


def test_appointments_are_stacked_above_the_override_outlines():
    css = (STATIC / "style.css").read_text()
    import re

    def z(selector):
        # Anchored at line start: the same class also appears in descendant
        # rules like ".cal-day.dimmed .cal-exc", which carry no z-index and
        # would otherwise be read as the definition.
        block = re.search(rf"^{re.escape(selector)} \{{(.*?)\}}", css, re.S | re.M)
        assert block, f"no rule for {selector}"
        found = re.search(r"z-index:\s*(\d+)", block.group(1))
        return int(found.group(1)) if found else 0

    assert z(".cal-avail") < z(".cal-exc") < z(".cal-block"), (
        "an appointment must never be drawn under the availability markings"
    )


@node
def test_the_schedule_distinguishes_every_state_a_slot_can_be_in():
    """Booked, cancelled, asked-to-move, and the slot it would move to must be
    separable at a glance, and the move must be drawn as a link between the two.
    """
    harness = r"""
      const vm = require("vm"), fs = require("fs");
      const noop = () => {};
      const sandbox = {console, setInterval: noop, setTimeout: noop,
        document: {addEventListener: noop, getElementById: () => null},
        location: {search: ""}, history: {replaceState: noop},
        URLSearchParams: class { get() { return "provider"; } },
        fetch: () => new Promise(noop), CSS: {escape: (s) => s}};
      const ctx = vm.createContext(sandbox);
      sandbox.window = sandbox;
      for (const f of process.argv.slice(1)) {
        try { new vm.Script(fs.readFileSync(f, "utf8"), {filename: f}).runInContext(ctx); }
        catch (e) { /* no DOM: wiring fails, the pure function still loads */ }
      }
      const state = {
        appointments: [
          {id: 1, client_id: "alice", service: "Hour", status: "booked",
           origin: "client", start: "2026-05-04T09:00", end: "2026-05-04T10:00"},
          {id: 2, client_id: "bob", service: "Hour", status: "cancelled_by_client",
           origin: "client", start: "2026-05-04T11:00", end: "2026-05-04T12:00"},
          {id: 3, client_id: "carol", service: "Hour", status: "booked",
           origin: "client", start: "2026-05-05T09:00", end: "2026-05-05T10:00"},
        ],
        approvals: [
          {id: 7, kind: "reschedule", status: "pending", client_id: "carol",
           appointment_id: 3, was: "2026-05-05T09:00", was_end: "2026-05-05T10:00",
           now: "2026-05-06T14:00", now_end: "2026-05-06T15:00"},
        ],
      };
      const out = sandbox.window.scheduleLayers(state, "provider", null);
      const cls = (id) => (out.blocks.find((b) => b.id === id) || {}).cls;
      console.log(JSON.stringify({
        booked: cls("a1"), cancelled: cls("x2"), moving: cls("a3"),
        proposed: cls("p7"), arrows: out.arrows,
      }));
    """
    result = subprocess.run(
        ["node", "-e", harness, *[str(STATIC / s) for s in SCRIPTS]],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
    out = json.loads(result.stdout.strip().splitlines()[-1])

    assert "cancelled" not in (out["booked"] or ""), "an ordinary booking is plain"
    assert out["cancelled"] == "cancelled"
    assert "moving" in out["moving"], "an appointment asked to move is marked at-risk"
    assert out["proposed"] == "proposed-slot"
    assert out["arrows"] == [{"from": "a3", "to": "p7"}], (
        "the move must be drawn from the current slot to the proposed one"
    )


def test_the_calendar_palette_is_defined():
    """`background: var(--undefined)` is transparent, so a missing variable
    makes a whole layer silently invisible — which is how availability came to
    be absent from the schedule.
    """
    import re

    css = (STATIC / "style.css").read_text()
    scripts = "".join((STATIC / s).read_text() for s in SCRIPTS)

    used = set(re.findall(r"var\((--[a-z-]+)\)", css))
    # Several are declared per line, and some are set inline by the scripts.
    declared = set(re.findall(r"(--[a-z-]+)\s*:", css))
    declared |= set(re.findall(r"(--[a-z-]+)\s*:", scripts))

    assert not (used - declared), f"CSS variables used but never defined: {used - declared}"


@node
def test_overlapping_bookings_sit_beside_each_other():
    """A booking and the slot it is vacating occupy the same hour. Stacked, the
    newer one hides the older; side by side, both are readable — new on the
    left, given-up on the right.
    """
    harness = r"""
      const vm = require("vm"), fs = require("fs");
      const noop = () => {};
      let painted = "";
      const target = {querySelector: () => null,
                      getBoundingClientRect: () => ({left:0,top:0,width:800,height:400})};
      Object.defineProperty(target, "innerHTML", {set(v){painted=v;}, get(){return painted;}});
      const sandbox = {console, CSS: {escape: (s) => s}, document: {addEventListener: noop}};
      const ctx = vm.createContext(sandbox); sandbox.window = sandbox;
      new vm.Script(fs.readFileSync(process.argv[1], "utf8")).runInContext(ctx);

      sandbox.window.renderCalendar(target, {
        weeks: 1, start: new Date("2026-05-04T00:00"),
        blocks: [
          {id: "new", start: "2026-05-04T10:00", end: "2026-05-04T11:00",
           label: "arriving", order: 0},
          {id: "gone", start: "2026-05-04T10:00", end: "2026-05-04T11:00",
           label: "leaving", order: 2},
          {id: "alone", start: "2026-05-04T14:00", end: "2026-05-04T15:00",
           label: "solo", order: 1},
        ],
      });
      const grab = (id) => {
        const m = painted.match(new RegExp(`id="cb-${id}"[^>]*style="([^"]*)"`));
        const style = m ? m[1] : "";
        const left = /left:([\d.]+)%/.exec(style), width = /width:([\d.]+)%/.exec(style);
        return {left: left ? +left[1] : null, width: width ? +width[1] : null};
      };
      console.log(JSON.stringify({fresh: grab("new"), gone: grab("gone"), alone: grab("alone")}));
    """
    result = subprocess.run(["node", "-e", harness, str(STATIC / "calendar.js")],
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    out = json.loads(result.stdout.strip().splitlines()[-1])

    assert out["fresh"]["width"] == 50 and out["gone"]["width"] == 50, "the hour is shared"
    assert out["fresh"]["left"] < out["gone"]["left"], "the new one goes on the left"
    assert out["alone"]["width"] == 100, "a booking with nothing beside it keeps the day"


def test_arrows_are_drawn_over_the_bookings():
    """A displacement arrow that vanishes under a booking explains nothing."""
    import re

    css = (STATIC / "style.css").read_text()

    def z(selector):
        # Anchored at line start: the same class also appears in descendant
        # rules like ".cal-day.dimmed .cal-exc", which carry no z-index and
        # would otherwise be read as the definition.
        block = re.search(rf"^{re.escape(selector)} \{{(.*?)\}}", css, re.S | re.M)
        assert block, f"no rule for {selector}"
        found = re.search(r"z-index:\s*(\d+)", block.group(1))
        return int(found.group(1)) if found else 0

    assert z(".cal-arrows") > z(".cal-block"), "arrows must sit above the bookings"
    assert z(".cal-peek") < z(".cal-block"), (
        "the hovered client's availability answers a question about a booking; "
        "it must not cover it"
    )


def test_peeking_hides_every_general_availability_marking():
    """While showing one client's hours, nothing else describing availability
    should remain — an override outline would read as a mark on that client.
    """
    css = (STATIC / "style.css").read_text()

    assert ".cal-day.dimmed .cal-exc" in css, "override outlines must fade too"
    assert ".cal-day.dimmed .cal-avail" in css


def test_hovering_works_on_proposals_not_only_the_schedule():
    app = (STATIC / "app.js").read_text()

    assert ".draft-cal" in app.split("const CALENDARS")[1][:80], (
        "the peek must find a proposal calendar, not just #calendar"
    )
    for builder in ("proposedBookingData", "proposedMoveData"):
        assert builder in app, f"proposals need their own hover facts: {builder}"
    assert app.count("clientFacts(") >= 3, (
        "client background belongs on proposals too, not only on the schedule"
    )


def test_a_proposal_calendar_is_readable_rather_than_a_thumbnail():
    import re

    css = (STATIC / "style.css").read_text()
    rows = re.search(r"\.draft-cal \.cal-day[^{]*\{[^}]*var\(--rows\) \* (\d+)px", css)
    font = re.search(r"\.draft-cal \.cal-block \{[^}]*font-size: (\d+)px", css)

    assert rows and int(rows.group(1)) >= 15, "draft rows too short to read"
    assert font and int(font.group(1)) >= 9, "draft labels too small to read"


@node
def test_a_proposal_hover_card_says_as_much_as_the_schedule_one():
    """Run the real builders against a real plan that moves somebody.

    Asserting on the source text would pass on a card that throws the moment a
    field is missing from the payload; this catches the mismatch between what
    the API sends and what the card reads.
    """
    from datetime import date, datetime, time, timedelta

    from mock_ui.state import PROVIDER, World

    monday = date.today() + timedelta(days=(7 - date.today().weekday()) % 7 or 7)
    at = lambda d, h: datetime.combine(monday + timedelta(days=d), time(h))

    world = World()
    world.policy.horizon_days = 10
    world.catalogue.add_service("s60", "Hour", 60, 8000)
    for who, name in (("alice", "Alice"), ("bob", "Bob")):
        world.add_client(who, name)
    # One hour on one date is the only thing that will do for alice, and bob is
    # sitting in it — so the only plan available is to move him.
    world.set_weekly_availability(PROVIDER, [
        {"weekday": 0, "from": "09:00", "to": "10:00"},
        {"weekday": 1, "from": "09:00", "to": "17:00"},
    ])
    world.set_weekly_availability("alice", [])
    world.set_exception("alice", monday, time(9), time(10), available=True)
    world.set_weekly_availability("bob", [
        {"weekday": 0, "from": "09:00", "to": "17:00"},
        {"weekday": 1, "from": "09:00", "to": "17:00"},
    ])
    world.store.book_appointment("bob", "s60", at(0, 9), at(0, 10))
    world.submit_request("alice", "s60", at(0, 9).isoformat())
    world.propose()

    state = world.snapshot()
    plan = next((p for p in state["plans"] if p["displacements"]), None)
    assert plan, "the scenario stopped producing a displacement"

    harness = r"""
      const vm = require("vm"), fs = require("fs");
      const files = process.argv.slice(1, -1);
      const state = JSON.parse(fs.readFileSync(process.argv[process.argv.length - 1], "utf8"));
      const noop = () => {}, els = {};
      // Every id resolves here: this test is about what the cards say, not
      // about which elements the page promises — that is tested above.
      const el = () => ({innerHTML: "", style: {}, textContent: "", value: "2",
                         options: {length: 0}, addEventListener: noop,
                         querySelectorAll: () => [], querySelector: () => null,
                         getBoundingClientRect: () => (
                           {left: 0, top: 0, width: 800, height: 400,
                            right: 800, bottom: 400})});
      const sandbox = {
        console, CSS: {escape: (s) => s}, setInterval: noop, setTimeout: noop,
        document: {addEventListener: noop,
                   getElementById: (id) => (els[id] = els[id] || el()),
                   querySelectorAll: () => [], querySelector: () => null},
        location: {search: "?as=provider"}, history: {replaceState: noop},
        URLSearchParams: class { get() { return "provider"; } },
        fetch: async () => ({ok: true, json: async () => state}),
        alert: noop, confirm: () => false,
      };
      const ctx = vm.createContext(sandbox);
      sandbox.window = sandbox;
      for (const f of files) {
        new vm.Script(fs.readFileSync(f, "utf8"), {filename: f}).runInContext(ctx);
      }
      // `state` is a `let`, so it is not reachable from outside; refresh() is
      // how the page itself fills it in.
      vm.runInContext("refresh()", ctx).then(() => {
        const plan = state.plans.find((p) => p.displacements.length);
        console.log(JSON.stringify({
          booking: vm.runInContext("proposedBookingData", ctx)(plan.placements[0], plan),
          leaving: vm.runInContext("proposedMoveData", ctx)(plan.displacements[0], "from", plan),
          landing: vm.runInContext("proposedMoveData", ctx)(plan.displacements[0], "to", plan),
          schedule: vm.runInContext("hoverData", ctx)(
            state.appointments.find((a) => a.status === "booked")),
        }));
      }).catch((e) => { console.error(e.stack); process.exit(1); });
    """

    import tempfile
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
        json.dump(state, fh, default=str)
        state_path = fh.name

    result = subprocess.run(
        ["node", "-e", harness, *[str(STATIC / s) for s in SCRIPTS], state_path],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
    cards = json.loads(result.stdout.strip().splitlines()[-1])

    for name, card in cards.items():
        assert card["Client"] in ("Alice", "Bob"), f"{name} shows a raw id: {card}"
        assert "History" in card, f"{name} lost the client's background"
        assert not any(v is None or v == "" for v in card.values()), \
            f"{name} has an empty field: {card}"

    # The proposal side is the one that was thin. It should now carry at least
    # as much as the schedule side does.
    for side in ("booking", "leaving", "landing"):
        assert len(cards[side]) >= len(cards["schedule"]) - 1, \
            f"{side} says less than the schedule: {cards[side]}"

    assert "Only if" in cards["booking"], "the engine's dependency went unreported"
    assert "Bob" in cards["booking"]["Only if"]
    assert cards["leaving"]["What"].startswith("would be moved out")
    assert cards["landing"]["What"].startswith("would be moved here")


def _render_as(state, who):
    """Run the page for one role and hand back what each panel drew."""
    harness = r"""
      const vm = require("vm"), fs = require("fs");
      const files = process.argv.slice(1, -2);
      const state = JSON.parse(fs.readFileSync(process.argv[process.argv.length - 2], "utf8"));
      const who = process.argv[process.argv.length - 1];
      const noop = () => {}, els = {};
      const el = () => ({innerHTML: "", style: {}, textContent: "", value: "2",
                         options: {length: 0}, addEventListener: noop,
                         querySelectorAll: () => [], querySelector: () => null,
                         getBoundingClientRect: () => (
                           {left: 0, top: 0, width: 800, height: 400,
                            right: 800, bottom: 400})});
      const sandbox = {
        console, CSS: {escape: (s) => s}, setInterval: noop, setTimeout: noop,
        document: {addEventListener: noop,
                   getElementById: (id) => (els[id] = els[id] || el()),
                   querySelectorAll: () => [], querySelector: () => null},
        location: {search: `?as=${who}`}, history: {replaceState: noop},
        URLSearchParams: class { get() { return who; } },
        fetch: async () => ({ok: true, json: async () => state}),
        alert: noop, confirm: () => false, prompt: () => null,
      };
      const ctx = vm.createContext(sandbox);
      sandbox.window = sandbox;
      for (const f of files) {
        new vm.Script(fs.readFileSync(f, "utf8"), {filename: f}).runInContext(ctx);
      }
      vm.runInContext("refresh()", ctx).then(() => {
        const out = {};
        for (const [id, e] of Object.entries(els)) out[id] = e.innerHTML || "";
        console.log(JSON.stringify(out));
      }).catch((e) => { console.error(e.stack); process.exit(1); });
    """
    import tempfile
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
        json.dump(state, fh, default=str)
        path = fh.name

    result = subprocess.run(
        ["node", "-e", harness, *[str(STATIC / s) for s in SCRIPTS], path, who],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout.strip().splitlines()[-1])


def _world_with_answers():
    """Alice has agreed to her slot, Bob has not answered, and Carol has asked
    to move a booking of her own — every state the two new panels show."""
    from datetime import date, datetime, time, timedelta

    from mock_ui.state import PROVIDER, World

    monday = date.today() + timedelta(days=(7 - date.today().weekday()) % 7 or 7)
    at = lambda d, h: datetime.combine(monday + timedelta(days=d), time(h))

    world = World()
    world.policy.horizon_days = 10
    world.catalogue.add_service("s60", "Hour", 60, 8000)
    for who in ("alice", "bob", "carol"):
        world.add_client(who, who.title())
        world.set_weekly_availability(
            who, [{"weekday": d, "from": "09:00", "to": "13:00"} for d in range(5)])
    world.set_weekly_availability(
        PROVIDER, [{"weekday": d, "from": "09:00", "to": "13:00"} for d in range(5)])

    booked = world.store.book_appointment("carol", "s60", at(0, 12), at(0, 13))
    world.request_reschedule(booked.id, at(2, 10).isoformat(), release_slot=False)

    world.submit_request("alice", "s60", at(0, 9).isoformat())
    world.submit_request("bob", "s60", at(0, 10).isoformat())
    world.propose()
    plan = next(p for p in world.plans.values() if p.status == "draft")
    world.provider_approve(plan.id)
    alice = next(a for a in world.pending_approvals() if a.client_id == "alice")
    world.respond_to_approval(alice.id, "accept")
    return world


@node
def test_the_provider_is_shown_the_answers_and_the_three_ways_to_settle():
    import re

    def plan_id(html):
        return re.search(r"settle\((\d+),", html).group(1)

    panels = _render_as(_world_with_answers().snapshot(), "provider")
    settlement = panels.get("settlement", "")

    assert "agreed" in settlement and "waiting" in settlement
    for how in ("'agreed'", "'agreed_only'", "'reoptimise'"):
        assert f"settle({plan_id(settlement)}, {how})" in settlement, f"missing {how}"
    assert "not a booking" in settlement, (
        "the provider must not read agreement as a calendar"
    )
    assert "Alice" in settlement, "answers are shown by name, not by id"


@node
def test_a_client_sees_which_of_their_bookings_are_settled():
    world = _world_with_answers()
    carol = _render_as(world.snapshot(), "carol")["bookings"]

    assert "you asked to move" in carol
    assert "askToMove" not in carol, (
        "no offering to move a booking that is already on its way out"
    )

    alice = _render_as(world.snapshot(), "alice")["bookings"]
    assert "Nothing booked." in alice, "alice has agreed, but nothing is written"
