"""The clinician chat.

A conversation between a client and their clinician is clinical — people
write about why they are coming — so the guarantees are about where it does
*not* go: not into the state every screen polls, not into the activity log,
not to anybody else, and never to an AI assistant, not even to read.
"""
import pytest
from fastapi.testclient import TestClient
from scheduling_contracts import Principal, Role, Session

from mock_ui import app as app_module
from mock_ui import persistence
from mock_ui.control import Forbidden, Gate, NotFound, build_command, tools_for

SECRET = "I have not been sleeping since the funeral"


@pytest.fixture
def client():
    c = TestClient(app_module.app)
    c.post("/api/reset?scenario=busy")
    return c


def run(client, who, name, **args):
    return client.post(f"/api/commands/{name}?as={who}", json={"args": args})


def test_a_client_writes_and_the_clinician_replies(client):
    assert run(client, "dan", "message.send", text=SECRET).status_code == 200
    assert run(client, "provider", "message.send", client_id="dan", text="Thank you for telling me").status_code == 200

    convo = run(client, "dan", "message.list").json()["result"]
    assert [(m["author"], m["text"]) for m in convo["messages"]] == [
        ("client", SECRET), ("provider", "Thank you for telling me")]


def test_who_wrote_it_comes_from_the_session(client):
    """A client cannot write as the clinician — there is no field for it."""
    response = run(client, "dan", "message.send", text="hi", author="provider")

    assert response.status_code == 400


def test_a_client_cannot_write_in_or_read_someone_elses_conversation(client):
    run(client, "alice", "message.send", text=SECRET)

    assert run(client, "dan", "message.send", client_id="alice", text="hi").status_code == 404
    assert run(client, "dan", "message.list", client_id="alice").status_code == 404
    mine = run(client, "dan", "message.list").json()["result"]
    assert SECRET not in str(mine)


def test_a_client_cannot_list_every_conversation(client):
    """With no client named, a client's request means their own — never the
    practice's list."""
    run(client, "alice", "message.send", text=SECRET)

    result = run(client, "dan", "message.list").json()["result"]

    assert result["client_id"] == "dan" and SECRET not in str(result)


def test_a_conversation_is_never_in_the_shared_state_or_the_log(client):
    run(client, "dan", "message.send", text=SECRET)

    state = client.get("/api/state").text
    assert SECRET not in state
    assert any("wrote in dan's conversation" in line for line in client.get("/api/state").json()["log"])


def test_no_assistant_may_touch_it_not_even_to_read(client):
    world = app_module.world
    gate = Gate(world)
    world.send_message("dan", "client", SECRET)
    for principal in (Principal(Role.CLIENT, "dan"), Principal(Role.PROVIDER)):
        session = Session(principal, via_assistant=True, conversation_id="x")
        args = {} if principal.role is Role.CLIENT else {"client_id": "Client A"}
        for name in ("message.list", "message.send", "message.mark_read"):
            extra = {"text": "hi"} if name == "message.send" else {}
            with pytest.raises((Forbidden, NotFound)):
                gate.execute(session, build_command(name, {**args, **extra}, session))
        assert not {t["command"] for t in tools_for(session)} & {
            "message.list", "message.send", "message.mark_read"}


def test_reading_marks_only_the_other_sides_messages(client):
    run(client, "dan", "message.send", text="one")
    run(client, "dan", "message.send", text="two")
    summary = run(client, "provider", "message.list").json()["result"]
    assert next(t for t in summary if t["client_id"] == "dan")["unread"] == 2

    run(client, "provider", "message.mark_read", client_id="dan")

    summary = run(client, "provider", "message.list").json()["result"]
    assert next(t for t in summary if t["client_id"] == "dan")["unread"] == 0
    mine = run(client, "dan", "message.list").json()["result"]
    assert all(m["read"] for m in mine["messages"]), "Dan sees they were read"


def test_clients_are_always_told_where_to_get_urgent_help(client):
    convo = run(client, "dan", "message.list").json()["result"]

    assert "995" in convo["settings"]["emergency_note"]
    assert client.post("/api/settings", json={"chat_emergency_note": "  "}).status_code == 400


def test_empty_and_overlong_messages_are_refused(client):
    assert run(client, "dan", "message.send", text="   ").status_code == 400
    assert run(client, "dan", "message.send", text="x" * 4001).status_code == 400


def test_switched_off_means_nobody_can_write(client):
    client.post("/api/settings", json={"chat_enabled": False})

    assert run(client, "dan", "message.send", text="hello").status_code == 400


def test_conversations_survive_a_restart(client, tmp_path):
    world = app_module.world
    world.send_message("dan", "client", SECRET)
    path = tmp_path / "s.json"
    persistence.save(world, path)

    restored = persistence.load(path)

    assert [m.text for m in restored.conversation("dan")][-1] == SECRET
