"""The AI assistant, with Claude replaced by a script.

A stand-in plays the model's part — asking for tools, then answering — and
records every request, so the tests can check what *leaves the practice*:
no client's name or id, nothing from the clinician chat, never a
confirmation token. And what comes back: a change is only ever a card the
person confirms; distress is a button the person presses.
"""
import copy
from datetime import timedelta
import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from scheduling_contracts import Principal, Role

from mock_ui import app as app_module
from mock_ui import assistant as assistant_mod
from mock_ui.assistant import Assistant, ClaudeBrain, LocalBrain, Unavailable
from mock_ui.control import Gate, NotFound
from mock_ui.state import World
from mock_ui import scenarios

DAN = Principal(Role.CLIENT, "dan")
PROVIDER = Principal(Role.PROVIDER)


def reply(text):
    return SimpleNamespace(stop_reason="end_turn", content=[{"type": "text", "text": text}])


def use(name, args, text="Let me do that."):
    return SimpleNamespace(stop_reason="tool_use", content=[
        {"type": "text", "text": text},
        {"type": "tool_use", "id": f"tu_{name}", "name": name, "input": args}])


class Claude:
    """Replays a script; each step may be a response or a function of the
    request that produces one."""

    def __init__(self, *script):
        self.script = list(script)
        self.requests = []
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self.create))

    def create(self, **request):
        self.requests.append(copy.deepcopy(request))
        step = self.script.pop(0) if self.script else reply("Done.")
        return step(request) if callable(step) else step

    def sent(self):
        return json.dumps(self.requests, default=str)

    def last_tool_results(self):
        last = self.requests[-1]["messages"][-1]["content"]
        return [r["content"] for r in last]


@pytest.fixture
def world():
    w = World()
    scenarios.load(w, "busy")
    w.assistant.enabled = True
    w.assistant.opted_in.add("dan")
    return w


def make(world, *script):
    claude = Claude(*script)
    return Assistant(world, Gate(world), client=claude), claude


def dans_first(world):
    from datetime import datetime, timedelta
    now = datetime.now()
    return min((a for a in world.store.appointment_history("dan", now, now + timedelta(days=28))
                if a.status.value == "booked"), key=lambda a: a.range.start)


# -- switched on, deliberately ---------------------------------------------------


