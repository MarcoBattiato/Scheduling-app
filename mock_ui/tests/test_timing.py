"""Time moves on: nothing is offered, moved or booked once it is too late.

A run plans from now, not from midnight; a booked session about to happen is
not asked to move; an offer whose time has passed cannot be accepted or
written down; a draft waiting for the provider is not made again every hour;
a session the clinic has to move stays within the reschedule window. And many
phones polling at once neither crash the server nor start two runs.
"""
import socket
import threading
import time as clock
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, time, timedelta

import httpx
import pytest

from mock_ui.state import PROVIDER, World


def on(days: int, hour: int, minute: int = 0) -> datetime:
    return datetime.combine(date.today() + timedelta(days=days), time(hour, minute))


@pytest.fixture
def world() -> World:
    w = World()
    w.policy.auto_run = False
    # Small on purpose: the planning window and the hours multiply the
    # solver's candidate slots for every request.
    w.policy.horizon_days = 5
    w.catalogue.add_service("s60", "Session", 60, 8000)
    every_day = [{"weekday": d, "from": "09:00", "to": "12:00"} for d in range(7)]
    w.set_weekly_availability(PROVIDER, every_day)
    for cid in ("alice", "bob"):
        w.add_client(cid, cid.title())
        w.set_weekly_availability(cid, every_day)
    return w


def draft(world):
    return next(p for p in world.plans.values() if p.status == "draft")


# -- 2. never into the past ---------------------------------------------------


def test_a_run_never_offers_a_time_that_has_gone(world):
    world.submit_request("alice", "s60", on(0, 9).isoformat())     # this morning

    world.propose(now=on(0, 10, 30))

    starts = [p["start"] for p in draft(world).placements]
    assert starts and all(s >= on(0, 11, 30) for s in starts), starts


# -- 5. no short-notice moves ----------------------------------------------------


@pytest.mark.parametrize("notice_hours, moved", [(24, False), (0, True)])
def test_a_session_about_to_happen_is_not_asked_to_move(world, notice_hours, moved):
    """Only 09-11 today: Alice can only come 09-10, where Bob is booked, and
    Bob could go to 10-11 — a move worth asking for, unless it is 2h away."""
    world.policy.horizon_days = 1
    world.policy.min_notice_hours = notice_hours
    world.set_weekly_availability(PROVIDER, [{"weekday": d, "from": "09:00", "to": "11:00"}
                                             for d in range(7)])
    world.set_weekly_availability("alice", [{"weekday": d, "from": "09:00", "to": "10:00"}
                                            for d in range(7)])
    world.store.book_appointment("bob", "s60", on(0, 9), on(0, 10))
    world.submit_request("alice", "s60", on(0, 9).isoformat())

    world.propose(now=on(0, 7), max_displacements=1)

    asked = [p for p in world.plans.values() if p.displacements]
    assert bool(asked) is moved


# -- 3. late answers --------------------------------------------------------------


def _offer_yesterday(world):
    request = world.submit_request("alice", "s60", on(-1, 10).isoformat())
    world.propose(now=on(-1, 7))
    plan = draft(world)
    world.provider_approve(plan.id)
    offer = next(a for a in world.approvals.values() if a.client_id == "alice")
    return plan, offer, request


def test_an_offer_whose_time_has_passed_cannot_be_accepted(world):
    plan, offer, request = _offer_yesterday(world)

    with pytest.raises(ValueError, match="passed"):
        world.respond_to_approval(offer.id, "accept")

    assert offer.status == "withdrawn" and request.status == "on_hold"
    assert [n["kind"] for n in world.notices_for("alice")] == ["withdrawn"]


def test_a_yes_to_a_time_that_has_passed_is_never_written_down(world):
    plan, offer, request = _offer_yesterday(world)
    offer.status = "accepted"              # they said yes in time; nobody settled it

    world.settle_plan(plan.id, "agreed")

    assert world.store.appointments_for("alice", on(-1, 0), on(0, 0)) == []
    assert "passed before it was confirmed" in world.notices_for("alice")[0]["text"]


def test_waiting_offers_expire_on_their_own(world):
    plan, offer, request = _offer_yesterday(world)

    world.tick()

    assert offer.status == "withdrawn"
    assert not world.pending_holds(), "its slot is no longer held"


# -- 4. drafts do not pile up ---------------------------------------------------------


def test_a_waiting_draft_is_not_made_again_every_hour(world):
    world.policy.auto_run = True
    world.policy.retry_after_minutes = 60
    world.submit_request("alice", "s60", on(2, 10).isoformat())
    world.propose()

    for hours in range(1, 9):
        world.tick(now=datetime.now() + timedelta(hours=hours, minutes=1))

    assert sum(p.status == "draft" for p in world.plans.values()) == 1


# -- 6, 7. the clinic moving a session: within the window ------------------------------


