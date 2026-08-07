"""The HTTP surface the browser talks to. Thin by design, so these are thin."""
from datetime import date, datetime, time, timedelta

import pytest
from fastapi.testclient import TestClient

from mock_ui import app as app_module


@pytest.fixture
def client():
    c = TestClient(app_module.app)
    c.post("/api/reset")          # fresh, seeded world for every test
    # The scenarios book "next Monday", which is 1-7 days out depending on the
    # weekday the suite runs. A 7-day horizon would exclude it on a Monday.
    c.post("/api/settings", json={"horizon_days": 10})
    return c


def monday() -> date:
    today = date.today()
    return today + timedelta(days=(7 - today.weekday()) % 7 or 7)


def at(day_offset: int, hour: int) -> str:
    return datetime.combine(
        monday() + timedelta(days=day_offset), time(hour)
    ).isoformat()


def test_a_fresh_server_has_something_to_look_at(client):
    state = client.get("/api/state").json()

    assert {c["id"] for c in state["clients"]} == {"alice", "bob", "carol"}
    assert state["appointments"], "seeded bookings so the first page is not empty"
    assert state["weekly"]["provider-self"], "provider has a working week"


def test_the_page_is_served(client):
    page = client.get("/")
    assert page.status_code == 200
    assert "Scheduling mock" in page.text


def test_submitting_a_request_queues_it_without_scheduling_anything(client):
    """A request arriving is not a reason to re-plan the week."""
    response = client.post("/api/requests", json={
        "client_id": "alice", "service_id": "session-60",
        "preferred_start": at(0, 9),
    })

    assert response.status_code == 200
    state = client.get("/api/state").json()
    assert any(r["status"] == "pending" for r in state["requests"])


def test_the_provider_drives_the_proposal_and_the_client_settles_it(client):
    client.post("/api/settings", json={"auto_run": False})
    client.post("/api/requests", json={
        "client_id": "alice", "service_id": "session-60",
        "preferred_start": at(0, 9),
    })

    ran = client.post("/api/solve").json()
    assert ran["ran"] and ran["placements"] >= 1

    plan = client.get("/api/state").json()["plans"][0]
    assert plan["status"] == "draft"

    client.post(f"/api/plans/{plan['id']}/approve")
    approvals = client.get("/api/state").json()["approvals"]
    assert approvals and all(a["status"] == "pending" for a in approvals)

    for approval in approvals:
        client.post(f"/api/approvals/{approval['id']}", json={"accept": True})

    state = client.get("/api/state").json()
    assert any(r["status"] == "placed" for r in state["requests"])


def test_a_rejected_plan_is_not_left_in_front_of_the_provider(client):
    client.post("/api/settings", json={"auto_run": False})
    client.post("/api/requests", json={
        "client_id": "alice", "service_id": "session-60",
        "preferred_start": at(0, 9),
    })
    client.post("/api/solve")
    plan = client.get("/api/state").json()["plans"][0]

    assert client.post(f"/api/plans/{plan['id']}/reject").status_code == 200
    assert client.post(f"/api/plans/{plan['id']}/approve").status_code == 409


def test_the_scheduler_policy_is_configurable(client):
    client.post("/api/settings", json={
        "auto_run": False, "urgency_hours": 6, "retry_after_minutes": 15,
    })

    scheduler = client.get("/api/state").json()["scheduler"]
    assert scheduler["auto_run"] is False
    assert scheduler["urgency_hours"] == 6
    assert scheduler["retry_after_minutes"] == 15


def test_every_tab_sees_the_same_world(client):
    """Two clients, one server: what one does the other must see. This is the
    only reason the mock is a server rather than a static page.
    """
    client.post("/api/requests", json={
        "client_id": "alice", "service_id": "session-60",
        "preferred_start": at(0, 9),
    })

    other_tab = TestClient(app_module.app)
    seen = other_tab.get("/api/state").json()

    assert any(r["client_id"] == "alice" for r in seen["requests"])


def test_availability_can_be_set_and_read_back(client):
    client.post("/api/availability", json={
        "client_id": "alice",
        "ranges": [{"weekday": 2, "from": "10:00", "to": "14:00"}],
    })

    weekly = client.get("/api/state").json()["weekly"]["alice"]
    assert weekly == [{"weekday": 2, "from": "10:00", "to": "14:00"}]


