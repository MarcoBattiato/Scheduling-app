"""What the code review of October 2026 found, pinned so it stays fixed.

Each test is one finding: the state it left inconsistent, and what must hold
instead. Grouped as the review grouped them — confirming a schedule, time
off, robustness, and the planner's own consistency.
"""
from datetime import date, datetime, time, timedelta

import pytest
from fastapi.testclient import TestClient

from calendar_store import AppointmentStatus, Party
from mock_ui import app as app_module
from mock_ui import billing, control
from mock_ui.state import PROVIDER, TimeOffConflict, World


def on(days: int, hour: int, minute: int = 0) -> datetime:
    return datetime.combine(date.today() + timedelta(days=days), time(hour, minute))


@pytest.fixture
def world() -> World:
    w = World()
    w.policy.auto_run = False
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


def offered(world, request_id=None):
    w = world.submit_request("alice", "s60", on(2, 10).isoformat()) if request_id is None else None
    world.propose()
    plan = draft(world)
    world.provider_approve(plan.id)
    return plan, next(a for a in world.approvals.values() if a.client_id == "alice")


# -- confirming a schedule ------------------------------------------------------


def test_a_plan_everyone_has_answered_is_still_on_the_providers_screen(world):
    """It is exactly then that the provider has to confirm it."""
    plan, offer = offered(world)
    world.respond_to_approval(offer.id, "accept")

    assert plan.status == "answered"
    assert [p["id"] for p in world.snapshot()["plans"]] == [plan.id]


def test_approving_a_draft_made_before_a_withdrawal_offers_nothing(world):
    request = world.submit_request("alice", "s60", on(2, 10).isoformat())
    world.propose()
    plan = draft(world)
    world.withdraw_request(request.id)

    with pytest.raises(ValueError, match="out of date"):
        world.provider_approve(plan.id)
    assert request.status == "withdrawn" and not world.approvals


def test_approving_a_draft_made_before_a_hand_placement_books_nothing_twice(world):
    request = world.submit_request("alice", "s60", on(2, 10).isoformat())
    world.propose()
    plan = draft(world)
    world.place_manually(request.id, on(3, 11))

    with pytest.raises(ValueError, match="out of date"):
        world.provider_approve(plan.id)


def test_settling_a_move_for_a_booking_cancelled_since_resurrects_nothing(world):
    """Bob agreed to move, then cancelled; settling must not bring it back."""
    world.set_weekly_availability(PROVIDER, [{"weekday": d, "from": "09:00", "to": "11:00"}
                                             for d in range(7)])
    world.set_weekly_availability("alice", [{"weekday": on(3, 9).weekday(),
                                             "from": "09:00", "to": "10:00"}])
    bobs = world.store.book_appointment("bob", "s60", on(3, 9), on(3, 10))
    world.submit_request("alice", "s60", on(3, 9).isoformat())
    world.propose(max_displacements=1)
    plan = draft(world)
    world.provider_approve(plan.id)
    move = next(a for a in world.approvals.values() if a.kind == "reschedule")
    world.respond_to_approval(move.id, "accept")

    world.cancel_appointment(bobs.id, by=Party.CLIENT)

    assert move.status == "withdrawn", "the move is taken back at once"
    assert world.store.get_appointment(bobs.id).status is AppointmentStatus.CANCELLED_BY_CLIENT
    assert not [a for a in world.store.appointments_for("bob", on(3, 0), on(4, 0))]


def test_the_store_never_rewrites_history(world):
    gone = world.store.book_appointment("bob", "s60", on(-2, 9), on(-2, 10))
    world.store.mark_attendance(gone.id, attended=True)

    with pytest.raises(ValueError):
        world.store.cancel_appointment(gone.id, by=Party.CLIENT)
    with pytest.raises(ValueError):
        world.store.reschedule_appointment(gone.id, on(3, 9), on(3, 10))
    assert world.store.get_appointment(gone.id).status is AppointmentStatus.COMPLETED