@pytest.mark.parametrize("kind, original, expected", [
    ("week", datetime(2026, 10, 15, 10), datetime(2026, 10, 19)),     # Thu → next Monday
    ("month", datetime(2026, 10, 15, 10), datetime(2026, 11, 1)),
    ("days", datetime(2026, 10, 15, 10), datetime(2026, 10, 18, 10)),
    ("any", datetime(2026, 10, 15, 10), None),
])
def test_the_reschedule_window_is_the_providers_setting(world, kind, original, expected):
    world.policy.reschedule_window = kind
    world.policy.reschedule_window_days = 3
    assert world.reschedule_deadline(original) == expected


def test_a_day_off_moves_weekly_sessions_within_the_week_not_away(world):
    """Offered another time, not called off — and not into next week, where it
    would stack on next week's own session."""
    day = on(3, 10)
    series = world.start_series("alice", "s60", day.isoformat(), every_weeks=1)
    session = next(r for r in world.requests.values() if r.series_id == series.id)

    result = world.provider_away(day.replace(hour=0), day.replace(hour=23, minute=59))

    assert result["sessions_moved"] == [session.id]
    assert session.status == "pending" and session.latest == world.reschedule_deadline(day)
    assert [n["kind"] for n in world.notices_for("alice")] == ["changed"]

    world.propose()
    placed = next(p for p in draft(world).placements if p["request_id"] == session.id)
    assert placed["start"].date() != day.date() and placed["end"] <= session.latest


def test_a_day_off_is_offered_as_a_move_by_default(world):
    booked = world.store.book_appointment("bob", "s60", on(3, 10), on(3, 11))

    result = world.provider_away(on(3, 0), on(3, 23, 59))

    assert result["rehoused"], "the provider has to say 'cancel' for a cancellation"
    request = next(r for r in world.requests.values() if r.replaces_appointment_id == booked.id)
    assert request.latest == world.reschedule_deadline(booked.range.start) and request.keep_looking
    assert world.store.get_appointment(booked.id).status.value == "booked", "kept until moved"
    assert [n["kind"] for n in world.notices_for("bob")] == ["changed"]


def test_a_booking_with_no_time_left_in_its_window_is_looked_for_further_out(world):
    booked = world.store.book_appointment("bob", "s60", on(3, 10), on(3, 11))
    world.provider_away(on(3, 0), on(3, 23, 59))
    request = next(r for r in world.requests.values() if r.replaces_appointment_id == booked.id)
    request.latest = on(0, 0)                             # the window has closed

    world.tick()

    assert request.latest is None and request.status == "pending", "there is no alternative"


# -- 8. the client's own time away ------------------------------------------------------


def test_a_client_away_is_told_that_not_that_no_time_was_found(world):
    day = on(3, 10)
    world.set_exception("alice", day.date(), time(0, 0), time(23, 59), available=False)

    series = world.start_series("alice", "s60", day.isoformat(), every_weeks=1)

    session = next(r for r in world.requests.values() if r.series_id == series.id)
    assert session.status == "lapsed" and session.lapsed_reason == World.SKIP_CLIENT_AWAY
    assert "you told us you're away" in world.notices_for("alice")[0]["text"]


# -- 1. many phones at once -------------------------------------------------------------


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_many_phones_polling_at_once_neither_crash_nor_run_twice(world, monkeypatch):
    """A real server, one event loop, 30 polls at once while a run is due:
    every poll answers, and exactly one run happens."""
    import uvicorn
    from mock_ui import app as app_module

    world.policy.auto_run = True
    for i in range(6):
        world.submit_request("alice" if i % 2 else "bob", "s60", on(1 + i % 3, 9 + i % 3).isoformat())
    monkeypatch.setattr(app_module, "world", world)
    monkeypatch.setattr(app_module, "_gate", None)

    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app_module.app, port=port, log_level="error"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(100):
        if server.started:
            break
        clock.sleep(0.05)

    try:
        with ThreadPoolExecutor(30) as pool:
            codes = list(pool.map(
                lambda i: httpx.get(f"http://127.0.0.1:{port}/api/state"
                                    + ("" if i % 2 else "?as=alice"), timeout=60).status_code,
                range(30)))
    finally:
        server.should_exit = True
        thread.join(10)

    assert codes == [200] * 30
    assert len(world.plans) == 1, "one run, not one per poll"


# -- a move for a session that has since happened -------------------------------------


def test_a_move_for_a_session_that_has_happened_ends_and_never_rewrites_it(world):
    """The session went ahead before a new time was found. Placing the move
    afterwards added a session and cancelled the one that took place."""
    booked = world.store.book_appointment("bob", "s60", on(1, 10), on(1, 11))
    world.provider_away(on(1, 0), on(1, 23, 59), action="flag")
    (request_id,) = world.rehouse_orphans()
    request = world.requests[request_id]
    world.store.mark_attendance(booked.id, attended=True)       # it happened after all

    world.tick()

    assert request.status == "withdrawn"
    assert world.store.get_appointment(booked.id).status.value == "completed"
    assert "no longer being moved" in world.notices_for("bob")[-1]["text"] \
        or "no longer being moved" in world.notices_for("bob")[0]["text"]