def test_cancelling_an_appointment_frees_it(client):
    state = client.get("/api/state").json()
    booked = next(a for a in state["appointments"] if a["status"] == "booked")

    assert client.post(f"/api/appointments/{booked['id']}/cancel").status_code == 200

    after = client.get("/api/state").json()
    assert not any(a["id"] == booked["id"] and a["status"] == "booked"
                   for a in after["appointments"])
    # Cancelled *by whom* is part of the status, so history can tell a client
    # dropping their slot from the provider closing the day.
    assert any(a["id"] == booked["id"] and a["status"] == "cancelled_by_client"
               for a in after["appointments"]), "the record survives"


def test_the_provider_cancelling_is_recorded_differently(client):
    state = client.get("/api/state").json()
    booked = next(a for a in state["appointments"] if a["status"] == "booked")

    client.post(f"/api/appointments/{booked['id']}/cancel", json={"by_provider": True})

    after = client.get("/api/state").json()
    assert any(a["id"] == booked["id"] and a["status"] == "cancelled_by_provider"
               for a in after["appointments"])


def test_the_provider_can_record_attendance(client):
    state = client.get("/api/state").json()
    booked = next(a for a in state["appointments"] if a["status"] == "booked")

    assert client.post(f"/api/appointments/{booked['id']}/attendance",
                       json={"attended": False}).status_code == 200

    after = client.get("/api/state").json()
    assert any(a["id"] == booked["id"] and a["status"] == "no_show"
               for a in after["appointments"])


def test_attendance_cannot_be_recorded_for_something_that_never_happened(client):
    state = client.get("/api/state").json()
    booked = next(a for a in state["appointments"] if a["status"] == "booked")
    client.post(f"/api/appointments/{booked['id']}/cancel")

    response = client.post(f"/api/appointments/{booked['id']}/attendance",
                           json={"attended": True})

    assert response.status_code == 409
    assert "cancelled" in response.json()["detail"]


def test_settings_are_clamped_to_something_sensible(client):
    client.post("/api/settings", json={"alpha": 5, "max_displacements": -3})

    settings = client.get("/api/state").json()["settings"]
    assert settings["alpha"] == 1.0
    assert settings["max_displacements"] == 0


def test_asking_for_a_service_that_does_not_exist_is_refused(client):
    response = client.post("/api/requests", json={
        "client_id": "alice", "service_id": "nonexistent",
        "preferred_start": at(0, 9),
    })

    assert response.status_code == 400
    assert "no service" in response.json()["detail"]


def test_a_request_without_a_wished_for_time_is_refused(client):
    """The time is the whole of what a request now says; availability supplies
    the rest."""
    response = client.post("/api/requests", json={
        "client_id": "alice", "service_id": "session-60",
    })

    assert response.status_code == 422


def test_unknown_ids_are_not_found_rather_than_crashing(client):
    assert client.post("/api/appointments/99999/cancel").status_code == 404
    assert client.post("/api/requests/99999/withdraw").status_code == 404
    assert client.post("/api/approvals/99999", json={"accept": True}).status_code == 404


def test_a_session_survives_being_saved_and_reloaded(client, tmp_path):
    from mock_ui import persistence

    client.post("/api/requests", json={
        "client_id": "alice", "service_id": "session-60",
        "preferred_start": at(0, 9),
    })
    before = client.get("/api/state").json()

    path = tmp_path / "session.json"
    persistence.save(app_module.world, path)
    restored = persistence.load(path)

    assert set(restored.clients) == set(app_module.world.clients)
    assert len(restored.snapshot()["appointments"]) == len(before["appointments"])
    # Ids must survive, or the supersedes chain stops meaning anything.
    assert ({a.id for a in restored.store._appointments}
            == {a.id for a in app_module.world.store._appointments})


def test_the_catalogue_is_visible_and_editable_by_the_provider(client):
    state = client.get("/api/state").json()
    assert {s["id"] for s in state["services"]} >= {"session-60", "session-90"}

    client.post("/api/services", json={
        "id": "intake", "name": "Intake", "duration_minutes": 90,
        "price_minor_units": 12000,
    })
    assert "intake" in {s["id"] for s in client.get("/api/state").json()["services"]}


