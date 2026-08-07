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
      const noop = () => {}, els = {};
      const el = () => ({innerHTML: "", style: {}, textContent: "", value: "2",
                         options: {length: 0}, addEventListener: noop,
                         querySelector: () => null, getBoundingClientRect: () => (
                           {left: 0, top: 0, width: 800, height: 400, right: 800, bottom: 400})});
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
      sandbox.window = sandbox;   // in a browser these are the same object
      const problems = [];
      for (const f of files) {
        try { new vm.Script(fs.readFileSync(f, "utf8"), {filename: f}).runInContext(ctx); }
        catch (e) { problems.push(`${f}: ${e.message}`); }
      }
      setTimeout(() => {
        const drawn = (els["calendar"] || {}).innerHTML || "";
        console.log(JSON.stringify({
          problems,
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