def test_placing_a_move_by_hand_retires_the_old_booking(world):
    old = world.store.book_appointment("bob", "s60", on(2, 9), on(2, 10))
    move = world.request_reschedule(old.id, on(3, 10).isoformat(), release_slot=False)

    world.place_manually(move.id, on(3, 10))

    live = world.store.appointments_for("bob", on(0, 0), on(6, 0))
    assert [a.range.start for a in live] == [on(3, 10)], "one booking, at the new time"


def test_discarding_a_plan_already_with_clients_is_refused(world):
    plan, _ = offered(world)

    for undo in (world.discard_plan, world.provider_reject):
        with pytest.raises(ValueError, match="not a draft"):
            undo(plan.id)


# -- time off ----------------------------------------------------------------------


def test_a_booking_months_out_is_moved_as_well_as_listed(world):
    far = world.store.book_appointment("bob", "s60", on(60, 10), on(60, 11))

    with pytest.raises(TimeOffConflict) as asked:
        world.provider_away(on(60, 0), on(60, 23))
    assert [b["appointment_id"] for b in asked.value.bookings] == [far.id]

    out = world.provider_away(on(60, 0), on(60, 23), action="reschedule")
    assert [world.requests[r].replaces_appointment_id for r in out["rehoused"]] == [far.id]


def test_an_offer_out_for_time_taken_off_is_withdrawn(world):
    plan, offer = offered(world)
    world.respond_to_approval(offer.id, "accept")

    world.set_exception(PROVIDER, offer.now_start.date(), time(0, 0), time(23, 59),
                        available=False)

    assert offer.status == "withdrawn"
    assert world.notices_for("alice")[0]["kind"] == "withdrawn"
    world.settle_plan(plan.id, "agreed")
    assert not world.store.appointments_for("alice", on(0, 0), on(6, 0)), \
        "nothing booked into closed time"


def test_weekly_hours_closing_a_raised_session_offer_it_another_time(world):
    series = world.start_series("alice", "s60", on(2, 10).isoformat(), every_weeks=1)
    session = next(r for r in world.requests.values() if r.series_id == series.id)
    weekday = on(2, 10).weekday()

    world.set_weekly_availability(PROVIDER, [{"weekday": d, "from": "09:00", "to": "12:00"}
                                             for d in range(7) if d != weekday])

    assert session.latest == world.reschedule_deadline(on(2, 10))
    assert world.notices_for("alice")[0]["kind"] == "changed"


def test_anywhere_still_gives_a_moved_weekly_session_an_end(world):
    world.policy.reschedule_window = "any"
    series = world.start_series("alice", "s60", on(2, 10).isoformat(), every_weeks=1)
    session = next(r for r in world.requests.values() if r.series_id == series.id)

    world.provider_away(on(2, 0), on(2, 23))

    assert session.latest is not None and session.latest > on(2, 23)
    world.tick(now=on(3, 9))
    assert session.status == "pending", "not lapsed at the end of its own day"


def test_the_decision_card_shows_only_bookings_still_to_happen(world):
    gone = world.store.book_appointment("bob", "s60", on(-1, 9), on(-1, 10))
    world.store.mark_attendance(gone.id, attended=True)
    world.set_weekly_availability(PROVIDER, [])

    assert world.needs_decision() == []


def test_the_providers_own_blocks_are_not_stranded_bookings(world):
    world.catalogue.add_service("admin", "Admin", 60, 0, client_bookable=False)
    world.store.book_appointment(PROVIDER, "admin", on(2, 9), on(2, 10))

    world.provider_away(on(2, 0), on(2, 23))

    assert not world.requests, "nobody is asked to move the provider's own time"


# -- the planner -------------------------------------------------------------------


def test_one_far_off_move_does_not_widen_the_window_for_everybody(world):
    world.store.book_appointment("bob", "s60", on(20, 10), on(20, 11))
    world.policy.reschedule_window = "days"
    world.policy.reschedule_window_days = 7
    world.provider_away(on(20, 0), on(20, 23), action="reschedule")
    later = world.submit_request("alice", "s60", on(15, 10).isoformat())

    world.propose()

    assert later.status == "pending", "a wish beyond the horizon stays out of this run"