def test_a_solver_that_runs_out_of_time_changes_nothing(world, monkeypatch):
    from mock_ui import state as state_module
    world.submit_request("alice", "s60", on(2, 10).isoformat())

    def gives_up(*args, **kwargs):
        raise RuntimeError("placement solve returned UNKNOWN — expected at least a feasible solution")
    monkeypatch.setattr(state_module, "solve_placements", gives_up)

    out = world.propose()

    assert out["ran"] is False and "ran out of time" in out["reason"]
    assert not world.plans and all(r.status == "pending" for r in world.requests.values())


# -- bookings in closed time: the provider decides ---------------------------------------


def _stranded_tomorrow(world):
    booked = world.store.book_appointment("bob", "s60", on(1, 10), on(1, 11))
    world.set_weekly_availability(PROVIDER, [])          # e.g. hours changed, "not now"
    return booked


def test_a_booking_left_in_closed_time_comes_to_the_provider_before_the_day(world):
    booked = _stranded_tomorrow(world)

    (item,) = world.needs_decision()

    assert item["appointment_id"] == booked.id and not item["past"]
    assert world.store.get_appointment(booked.id).status.value == "booked", "nothing by itself"
    assert world.notices_for("bob") == [], "nobody is told before the provider decides"


def test_far_off_ones_wait_until_48_hours_before(world):
    world.store.book_appointment("bob", "s60", on(4, 10), on(4, 11))
    world.set_weekly_availability(PROVIDER, [])

    assert world.needs_decision() == []


@pytest.mark.parametrize("how, status, told", [
    ("cancel", "cancelled_by_provider", ["cancelled"]),
    ("keep", "booked", []),          # never told anything was wrong, so nothing to undo
])
def test_the_provider_decides_and_the_client_is_told(world, how, status, told):
    booked = _stranded_tomorrow(world)

    world.decide(booked.id, how)

    assert world.store.get_appointment(booked.id).status.value == status
    assert [n["kind"] for n in world.notices_for("bob")] == told
    assert world.needs_decision() == []


def test_a_past_one_stays_until_decided_rather_than_looking_as_if_it_happened(world):
    booked = world.store.book_appointment("bob", "s60", on(-1, 10), on(-1, 11))
    world.set_weekly_availability(PROVIDER, [])

    (item,) = world.needs_decision()

    assert item["appointment_id"] == booked.id and item["past"]


def test_a_rehouse_that_finds_nothing_is_still_the_providers_to_decide(world):
    booked = _stranded_tomorrow(world)
    world.decide(booked.id, "rehouse")

    (item,) = world.needs_decision()
    assert item["being_moved"]
    world.decide(booked.id, "cancel")

    assert not [r for r in world.requests.values()
                if r.replaces_appointment_id == booked.id and r.status != "withdrawn"]
    assert [n["kind"] for n in world.notices_for("bob")] == ["cancelled", "changed"]


# -- R3: a closure announced early is planned early -----------------------------------------


def test_a_closure_announced_early_looks_for_new_times_at_once(world):
    world.policy.horizon_days = 3
    day = on(9, 10)
    series = world.start_series("alice", "s60", (day - timedelta(days=14)).isoformat(), every_weeks=1)

    world.provider_away(day.replace(hour=0), day.replace(hour=23, minute=59))

    session = next(r for r in world.requests.values()
                   if r.series_id == series.id and r.preferred_start == day)
    assert session.latest == world.reschedule_deadline(day)
    world.propose()
    assert any(p["request_id"] == session.id for p in draft(world).placements), \
        "planned now, beyond the usual window, inside its own week"


# -- R6: a client's own move respects what is promised ---------------------------------------


def test_a_client_cannot_move_into_a_slot_offered_to_somebody_else(world):
    world.submit_request("alice", "s60", on(2, 10).isoformat())
    world.propose()
    world.provider_approve(draft(world).id)
    offer = next(a for a in world.approvals.values() if a.client_id == "alice")
    mine = world.store.book_appointment("bob", "s60", on(3, 9), on(3, 10))

    with pytest.raises(ValueError, match="promised"):
        world.move_appointment(mine.id, offer.now_start, offer.now_end)


def test_keeping_one_the_client_was_told_about_tells_them_it_goes_ahead(world):
    booked = _stranded_tomorrow(world)
    world.decide(booked.id, "rehouse")             # "a new time is being looked for"

    world.decide(booked.id, "keep")

    assert world.notices_for("bob")[0]["text"].endswith("goes ahead as booked.")