def test_a_discontinued_service_disappears_from_sale_but_not_from_the_record(client):
    client.post("/api/services/session-90/active?active=false")

    services = {s["id"]: s for s in client.get("/api/state").json()["services"]}
    assert services["session-90"]["active"] is False, "still listed to the provider"

    booked = client.post("/api/requests", json={
        "client_id": "alice", "service_id": "session-90",
        "preferred_start": at(0, 9),
    })
    assert booked.status_code == 200, "existing ids still resolve for rescheduling"


def test_duplicate_service_ids_are_refused(client):
    response = client.post("/api/services", json={
        "id": "session-60", "name": "Clash", "duration_minutes": 60,
        "price_minor_units": 1,
    })
    assert response.status_code == 400
    assert "already exists" in response.json()["detail"]


def test_exceptions_can_be_set_from_the_calendar(client):
    from datetime import date as _d, timedelta as _td
    day = (_d.today() + _td(days=10)).isoformat()

    response = client.post("/api/exceptions", json={
        "client_id": "alice", "date": day,
        "from_time": "10:00", "to_time": "14:00", "available": True,
    })

    assert response.status_code == 200
    assert any(e["date"] == day
               for e in client.get("/api/state").json()["exceptions"]["alice"])


def test_the_state_carries_what_the_calendar_draws(client):
    state = client.get("/api/state").json()

    assert "today" in state, "the calendar needs an anchor date"
    assert "exceptions" in state
    appointment = state["appointments"][0]
    assert {"service", "price", "origin", "status"} <= set(appointment)
    assert {"completed", "no_show", "cancelled", "moved_by_us"} <= set(state["clients"][0])


def test_the_provider_can_add_a_client(client):
    response = client.post("/api/clients", json={"id": "dana", "name": "Dana"})

    assert response.status_code == 200
    state = client.get("/api/state").json()
    assert "dana" in {c["id"] for c in state["clients"]}
    assert state["weekly"]["dana"], "given the provider's hours by default"


def test_adding_a_client_twice_is_refused_with_a_reason(client):
    client.post("/api/clients", json={"id": "dana", "name": "Dana"})

    response = client.post("/api/clients", json={"id": "dana", "name": "Dana"})

    assert response.status_code == 400
    assert "already a client" in response.json()["detail"]


def test_an_exception_can_be_cleared_through_the_api(client):
    from datetime import date as _d, timedelta as _td
    day = (_d.today() + _td(days=10)).isoformat()
    # Outside alice's usual 09:00-13:00, so it is a real deviation. An
    # exception matching the weekly pattern normalises away to nothing, which
    # is calendar_store storing only what actually differs.
    body = {"client_id": "alice", "date": day,
            "from_time": "15:00", "to_time": "17:00", "available": True}
    client.post("/api/exceptions", json=body)
    assert client.get("/api/state").json()["exceptions"]["alice"]

    client.post("/api/exceptions", json={**body, "clear": True})

    assert client.get("/api/state").json()["exceptions"]["alice"] == []


def test_a_session_is_saved_without_being_asked(client, tmp_path, monkeypatch):
    """Losing an afternoon of hand-built history to a restart is a poor way to
    find out that saving was manual.
    """
    from mock_ui import app as module
    monkeypatch.setattr(module, "SNAPSHOT", tmp_path / "auto.json")

    client.post("/api/clients", json={"id": "dana", "name": "Dana"})

    assert (tmp_path / "auto.json").exists()
    assert "dana" in (tmp_path / "auto.json").read_text()


def test_resetting_clears_the_saved_session_too(client, tmp_path, monkeypatch):
    """Otherwise a reset undoes itself at the next restart."""
    from mock_ui import app as module
    monkeypatch.setattr(module, "SNAPSHOT", tmp_path / "auto.json")
    client.post("/api/clients", json={"id": "dana", "name": "Dana"})
    assert (tmp_path / "auto.json").exists()

    client.post("/api/reset")

    assert not (tmp_path / "auto.json").exists()


def test_the_snapshot_lives_beside_the_package_not_the_shell():
    """A relative default put the session wherever you happened to launch
    from, so a restart could silently start empty.

    Asserts the default rather than the live value: conftest redirects the live
    one at a temporary path so the suite cannot touch a real session.
    """
    from mock_ui import app as module

    assert module.default_snapshot().is_absolute()
    assert module.default_snapshot().parent.name == "mock_ui"
    assert module.SNAPSHOT != module.default_snapshot(), (
        "the suite must never be pointed at the real session file"
    )
