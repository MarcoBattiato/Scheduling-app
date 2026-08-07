"""The HTTP surface the browser talks to. Thin by design, so these are thin."""
from datetime import date, datetime, time, timedelta

import pytest
from fastapi.testclient import TestClient

from mock_ui import app as app_module


@pytest.fixture
def client():
    c = TestClient(app_module.app)
    c.post("/api/reset")          # fresh, seeded world for every test
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


def test_submitting_a_request_schedules_it_in_one_call(client):
    response = client.post("/api/requests", json={
        "client_id": "alice", "service_id": "session-60",
        "windows": [{"from": at(0, 9), "to": at(0, 17)}],
    })

    assert response.status_code == 200
    body = response.json()
    assert body["outcome"]["placed"] == 1

    state = client.get("/api/state").json()
    assert any(r["status"] == "placed" for r in state["requests"])


def test_every_tab_sees_the_same_world(client):
    """Two clients, one server: what one does the other must see. This is the
    only reason the mock is a server rather than a static page.
    """
    client.post("/api/requests", json={
        "client_id": "alice", "service_id": "session-60",
        "windows": [{"from": at(0, 9), "to": at(0, 17)}],
    })

    other_tab = TestClient(app_module.app)
    seen = other_tab.get("/api/state").json()

    assert any(a["client_id"] == "alice" and a["status"] == "booked"
               for a in seen["appointments"])


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


@pytest.mark.parametrize("payload,detail", [
    ({"client_id": "alice", "service_id": "nonexistent",
      "windows": [{"from": at(0, 9), "to": at(0, 17)}]}, "no service"),
    ({"client_id": "alice", "service_id": "session-60", "windows": []}, "window"),
])
def test_nonsense_requests_are_refused_with_a_reason(client, payload, detail):
    response = client.post("/api/requests", json=payload)

    assert response.status_code == 400
    assert detail in response.json()["detail"]


def test_unknown_ids_are_not_found_rather_than_crashing(client):
    assert client.post("/api/appointments/99999/cancel").status_code == 404
    assert client.post("/api/requests/99999/withdraw").status_code == 404
    assert client.post("/api/approvals/99999", json={"accept": True}).status_code == 404


def test_a_session_survives_being_saved_and_reloaded(client, tmp_path):
    from mock_ui import persistence

    client.post("/api/requests", json={
        "client_id": "alice", "service_id": "session-60",
        "windows": [{"from": at(0, 9), "to": at(0, 17)}],
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
        "windows": [{"from": at(0, 9), "to": at(0, 17)}],
    })
    assert booked.status_code == 200, "existing ids still resolve for rescheduling"


def test_duplicate_service_ids_are_refused(client):
    response = client.post("/api/services", json={
        "id": "session-60", "name": "Clash", "duration_minutes": 60,
        "price_minor_units": 1,
    })
    assert response.status_code == 400
    assert "already exists" in response.json()["detail"]