def test_nothing_without_a_key(world, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    helper = Assistant(world, Gate(world), brain=ClaudeBrain())

    assert helper.status(DAN)["configured"] is False
    with pytest.raises(Unavailable):
        helper.send(DAN, None, "hello")


def test_off_until_the_practice_offers_it(world):
    world.assistant.enabled = False
    helper, _ = make(world)
    with pytest.raises(Unavailable):
        helper.send(DAN, None, "hello")


def test_a_client_has_to_switch_it_on_themselves(world):
    world.assistant.opted_in.discard("dan")
    helper, claude = make(world)

    with pytest.raises(Unavailable):
        helper.send(DAN, None, "hello")
    assert claude.requests == [], "nothing is sent before they opt in"
    assert "never your name" in helper.status(DAN)["notice"]


# -- changes are cards ------------------------------------------------------------


def test_a_change_comes_back_as_a_card_and_nothing_happens(world):
    appointment = dans_first(world)
    helper, claude = make(world, use("appointment_cancel", {"appointment_id": appointment.id}),
                          reply("I've prepared that — please confirm it on the card."))

    out = helper.send(DAN, None, "Cancel my next appointment please")

    assert world.store.get_appointment(appointment.id).status.value == "booked"
    [card] = helper.gate.pending(DAN)
    assert card.summary.startswith("Cancel your")
    assert out["messages"][-1]["text"].startswith("I've prepared that")
    told = claude.last_tool_results()[0]
    assert "Prepared, not done" in told and card.token not in told, "the model never holds a token"


def test_what_the_model_was_told_is_true_when_refused(world):
    """Someone else's appointment is 'no such appointment' to the model too."""
    from datetime import datetime, timedelta
    now = datetime.now()
    alices = next(a for a in world.store.appointment_history("alice", now, now + timedelta(days=28))
                  if a.status.value == "booked")
    helper, claude = make(world, use("appointment_cancel", {"appointment_id": alices.id}))

    helper.send(DAN, None, "cancel appointment " + str(alices.id))

    assert claude.last_tool_results() == ["no such appointment"]
    assert world.store.get_appointment(alices.id).status.value == "booked"


def test_a_clients_assistant_is_offered_only_the_clients_tools(world):
    helper, claude = make(world)
    helper.send(DAN, None, "hi")

    names = {t["name"] for t in claude.requests[0]["tools"]}
    assert "appointment_request_reschedule" in names and "offer_clinician_handoff" in names
    assert not names & {"plan_propose", "plan_preview", "client_set_limits", "message_list",
                        "message_send", "charge_confirm_fee", "payment_record"}
    assert all("client_id" not in t["input_schema"]["properties"] for t in claude.requests[0]["tools"])


# -- what leaves the practice -------------------------------------------------------


def everyone(world):
    out = set()
    for c in world.clients.values():
        out |= {c.id, c.name}
    return out


def test_a_clients_conversation_never_carries_anybodys_identity(world):
    appointment = dans_first(world)
    helper, claude = make(world,
                          use("appointment_read", {"window": {"start": "2026-01-01T00:00", "end": "2027-01-01T00:00"}}),
                          use("request_list", {}),
                          use("approval_list", {}),
                          use("appointment_request_reschedule", {"appointment_id": appointment.id}),
                          reply("Done."))
    world.send_message("dan", "client", "PRIVATE: about my sleep")

    helper.send(DAN, None, "What do I have coming up, and can I move the first one?")

    sent = claude.sent()
    assert not {i for i in everyone(world) if f'"{i}"' in sent or f" {i} " in sent}
    assert "PRIVATE" not in sent, "nothing from the clinician chat"


def test_the_provider_types_names_but_claude_sees_pseudonyms(world):
    helper, claude = make(world,
                          lambda req: reply(f"{_first_alias(req)} has one session on Monday."))

    out = helper.send(PROVIDER, None, "What does Dan have next week? And alice?")

    sent = claude.sent()
    assert "Dan" not in sent and "alice" not in sent.lower()
    assert '"Client ' in sent or "Client " in sent
    assert out["messages"][-1]["text"].startswith("Dan"), "the provider reads real names"


def test_the_providers_card_text_is_pseudonymised(world):
    helper, claude = make(world, reply("noted"))
    from scheduling_contracts import Session
    session = Session(PROVIDER, via_assistant=True, conversation_id="c")
    out = helper._tool(session, assistant_mod.Conversation(id="c", principal=PROVIDER, created=None),
                       {"appointment_cancel": "appointment.cancel"},
                       assistant_mod.Call(id="1", name="appointment_cancel",
                                          args={"appointment_id": dans_first(world).id}))
    text, is_error = out
    assert "Dan" in world.clients["dan"].name and "Dan" not in text, text


def _first_alias(request):
    import re
    return re.search(r"Client [A-Z]{1,2}", request["messages"][0]["content"]).group(0)


def test_the_provider_can_compare_settings_freely(world):
    helper, claude = make(world, use("plan_preview", {"alpha": 0.9}), reply("At 0.9 …"))

    helper.send(PROVIDER, None, "What if I favour a packed diary?")

    result = json.loads(claude.last_tool_results()[0])
    assert result["dry_run"] is True and "metrics" in result
    assert not {i for i in everyone(world) if f'"{i}"' in claude.last_tool_results()[0]}


# -- distress: a button, pressed by the person -----------------------------------------


def test_distress_offers_a_handoff_and_sends_nothing_by_itself(world):
    helper, claude = make(world, use("offer_clinician_handoff", {}),
                          reply("I'm sorry you're feeling this way. Call 995 or 1767 now if you need help."))

    out = helper.send(DAN, None, "I can't cope anymore")

    assert out["handoff_offered"] is True and "995" in out["emergency_note"]
    assert world.conversation("dan") == [], "nothing reaches the clinician until the person says so"


def test_the_person_sends_it_and_only_their_own_words_go(world):
    helper, claude = make(world, reply("Let's look at your bookings."),
                          use("offer_clinician_handoff", {}), reply("I'm sorry. Here is help."))
    first = helper.send(DAN, None, "Hi, can I move my Tuesday?")
    helper.send(DAN, first["conversation_id"], "Actually I really need to talk to someone")

    out = helper.handoff(DAN, first["conversation_id"])

    [message] = world.conversation("dan")
    assert message.author == "client"
    assert "Hi, can I move my Tuesday?" in message.text
    assert "I really need to talk to someone" in message.text
    assert "Here is help" not in message.text and "Let's look" not in message.text
    assert out["handoff_sent"] is True
    assert message.handoff is True, "flagged for the clinician, by the server"


def test_a_client_cannot_make_an_ordinary_message_look_handed_off():
    client = TestClient(app_module.app)
    client.post("/api/reset?scenario=busy")

    response = client.post("/api/commands/message.send?as=dan",
                           json={"args": {"text": "hi", "handoff": True}})

    assert response.status_code == 400


def test_a_client_can_turn_it_off_again():
    client = TestClient(app_module.app)
    client.post("/api/reset?scenario=busy")
    client.post("/api/assistant/opt-in?as=dan", json={"on": True})
    client.post("/api/assistant/opt-in?as=dan", json={"on": False})

    assert client.get("/api/assistant/status?as=dan").json()["opted_in"] is False


def test_the_person_can_always_send_it_themselves(world):
    """Not only when the model offers: a model noticing distress is not
    reliable enough to be the only way to reach a person."""
    helper, _ = make(world, reply("Sure."))
    convo = helper.send(DAN, None, "please pass this on")

    out = helper.handoff(DAN, convo["conversation_id"])

    assert out["handoff_sent"] and world.conversation("dan")


def test_the_provider_has_nothing_to_hand_off(world):
    helper, _ = make(world, reply("Sure."))
    convo = helper.send(PROVIDER, None, "hello")

    from mock_ui.control import Forbidden
    with pytest.raises(Forbidden):
        helper.handoff(PROVIDER, convo["conversation_id"])


@pytest.mark.parametrize("text", ["I can't cope anymore", "I've been feeling really low",
                                  "I want to talk to someone", "thinking about suicide"])
def test_distress_shows_the_handoff_even_if_the_model_misses_it(world, text):
    helper, _ = make(world, reply("Let's look at your bookings."))   # the model did not notice

    out = helper.send(DAN, None, text)

    assert out["handoff_offered"] is True and "995" in out["emergency_note"]


def test_ordinary_scheduling_does_not_trigger_it(world):
    helper, _ = make(world, reply("Sure."))

    assert helper.send(DAN, None, "Can I move Tuesday to Thursday?")["handoff_offered"] is False


def test_a_client_is_given_their_own_diary_with_each_message(world):
    """Real appointment numbers up front, so nothing is guessed — and still
    nothing that identifies anybody."""
    appointment = dans_first(world)
    helper, claude = make(world, reply("ok"))

    helper.send(DAN, None, "when is my next one?")

    note = claude.requests[0]["messages"][1]["content"]
    assert f"appointment_id {appointment.id}" in note
    assert f"{appointment.range.start:%a %d %b %H:%M}" in note
    assert not {i for i in everyone(world) if i in note.split()}


# -- conversations ----------------------------------------------------------------------


def test_a_conversation_is_only_its_owners(world):
    world.assistant.opted_in.add("alice")
    helper, _ = make(world, reply("Hi"))
    convo = helper.send(DAN, None, "hello")

    with pytest.raises(NotFound):
        helper.send(Principal(Role.CLIENT, "alice"), convo["conversation_id"], "hi")
    with pytest.raises(NotFound):
        helper.send(PROVIDER, convo["conversation_id"], "hi")


def test_history_sent_to_the_model_only_ever_grows(world):
    """Thinking blocks are bound to the conversation: never edit what was sent."""
    helper, claude = make(world, reply("one"), reply("two"))
    convo = helper.send(DAN, None, "first")
    helper.send(DAN, convo["conversation_id"], "second")

    first, second = claude.requests
    assert second["messages"][: len(first["messages"])] == first["messages"]


def test_the_current_time_is_a_note_not_part_of_the_cached_prompt(world):
    helper, claude = make(world, reply("one"))
    helper.send(DAN, None, "hello")

    request = claude.requests[0]
    assert "Now:" not in request["system"]
    assert request["messages"][1]["role"] == "system" and request["messages"][1]["content"].startswith("Now:")


def test_a_refusal_is_answered_politely(world):
    helper, _ = make(world, SimpleNamespace(stop_reason="refusal", content=[]))

    out = helper.send(DAN, None, "something")

    assert "screens" in out["messages"][-1]["text"]


def test_a_runaway_loop_is_stopped(world):
    helper, claude = make(world, *[use("service_list", {})] * 20)

    out = helper.send(DAN, None, "list services forever")

    assert len(claude.requests) == assistant_mod.MAX_STEPS
    assert "too many steps" in out["messages"][-1]["text"]


def test_requests_use_the_chosen_model_with_fallbacks(world):
    helper, claude = make(world, reply("hi"))
    helper.send(DAN, None, "hello")

    request = claude.requests[0]
    assert request["model"] == assistant_mod.MODEL
    assert request["fallbacks"] == "default"
    assert "server-side-fallback-2026-07-01" in request["betas"]
    assert request["thinking"] == {"type": "adaptive"}


# -- over HTTP ------------------------------------------------------------------------------


def test_the_http_side(monkeypatch):
    client = TestClient(app_module.app)
    client.post("/api/reset?scenario=busy")
    claude = Claude(reply("Hello!"))
    app_module.assistant().brain = ClaudeBrain(claude)

    assert client.post("/api/assistant/send?as=dan", json={"text": "hi"}).status_code == 503
    client.post("/api/settings", json={"assistant_enabled": True})
    assert client.post("/api/assistant/send?as=dan", json={"text": "hi"}).status_code == 503
    client.post("/api/assistant/opt-in?as=dan", json={"on": True})

    out = client.post("/api/assistant/send?as=dan", json={"text": "hi"})
    assert out.status_code == 200 and out.json()["messages"][-1]["text"] == "Hello!"
    assert client.get("/api/assistant/status?as=dan").json()["opted_in"] is True
    assert client.post("/api/assistant/opt-in?as=provider", json={"on": True}).status_code == 400



# -- the free model on this computer ------------------------------------------------


class Ollama:
    """Plays Ollama's /api/chat: a script of assistant messages, recording
    every request so the tests can see what the local model was sent."""

    def __init__(self, *script, models=("qwen3:8b",)):
        self.script, self.requests, self.models = list(script), [], models

    def post(self, path, body):
        assert path == "/api/chat"
        self.requests.append(copy.deepcopy(body))
        return {"message": self.script.pop(0) if self.script else {"role": "assistant", "content": "Done."},
                "done_reason": "stop"}

    def get(self, path):
        return {"models": [{"name": m} for m in self.models]}


def local(world, *script, models=("qwen3:8b",)):
    ollama = Ollama(*script, models=models)
    brain = LocalBrain(post=ollama.post, get=ollama.get)
    return Assistant(world, Gate(world), brain=brain), ollama


def test_the_local_model_is_ready_only_once_downloaded(world):
    helper, _ = local(world, models=())
    assert helper.status(DAN)["configured"] is False

    helper, _ = local(world)
    status = helper.status(DAN)
    assert status["configured"] is True and status["leaves_practice"] is False
    assert "nothing you type to it leaves the practice" in status["notice"]


def test_the_local_model_goes_through_the_same_gate(world):
    appointment = dans_first(world)
    helper, ollama = local(world,
                           {"role": "assistant", "content": "",
                            "tool_calls": [{"function": {"name": "appointment_cancel",
                                                         "arguments": {"appointment_id": appointment.id}}}]},
                           {"role": "assistant", "content": "<think>hmm</think>Prepared — please confirm the card."})

    out = helper.send(DAN, None, "cancel my next one")

    assert world.store.get_appointment(appointment.id).status.value == "booked"
    [card] = helper.gate.pending(DAN)
    text = out["messages"][-1]["text"]
    assert text.startswith("Prepared — please confirm the card.") and "<think" not in text, \
        "thinking tags stripped"
    assert card.summary in text, "the card's own words follow"
    told = ollama.requests[-1]["messages"][-1]
    assert told["role"] == "tool" and "Prepared, not done" in told["content"]
    assert card.token not in json.dumps(ollama.requests)


def test_the_local_model_is_told_nobodys_name_either(world):
    helper, ollama = local(world,
                           {"role": "assistant", "content": "",
                            "tool_calls": [{"function": {"name": "request_list", "arguments": "{}"}}]},
                           {"role": "assistant", "content": "Two requests are waiting."})

    helper.send(PROVIDER, None, "What is Dan waiting for?")

    sent = json.dumps(ollama.requests)
    assert not {i for i in everyone(world) if f'"{i}"' in sent or f" {i}" in sent}
    assert "Client " in sent


def test_a_client_on_the_local_model_still_gets_only_client_tools(world):
    helper, ollama = local(world)
    helper.send(DAN, None, "hi")

    names = {t["function"]["name"] for t in ollama.requests[0]["tools"]}
    assert "offer_clinician_handoff" in names and "plan_preview" not in names
    assert all("client_id" not in t["function"]["parameters"]["properties"]
               for t in ollama.requests[0]["tools"])


def test_ollama_down_is_said_plainly(world):
    def down(path, body):
        raise ConnectionError("refused")
    helper = Assistant(world, Gate(world), brain=LocalBrain(post=down, get=Ollama().get))

    with pytest.raises(assistant_mod.AssistantError, match="Ollama"):
        helper.send(DAN, None, "hi")


def test_later_distress_in_the_same_conversation_can_be_passed_on_too(world):
    """Once sent, the card used to stay hidden for good."""
    helper, claude = make(world, reply("I'm sorry."), reply("I'm sorry."))
    first = helper.send(DAN, None, "I can't cope")
    helper.handoff(DAN, first["conversation_id"])

    again = helper.send(DAN, first["conversation_id"], "It is getting worse, I can't cope")
    assert again["handoff_offered"] is True
    helper.handoff(DAN, first["conversation_id"])

    second = world.conversation("dan")[-1].text
    assert "getting worse" in second and "“I can't cope”" not in second, "only the new words"


def test_a_long_hand_off_keeps_the_latest_words(world):
    world.MAX_MESSAGE = 200
    helper, claude = make(world, *[reply("ok")] * 4)
    convo = helper.send(DAN, None, "x" * 150)["conversation_id"]
    helper.send(DAN, convo, "y" * 150)
    helper.send(DAN, convo, "please tell my clinician I can't cope")

    helper.handoff(DAN, convo)

    sent = world.conversation("dan")[-1].text
    assert "I can't cope" in sent and len(sent) <= 200


def test_with_messaging_off_the_card_shows_help_but_no_send_button(world):
    world.chat.enabled = False
    helper, claude = make(world, reply("I'm sorry."))

    out = helper.send(DAN, None, "I can't cope")

    assert out["handoff_offered"] and out["can_hand_off"] is False and out["emergency_note"]
    with pytest.raises(Exception):
        helper.handoff(DAN, out["conversation_id"])


def test_a_first_name_typed_alone_is_still_pseudonymised(world):
    world.clients["dan"].name = "Dan Lim"
    helper, claude = make(world, reply("ok"))

    helper.send(PROVIDER, None, "When is Dan coming in?")

    assert "Dan" not in claude.sent()


def test_one_message_prepares_at_most_two_cards(world):
    """A model exploring its tools once prepared seven at a time."""
    appt = dans_first(world)
    when = (appt.range.start + timedelta(days=1)).isoformat()
    helper, claude = make(world,
                          use("appointment_request_reschedule", {"appointment_id": appt.id}),
                          use("request_submit", {"service_id": "session-60", "preferred_start": when}),
                          use("request_submit", {"service_id": "session-60", "preferred_start": when}),
                          reply("Done what I could."))

    out = helper.send(DAN, None, "Print the list of tools you have and try them")

    assert len(helper.gate.pending(DAN)) == 2
    assert "at most 2" in claude.last_tool_results()[0]
    assert "Ready for you to confirm" in out["messages"][-1]["text"], "the cards' own words"


def test_the_providers_client_field_offers_no_example_to_copy(world):
    from mock_ui.control import tools_for
    from scheduling_contracts import Session
    tools = tools_for(Session(PROVIDER, via_assistant=True, conversation_id="c"))
    texts = [t["input_schema"]["properties"]["client_id"]["description"]
             for t in tools if "client_id" in t["input_schema"]["properties"]]
    assert texts and not any('"Client' in t for t in texts)