def test_a_booking_being_moved_counts_once_towards_the_weekly_limit(world):
    world.set_client_limits("bob", per_day=1, per_week=2)
    booked = world.store.book_appointment("bob", "s60", on(1, 9), on(1, 10))
    world.request_reschedule(booked.id, on(1, 11).isoformat(), release_slot=False)

    assert not world._week_is_full("bob", on(1, 9))


# -- robustness and cost -----------------------------------------------------------


def test_a_bad_payment_key_falls_back_instead_of_crashing(monkeypatch):
    monkeypatch.setattr(billing, "processor_from_environment",
                        lambda: (billing.SimulatedProcessor(), "the key was refused"))
    w = World()
    assert any("refused" in line for line in w.log)


def test_reading_your_own_screen_is_neither_saved_nor_audited():
    """Phones poll their messages every few seconds."""
    client = TestClient(app_module.app)
    client.post("/api/reset")
    before = len(app_module.world.audit)

    for _ in range(3):
        assert client.post("/api/commands/message.list?as=alice", json={"args": {}}).status_code == 200

    assert len(app_module.world.audit) == before
    assert app_module._is_read("/api/commands/message.list")
    assert not app_module._is_read("/api/commands/message.send")


def test_a_replaced_calendar_link_survives_a_restart(tmp_path, monkeypatch):
    monkeypatch.setattr(app_module, "SNAPSHOT", tmp_path / "s.json")
    client = TestClient(app_module.app)
    client.post("/api/reset")
    client.post("/api/calendar/alice")
    new = client.post("/api/calendar/alice/reset").json()

    saved = (tmp_path / "s.json").read_text()
    token = new.get("token") or next(iter(new.values()))
    assert str(token).split("/")[-1].replace(".ics", "") in saved


def test_a_clients_poll_works_out_only_that_clients_things(world, monkeypatch):
    asked = []
    real = world.availability_segments
    monkeypatch.setattr(world, "availability_segments",
                        lambda who, *a: asked.append(who) or real(who, *a))

    world.snapshot_for("alice")

    assert set(asked) == {"alice"}


def test_a_repeating_booking_for_a_withdrawn_service_ends_and_says_so(world):
    series = world.start_series("alice", "s60", on(2, 10).isoformat(), every_weeks=1)
    world.catalogue.deactivate_service("s60")

    world.raise_due_sessions(now=datetime.now() + timedelta(days=7))

    assert series.status == "stopped"
    assert "no longer offers it" in world.notices_for("alice")[0]["text"]
    with pytest.raises(ValueError):
        world.submit_request("alice", "s60", on(3, 10).isoformat())


def test_naming_another_clients_id_is_on_the_audit_trail(world):
    from scheduling_contracts import Principal, Role, Session
    gate = control.Gate(world)
    session = Session(Principal(Role.CLIENT, "alice"), via_assistant=True, conversation_id="c")
    before = len(world.audit)

    with pytest.raises(control.NotFound):
        gate.execute(session, control.build_command(
            "appointment.read", {"client_id": "bob", "window": {
                "start": "2026-01-01T00:00", "end": "2027-01-01T00:00"}}, session))

    assert world.audit[before:] and world.audit[-1]["outcome"] == "refused"


def test_asking_for_one_plans_questions_returns_only_that_plans(world):
    from scheduling_contracts import Principal, Role, Session
    plan, offer = offered(world)
    gate = control.Gate(world)
    session = Session(Principal(Role.PROVIDER))

    other = gate.execute(session, control.build_command("approval.list", {"plan_id": plan.id + 999}, session))
    this = gate.execute(session, control.build_command("approval.list", {"plan_id": plan.id}, session))

    assert other.result == [] and [a["approval_id"] for a in this.result] == [offer.id]


def test_an_undecided_booking_never_leaves_the_decision_card(world):
    """However long ago: it must not sit in the diary as if it happened."""
    old = world.store.book_appointment("bob", "s60", on(-200, 9), on(-200, 10))
    world.set_weekly_availability(PROVIDER, [])

    assert [d["appointment_id"] for d in world.needs_decision()] == [old.id]
