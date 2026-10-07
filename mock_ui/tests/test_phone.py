"""The phone front end, checked the way the desktop page is: load the script
the way the browser does, feed it real state, and look at what it draws.

Every screen is rendered for every person against the states that matter —
an untouched calendar, a draft proposal, a plan out with the clients, and a
plan where somebody has agreed to something that cannot yet happen. A view
that throws on one of those is a blank tab on somebody's phone.
"""
import json
import re
import shutil
import subprocess
import tempfile
from datetime import datetime, time
from pathlib import Path

import pytest

from mock_ui import scenarios
from mock_ui.state import World

STATIC = Path(__file__).resolve().parent.parent / "src" / "mock_ui" / "static"
node = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")

CLIENT_TABS = ["home", "book", "bookings", "hours", "messages"]
PROVIDER_TABS = ["home", "diary", "queue", "plans", "more", "more/hours", "more/away",
                 "more/clients", "more/services", "more/settings", "more/activity", "more/calendar", "more/payments", "more/billing", "more/messages", "more/assistant"]


# -- states worth drawing --------------------------------------------------


def _small():
    world = World()
    scenarios.load(world, "simple")
    return world


def untouched():
    return _small().snapshot()


def drafted():
    world = _small()
    world.propose()
    return world.snapshot()


def asked():
    world = _small()
    plan_id = world.propose()["plan_id"]
    world.provider_approve(plan_id)
    return world.snapshot()


def carol_agreed_dan_has_not():
    """Carol's slot is Dan's, and Dan has not answered. Her "yes" cannot be
    written down yet, and the screen must not offer to confirm it."""
    world = _small()
    plan_id = world.propose()["plan_id"]
    world.provider_approve(plan_id)
    carol = next(a for a in world.approvals.values() if a.client_id == "carol")
    world.respond_to_approval(carol.id, "accept")
    return world.snapshot()


def busy():
    world = World()
    scenarios.load(world, "busy")
    world.propose()
    return world.snapshot()


def repeating():
    """Carol comes every week, Tuesdays at nine."""
    world = _small()
    tuesday = scenarios._at(scenarios.next_monday(), 1, 9)
    world.start_series("carol", "session-60", tuesday.isoformat(), every_weeks=1)
    return world.snapshot()


STATES = {"untouched": untouched, "drafted": drafted, "asked": asked,
          "blocked": carol_agreed_dan_has_not, "busy": busy, "repeating": repeating}


# -- the harness ---------------------------------------------------------

HARNESS = r"""
  const vm = require("vm"), fs = require("fs");
  const [script, html, statePath, plan] = process.argv.slice(1);
  const stateText = fs.readFileSync(statePath, "utf8");
  const wanted = JSON.parse(plan);
  // Only ids really in the page resolve, so a renamed element is caught.
  const realIds = new Set([...fs.readFileSync(html, "utf8").matchAll(/id="([^"]+)"/g)].map((m) => m[1]));
  const noop = () => {}, els = {}, missing = [];
  const el = () => ({innerHTML: "", textContent: "", value: "", hidden: true, dataset: {}, style: {},
                     addEventListener: noop, querySelector: () => null, querySelectorAll: () => [],
                     classList: {add: noop, remove: noop, toggle: noop}});
  const sandbox = {
    console, setInterval: noop, setTimeout, clearTimeout,
    document: {
      addEventListener: noop, activeElement: null, title: "",
      querySelectorAll: () => [], querySelector: () => null,
      getElementById: (id) => {
        if (!realIds.has(id)) { missing.push(id); return null; }
        return els[id] = els[id] || el();
      },
    },
    location: {search: "?as=provider", hash: ""},
    history: {replaceState: noop},
    localStorage: {getItem: () => null, setItem: noop},
    URLSearchParams,
    fetch: async () => ({ok: true, text: async () => stateText, json: async () => JSON.parse(stateText)}),
    scrollTo: noop, addEventListener: noop,
  };
  sandbox.window = sandbox;
  const ctx = vm.createContext(sandbox);
  const out = {problems: [], missing: [], views: {}};
  try { new vm.Script(fs.readFileSync(script, "utf8"), {filename: script}).runInContext(ctx); }
  catch (e) { out.problems.push(`load: ${e.message}`); }
  setTimeout(() => {
    const P = sandbox.Phone;
    if (!P) { out.problems.push("window.Phone never defined"); console.log(JSON.stringify(out)); return; }
    out.actions = P.actions; out.forms = P.forms;
    for (const [who, tabs] of Object.entries(wanted)) {
      for (const tab of tabs) {
        try { out.views[`${who}:${tab}`] = P.show(who, tab) + "\n<!--tabs-->" + P.tabs(); }
        catch (e) { out.problems.push(`${who}:${tab}: ${e.stack.split("\n").slice(0, 2).join(" ")}`); }
      }
    }
    out.missing = [...new Set(missing)];
    console.log(JSON.stringify(out));
  }, 30);
"""


