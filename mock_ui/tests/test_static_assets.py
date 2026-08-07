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
    checker = """
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

    state = TestClient(app_module.app).get("/api/state").json()

    harness = """
      const vm = require("vm"), fs = require("fs");
      const files = process.argv.slice(1, -1);
      const state = JSON.parse(fs.readFileSync(process.argv[process.argv.length - 1], "utf8"));
      // Only ids that are really in the page resolve. A stub that invents
      // elements cannot notice one that is missing, which is the whole point.
      const html = fs.readFileSync(files[0].replace(/[^/]+$/, "index.html"), "utf8");
      const realIds = new Set([...html.matchAll(/id="([^"]+)"/g)].map((m) => m[1]));
      const noop = () => {}, els = {}, missing = [];
      const el = () => ({innerHTML: "", style: {}, textContent: "", value: "2",
                         options: {length: 0}, addEventListener: noop,
                         querySelector: () => null, getBoundingClientRect: () => (
                           {left: 0, top: 0, width: 800, height: 400, right: 800, bottom: 400})});
      const sandbox = {
        console, CSS: {escape: (s) => s}, setInterval: noop, setTimeout: noop,
        document: {addEventListener: noop,
                   getElementById: (id) => {
                     if (!realIds.has(id)) { missing.push(id); return null; }
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
    harness = """
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
        block = re.search(rf"{re.escape(selector)} \{{(.*?)\}}", css, re.S)
        found = re.search(r"z-index:\s*(\d+)", block.group(1)) if block else None
        return int(found.group(1)) if found else 0

    assert z(".cal-avail") < z(".cal-exc") < z(".cal-block"), (
        "an appointment must never be drawn under the availability markings"
    )


@node
def test_the_schedule_distinguishes_every_state_a_slot_can_be_in():
    """Booked, cancelled, asked-to-move, and the slot it would move to must be
    separable at a glance, and the move must be drawn as a link between the two.
    """
    harness = """
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