def draw(state: dict, plan: dict) -> dict:
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
        json.dump(state, fh, default=str)
        path = fh.name
    result = subprocess.run(
        ["node", "-e", HARNESS, str(STATIC / "phone.js"), str(STATIC / "phone.html"),
         path, json.dumps(plan)],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout.strip().splitlines()[-1])


def everyone(state: dict) -> dict:
    plan = {"provider": PROVIDER_TABS}
    for client in state["clients"]:
        plan[client["id"]] = CLIENT_TABS
    return plan


@pytest.fixture(scope="module")
def drawn():
    """Every screen, for every person, in every state — drawn once."""
    out = {}
    for name, build in STATES.items():
        state = build()
        out[name] = draw(state, everyone(state))
    return out


# -- it loads, and every screen draws ---------------------------------------


@node
def test_the_script_parses():
    result = subprocess.run(["node", "--check", str(STATIC / "phone.js")],
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


@node
@pytest.mark.parametrize("name", sorted(STATES))
def test_every_screen_draws_for_everyone(drawn, name):
    out = drawn[name]

    assert not out["problems"], out["problems"]
    assert not out["missing"], f"phone.js reaches for ids not in phone.html: {out['missing']}"
    blank = [k for k, html in out["views"].items() if len(html.split("<!--tabs-->")[0]) < 80]
    assert not blank, f"screens that drew next to nothing: {blank}"


@node
def test_every_button_has_something_to_do(drawn):
    """A data-act with no handler is a button that silently does nothing —
    the commonest way a delegated-click page breaks."""
    acts, forms = set(), set()
    for out in drawn.values():
        for html in out["views"].values():
            acts |= set(re.findall(r'data-act="([^"]+)"', html))
            forms |= set(re.findall(r'data-form="([^"]+)"', html))
    handled = set(next(iter(drawn.values()))["actions"])
    # "alpha" is read on input rather than dispatched, so the slider stays smooth.
    assert acts - handled - {"alpha"} == set()
    assert forms - set(next(iter(drawn.values()))["forms"]) == set()


@node
def test_a_client_and_the_provider_get_different_tabs(drawn):
    views = drawn["untouched"]["views"]
    client_tabs = views["carol:home"].split("<!--tabs-->")[1]
    provider_tabs = views["provider:home"].split("<!--tabs-->")[1]

    assert 'data-tab="book"' in client_tabs and 'data-tab="plans"' not in client_tabs
    assert 'data-tab="plans"' in provider_tabs and 'data-tab="book"' not in provider_tabs


# -- what each person is shown ---------------------------------------------


def screen(drawn, state, key):
    return drawn[state]["views"][key].split("<!--tabs-->")[0]


@node
def test_a_proposal_says_what_it_rests_on(drawn):
    """Carol only fits if Dan moves. A proposal that hides that looks
    free-standing, and the provider approves one part without the other."""
    plans = screen(drawn, "drafted", "provider:plans")

    assert "Only if Dan moves first" in plans
    assert "strip-track" in plans, "drawn against the day it lands in, not as a bare list"


@node
def test_being_asked_to_move_has_three_answers(drawn):
    dan = screen(drawn, "asked", "dan:home")

    for answer in ("accept", "decline", "refuse"):
        assert f'data-answer="{answer}"' in dan
    assert "Not that time" in dan and "Keep my slot" in dan


@node
def test_an_offer_has_two_answers_not_three(drawn):
    """There is nothing to refuse to move when nothing is being moved."""
    carol = screen(drawn, "asked", "carol:home")

    assert 'data-answer="accept"' in carol and 'data-answer="decline"' in carol
    assert 'data-answer="refuse"' not in carol


@node
def test_agreeing_does_not_make_it_vanish(drawn):
    """A client who said yes needs to see that the clinic has not confirmed
    yet, or "it went through" and "it was dropped" look the same."""
    carol = screen(drawn, "blocked", "carol:home")

    assert "waiting for the clinic" in carol


@node
def test_a_yes_that_cannot_happen_yet_is_not_offered_for_confirming(drawn):
    """The server holds Carol's booking until Dan agrees to vacate the slot.
    A button promising to confirm "the 1 agreed" would confirm nothing."""
    plans = screen(drawn, "blocked", "provider:plans")

    assert "waits for Dan" in plans
    assert "Nothing ready to confirm yet" in plans
    assert "Confirm the 1 agreed" not in plans


@node
def test_a_client_never_sees_anybody_else(drawn):
    """Scoping is not enforced by the server yet, so the views must not leak
    names on their own account."""
    others = {"Alice", "Bob", "Dan"}
    for tab in CLIENT_TABS:
        html = screen(drawn, "asked", f"carol:{tab}")
        assert not {name for name in others if name in html}, (tab, html[:400])


@node
def test_a_client_is_only_offered_times_they_are_free(drawn):
    """Carol is free Tuesdays 09:00-11:00 only, so the time chips on her
    booking screen are 09:00, 09:30 and 10:00 for an hour's session."""
    book = screen(drawn, "untouched", "carol:book")
    chips = re.findall(r'data-act="pick-time"[^>]*>(\d\d:\d\d)<', book)

    assert chips == ["09:00", "09:30", "10:00"], chips


@node
def test_the_diary_lays_overlaps_side_by_side():
    """An offered slot sits exactly on the booking it would replace; drawn on
    top of each other, one of them is invisible."""
    state = asked()
    tuesday = next(a["now"][:10] for a in state["approvals"] if a["client_id"] == "carol")

    out = draw(state, {"provider": [f"diary@{tuesday}"]})

    assert not out["problems"], out["problems"]
    html = out["views"][f"provider:diary@{tuesday}"]
    assert 'class="slot offered' in html, "Carol's offered slot is drawn"
    assert "(100% - 12px) / 2 * 1" in html, "and shares the width with Dan's booking"


# -- served, cached, and installable ----------------------------------------


@pytest.fixture
def client():
    from fastapi.testclient import TestClient
    from mock_ui import app as app_module
    return TestClient(app_module.app)


def test_the_phone_page_is_served_fresh_and_versioned(client):
    page = client.get("/phone")

    assert page.status_code == 200
    for asset in ("phone.js", "phone.css"):
        assert f"/static/{asset}?v=" in page.text, f"{asset} is not cache-busted"
    assert "no-store" in page.headers.get("cache-control", "")


def test_it_can_be_added_to_a_home_screen(client):
    manifest = client.get("/static/phone.webmanifest")

    assert manifest.status_code == 200
    assert "manifest" in manifest.headers["content-type"]
    body = manifest.json()
    assert body["start_url"] == "/phone"
    assert body["display"] == "standalone"
    for icon in body["icons"]:
        assert client.get(icon["src"]).status_code == 200, icon["src"]
    assert any(i["type"] == "image/png" and i["sizes"] == "512x512" for i in body["icons"]), (
        "Android wants a PNG at 512; SVG alone is not enough"
    )


def test_an_iphone_gets_a_home_screen_icon_it_can_use(client):
    """iOS ignores SVG and manifest icons on the Home Screen and uses the
    apple-touch-icon — and turns transparent corners black, so it must be a
    full-bleed PNG."""
    page = client.get("/phone").text
    href = re.search(r'rel="apple-touch-icon" href="([^"]+)"', page).group(1)
    icon = client.get(href)

    assert icon.status_code == 200
    assert icon.content[:8] == b"\x89PNG\r\n\x1a\n"


def test_the_phone_page_reports_a_broken_script():
    html = (STATIC / "phone.html").read_text()

    assert html.index("boot-error") < html.index("phone.js")
    assert "unhandledrejection" in html


def test_every_element_the_script_reaches_for_exists():
    html = (STATIC / "phone.html").read_text()
    wanted = set(re.findall(r'\$\("([a-z-]+)"\)', (STATIC / "phone.js").read_text()))

    assert not {i for i in wanted if f'id="{i}"' not in html}


def test_a_phone_opening_the_desktop_page_is_sent_to_the_phone_one():
    """And can still opt out, since the desktop page is the only place some
    things (the arrows, the hover peek) exist."""
    html = (STATIC / "index.html").read_text()
    head = html[:html.index("</head>")]

    assert 'location.replace("/phone"' in head
    assert "desktop=1" in head


def test_nothing_needs_a_mouse():
    """No hover-only behaviour and no drag: neither exists on a phone."""
    script = (STATIC / "phone.js").read_text()
    css = (STATIC / "phone.css").read_text()

    for event in ("mouseover", "mousedown", "mouseup", "dragstart"):
        assert event not in script
    assert ":hover" not in css


def test_inputs_are_big_enough_that_ios_does_not_zoom():
    """iOS zooms the page into any input under 16px and does not zoom back."""
    css = (STATIC / "phone.css").read_text()

    assert re.search(r"\.field input, \.field select \{[^}]*font-size: 16px", css)
    assert re.search(r"body \{[^}]*16px", css)


@node
def test_a_client_can_choose_how_often(drawn):
    book = screen(drawn, "untouched", "carol:book")

    for n in ("0", "1", "2", "4"):
        assert f'data-act="book-every" data-n="{n}"' in book


@node
def test_a_repeating_booking_is_listed_and_can_be_stopped(drawn):
    bookings = screen(drawn, "repeating", "carol:bookings")

    assert "Repeating" in bookings
    assert "every week · Tues 09:00" in bookings
    assert 'data-act="series-stop"' in bookings


@node
def test_a_session_raised_by_a_series_says_so(drawn):
    """In the provider's queue a repeating session should not look like a
    one-off: it is somebody's regular slot."""
    queue = screen(drawn, "repeating", "provider:queue")

    assert "every week" in queue


@node
def test_each_clients_limit_is_shown_to_the_provider(drawn):
    clients = screen(drawn, "untouched", "provider:more/clients")

    assert "1 a day" in clients


@node
def test_a_client_who_owes_is_shown_it_with_a_way_to_pay(drawn):
    home = screen(drawn, "busy", "dan:home")

    assert "to pay" in home and 'data-act="pay"' in home


@node
def test_a_client_never_sees_a_fee_the_provider_has_not_confirmed(drawn):
    """Frank's no-show fee is only proposed: until the provider decides, it is
    not his to see."""
    for tab in CLIENT_TABS:
        assert "Missed appointment" not in screen(drawn, "busy", f"frank:{tab}")


@node
def test_the_provider_is_asked_about_fees_and_shown_what_is_owed(drawn):
    home = screen(drawn, "busy", "provider:home")
    payments = screen(drawn, "busy", "provider:more/payments")

    assert "to confirm or waive" in home
    assert 'data-act="fee-confirm"' in payments and 'data-act="record-open"' in payments
