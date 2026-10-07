"""The AI assistant: a person talking to the app instead of tapping it.

The model — Claude, or a free model running on the practice's own computer
(`ASSISTANT_PROVIDER`) — is given tools, and every tool is a command that
goes through the gate
(control.py) as the person, via the assistant channel — so the model can
never do more than the person could, every change comes back as a draft the
person confirms on their own screen, and the model is never handed a
confirmation token to press for them.

What leaves the practice is kept to scheduling (INTEGRATIONS_PLAN.md Part 3):

- A client's assistant carries no identifier: its tools never ask who it is
  acting for, and what they return has no names or ids in it.
- The provider's assistant sees clients only as per-conversation pseudonyms.
  Names the provider *types* are swapped for those pseudonyms before anything
  is sent, and swapped back in the replies the provider reads.
- Nothing from the clinician chat: every `message.*` command is forbidden to
  an assistant, reads included.

Distress is handled by hand-off, not counselling. The model can only *offer*
to pass the conversation to the clinician (a tool that shows the person a
button); sending happens when the person taps it, and what is sent is their
own words, never the model's.

Conversations are kept in memory only, and are gone on restart: keeping them
no longer than needed is part of the privacy design.
"""
from __future__ import annotations

import json
import os
import re
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Dict, List, Optional

from scheduling_contracts import Principal, Role, Session

from . import control

# Which model answers: "claude" (Anthropic's API, needs ANTHROPIC_API_KEY) or
# "local" (a free model on this computer, through Ollama). Unset: Claude when
# a key is present, otherwise the local model.
PROVIDER = os.environ.get("ASSISTANT_PROVIDER", "auto")
MODEL = os.environ.get("ASSISTANT_MODEL", "claude-opus-5-5")
LOCAL_MODEL = os.environ.get("ASSISTANT_LOCAL_MODEL", "qwen3:8b")
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434")
# Opus 5.5 defaults to medium; set explicitly so a model change does not
# silently change how hard it thinks about someone's diary.
EFFORT = os.environ.get("ASSISTANT_EFFORT", "medium")
MAX_STEPS = 8                  # model round-trips per message, tool calls included
MAX_TEXT = 2000
MAX_CONVERSATIONS = 200

HANDOFF_TOOL = {
    "name": "offer_clinician_handoff",
    "description": (
        "Show the person a button to send this conversation to their clinician, "
        "together with where to get urgent help. Use it when the person seems "
        "distressed or at risk, or asks to talk to a person or their clinician. "
        "It sends nothing by itself: the person decides."),
    "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
}


# Phrases that put the hand-off card in front of the person whatever the
# model does. A model noticing distress is not reliable enough to be the only
# way to reach a person (INTEGRATIONS_PLAN.md Part 3); this is a floor, not a
# classifier, and the always-visible "Send this to my clinician" is another.
DISTRESS = re.compile(
    r"suicid|kill (my ?self|me)|end (it all|my life)|don'?t want to (live|be here|go on)|"
    r"self[- ]?harm|hurt(ing)? myself|can'?t cope|cannot cope|not coping|can'?t go on|"
    r"hopeless|feel(ing)? (really |so |very )?(low|down|awful|terrible)|panic attack|"
    r"in crisis|emergency|(talk|speak) to (someone|somebody|a person|a human|my clinician|my therapist)",
    re.IGNORECASE)


class Unavailable(Exception):
    """No API key, the practice has not switched it on, or the client has not."""


class AssistantError(Exception):
    pass


@dataclass
class Conversation:
    id: str
    principal: Principal
    created: datetime
    messages: List[dict] = field(default_factory=list)   # sent to the model, append-only
    visible: List[dict] = field(default_factory=list)    # what the person sees
    typed: List[str] = field(default_factory=list)       # the person's own words, for a hand-off
    handoff_offered: bool = False
    handoff_sent: bool = False


def _client_prompt(world) -> str:
    chat = world.chat
    return f"""You are the scheduling assistant for {world.practice_name}. You are talking with one of its clients, about their own appointments only: booking, moving or cancelling them, repeating bookings, and the hours they are free. You act only through your tools.

You do not know the person's name and do not need it; speak to them as "you".

Rules:
- Every change you make is only prepared. The person sees a card in the app and must confirm it there. Say this plainly. Never say something is done unless a tool result says it is done.
- You are not a clinician. Do not give medical, psychological or any other advice, and do not discuss why the person is seeing the practice.
- If the person seems distressed or at risk, or asks to speak to someone or to their clinician: stop helping with scheduling, respond briefly and kindly, tell them: "{chat.emergency_note}", and call offer_clinician_handoff so they can choose to send this conversation to their clinician. Do not promise when it will be read; say: "{chat.reply_expectation}"
- All times are local to Singapore. The current date and time, and the person's own upcoming appointments, waiting requests and open questions, are given with each message — use those numbers rather than guessing.
- Keep replies short and plain: they are read on a phone.
- When you use a tool, you may say a brief sentence first. If no tool can do what was asked, say so instead of guessing.
- Do not include internal or system XML tags in your response."""


def _provider_prompt(world) -> str:
    return f"""You are the scheduling assistant for the clinician who runs {world.practice_name}. You help them review and run their diary through your tools.

Clients appear to you only as pseudonyms such as "Client B". Use exactly those; you are never told real names, and the app shows the clinician the real ones.

How the scheduler works:
- Requests wait in a queue until a schedule is made. plan_preview shows what a schedule would look like at given settings and changes nothing, so use it freely to compare options. alpha runs from 0 (book people as close as possible to the times they asked for) to 1 (leave no unusable gaps in the diary). max_displacements is how many already-booked clients may be asked to move.
- Making a real proposal, approving it, confirming answers, marking attendance and similar changes are only prepared: the clinician confirms each on a card in the app. Say so. Never say something is done unless a tool result says it is done.
- You have no access to conversations between the clinician and clients, or to anything about money.

Keep replies short and concrete; they are read on a phone. All times are local to Singapore; the current date and time are given with each message.
When you use a tool, you may say a brief sentence first. If no tool can do what was asked, say so instead of guessing.
Do not include internal or system XML tags in your response."""


def _as_dict(block):
    if isinstance(block, dict):
        return block
    return block.model_dump()


def _get(block, key):
    return block.get(key) if isinstance(block, dict) else getattr(block, key, None)


# -- the model, behind one small interface -----------------------------------
#
# Everything that matters for safety — the gate's tools, the cards, the
# pseudonyms, the hand-off — lives in `Assistant` and is the same whichever
# model answers. A brain only knows how to talk to its model: how to add the
# person's message, take one step, and report tool results back.


@dataclass
class Call:
    id: str
    name: str
    args: dict


@dataclass
class Step:
    text: str
    calls: List[Call]
    refused: bool = False


class ClaudeBrain:
    """Anthropic's API. Leaves the practice: covered by the notice and the
    data processing terms in INTEGRATIONS_PLAN.md Part 3."""
    name = "claude"
    leaves_practice = True

    def __init__(self, client=None) -> None:
        self._client = client

    @property
    def model(self) -> str:
        return MODEL

    def configured(self) -> bool:
        return self._client is not None or bool(os.environ.get("ANTHROPIC_API_KEY"))

    def client(self):
        if self._client is None:
            import anthropic
            self._client = anthropic.Anthropic()
        return self._client

    def add_user(self, messages: list, text: str, now: str) -> None:
        messages.append({"role": "user", "content": text})
        # Volatile facts go in a system note after the message rather than in
        # the system prompt, so the cached prefix survives from turn to turn.
        messages.append({"role": "system", "content": now})

    def step(self, system: str, tools: list, messages: list) -> Step:
        try:
            response = self.client().beta.messages.create(
                model=MODEL,
                max_tokens=8000,
                system=system,
                tools=tools,
                messages=messages,
                thinking={"type": "adaptive"},
                output_config={"effort": EFFORT},
                cache_control={"type": "ephemeral"},
                # On a safety decline, rerun on Anthropic's recommended
                # fallback model instead of failing the person.
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
            )
        except Exception as exc:                          # network, rate limit, outage
            raise AssistantError("The assistant could not be reached. Try again shortly.") from exc
        if response.stop_reason == "refusal":
            return Step("", [], refused=True)
        content = [_as_dict(b) for b in response.content]
        messages.append({"role": "assistant", "content": content})       # unchanged, in full
        text = "".join(_get(b, "text") or "" for b in content if _get(b, "type") == "text")
        calls = ([Call(_get(b, "id"), _get(b, "name"), _get(b, "input") or {})
                  for b in content if _get(b, "type") == "tool_use"]
                 if response.stop_reason == "tool_use" else [])
        return Step(text, calls)

    def add_results(self, messages: list, results) -> None:
        blocks = []
        for call, text, error in results:
            block = {"type": "tool_result", "tool_use_id": call.id, "content": text}
            if error:
                block["is_error"] = True
            blocks.append(block)
        messages.append({"role": "user", "content": blocks})


_THINK = re.compile(r"<think>.*?</think>", re.DOTALL)


class LocalBrain:
    """A free, open model on this computer, through Ollama's own API.

    Nothing leaves the machine. Smaller models make more mistakes — misread a
    day, pick the wrong booking — which the design already absorbs: every
    change is still a card the person reads and confirms.
    """
    name = "local"
    leaves_practice = False

    def __init__(self, post=None, get=None) -> None:
        self._post, self._get = post, get
        self._checked: Optional[tuple] = None

    @property
    def model(self) -> str:
        return LOCAL_MODEL

    def _http(self):
        import httpx
        return httpx

    def configured(self) -> bool:
        """Ollama running and the model downloaded — checked at most every
        ten seconds, since the screens ask on every poll."""
        now = datetime.now().timestamp()
        if self._checked and now - self._checked[0] < 10:
            return self._checked[1]
        try:
            tags = (self._get or (lambda path: self._http().get(OLLAMA_URL + path, timeout=2).json()))("/api/tags")
            ok = any(m.get("name") == LOCAL_MODEL or m.get("model") == LOCAL_MODEL
                     for m in tags.get("models", []))
        except Exception:
            ok = False
        self._checked = (now, ok)
        return ok

    def add_user(self, messages: list, text: str, now: str) -> None:
        messages.append({"role": "user", "content": f"[{now}]\n{text}"})

    def step(self, system: str, tools: list, messages: list) -> Step:
        payload = {
            "model": LOCAL_MODEL,
            "messages": [{"role": "system", "content": system}] + messages,
            "tools": [{"type": "function", "function": {
                "name": t["name"], "description": t["description"],
                "parameters": t["input_schema"]}} for t in tools],
            "stream": False,
            "think": False,                 # faster, and tool use does not need it
            "options": {"temperature": 0.2},
        }
        try:
            post = self._post or (lambda path, body: self._http().post(
                OLLAMA_URL + path, json=body, timeout=180).json())
            out = post("/api/chat", payload)
        except Exception as exc:
            raise AssistantError("The assistant on this computer is not answering. "
                                 "Is Ollama running?") from exc
        message = out.get("message") or {}
        text = _THINK.sub("", message.get("content") or "").strip()
        calls = []
        for i, call in enumerate(message.get("tool_calls") or []):
            fn = call.get("function", {})
            args = fn.get("arguments") or {}
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except ValueError:
                    args = {}
            calls.append(Call(f"call_{len(messages)}_{i}", fn.get("name", ""), args))
        messages.append({"role": "assistant", "content": message.get("content") or "",
                         **({"tool_calls": message["tool_calls"]} if message.get("tool_calls") else {})})
        return Step(text, calls)

    def add_results(self, messages: list, results) -> None:
        for call, text, error in results:
            messages.append({"role": "tool", "tool_name": call.name,
                             "content": ("Error: " + text) if error else text})


def choose_brain(client=None):
    if client is not None:
        return ClaudeBrain(client)
    if PROVIDER == "claude":
        return ClaudeBrain()
    if PROVIDER == "local":
        return LocalBrain()
    return ClaudeBrain() if os.environ.get("ANTHROPIC_API_KEY") else LocalBrain()


class Assistant:
    def __init__(self, world, gate: control.Gate, client=None, brain=None) -> None:
        self.world = world
        self.gate = gate
        self.brain = brain or choose_brain(client)
        self.conversations: Dict[str, Conversation] = {}

    # -- is it on? ------------------------------------------------------------

    def configured(self) -> bool:
        return self.brain.configured()

    def status(self, principal: Principal) -> dict:
        settings = self.world.assistant
        opted = (principal.role is Role.PROVIDER
                 or principal.client_id in settings.opted_in)
        return {"configured": self.configured(), "offered": settings.enabled,
                "opted_in": opted, "model": self.brain.model, "provider": self.brain.name,
                "leaves_practice": self.brain.leaves_practice,
                "notice": self.notice(principal),
                # What clients are told, so the provider can see it too.
                "client_notice": self.notice(Principal(Role.CLIENT, "any"))}

    def notice(self, principal: Principal) -> str:
        if not self.brain.leaves_practice:
            if principal.role is Role.PROVIDER:
                return ("The assistant runs on the practice's own computer: nothing typed to it "
                        "leaves the practice. Clients still appear to it as \"Client A\", "
                        "\"Client B\". It never sees conversations with clients or money. It is a "
                        "smaller model than the big online ones, so check each card before confirming.")
            return ("The assistant runs on your practice's own computer: nothing you type to it "
                    "leaves the practice. It sees your appointment times and what you type — never "
                    "your name, and never your messages to your clinician. It can only prepare "
                    "changes; you confirm each one, so read each card. It is not for anything urgent.")
        if principal.role is Role.PROVIDER:
            return ("The assistant uses Claude, an AI made by Anthropic, which runs outside "
                    "Singapore. It sees appointment times and what you type. Clients appear "
                    "to it as \"Client A\", \"Client B\"; their names are swapped out before "
                    "anything is sent. It never sees conversations with clients or money.")
        return ("The assistant uses Claude, an AI made by Anthropic, which runs outside "
                "Singapore. It sees your appointment times and what you type to it — never "
                "your name, and never your messages to your clinician. It can only prepare "
                "changes; you confirm each one. It is not for anything urgent.")

    def _require(self, principal: Principal) -> None:
        if not self.configured():
            raise Unavailable("The assistant is not set up on this server yet.")
        if not self.world.assistant.enabled:
            raise Unavailable("The practice has not switched the assistant on.")
        if principal.role is Role.CLIENT and principal.client_id not in self.world.assistant.opted_in:
            raise Unavailable("Switch the assistant on first.")

    # -- conversations -----------------------------------------------------------

    def conversation(self, principal: Principal, conversation_id: Optional[str]) -> Conversation:
        if conversation_id:
            convo = self.conversations.get(conversation_id)
            if convo is None or convo.principal != principal:
                raise control.NotFound("no such conversation")
            return convo
        convo = Conversation(id=secrets.token_urlsafe(12), principal=principal,
                             created=datetime.now())
        self.conversations[convo.id] = convo
        while len(self.conversations) > MAX_CONVERSATIONS:      # oldest go first
            del self.conversations[next(iter(self.conversations))]
        return convo

    # -- one message ----------------------------------------------------------------

    def send(self, principal: Principal, conversation_id: Optional[str], text: str) -> dict:
        self._require(principal)
        text = (text or "").strip()
        if not text:
            raise control.Invalid("say something first")
        if len(text) > MAX_TEXT:
            raise control.Invalid(f"at most {MAX_TEXT} characters at a time")
        convo = self.conversation(principal, conversation_id)
        session = Session(principal, via_assistant=True, conversation_id=convo.id)
        provider = principal.role is Role.PROVIDER

        convo.typed.append(text)
        convo.visible.append({"role": "user", "text": text})
        if not provider and DISTRESS.search(text):
            convo.handoff_offered = True          # shown whatever the model does
        outbound = self._pseudonymise(session, text) if provider else text
        note = f"Now: {datetime.now():%A %d %B %Y, %H:%M} (Singapore)."
        if not provider:
            note += "\n" + self._client_context(session)
        self.brain.add_user(convo.messages, outbound, note)

        tools = control.tools_for(session)
        by_name = {t["name"]: t["command"] for t in tools}
        api_tools = [{k: t[k] for k in ("name", "description", "input_schema")} for t in tools]
        if not provider:
            api_tools.append(HANDOFF_TOOL)
        system = _provider_prompt(self.world) if provider else _client_prompt(self.world)

        reply = ""
        for _ in range(MAX_STEPS):
            step = self.brain.step(system, api_tools, convo.messages)
            if step.refused:
                reply = "I can't help with that here. You can still do everything from the app's screens."
                break
            if not step.calls:
                reply = step.text
                break
            self.brain.add_results(convo.messages,
                                   [(call, *self._tool(session, convo, by_name, call))
                                    for call in step.calls])
        else:
            reply = "That took too many steps. Could you ask in a simpler way?"

        if provider:
            reply = self._restore_names(session, reply)
        reply = reply.strip() or "Done."
        convo.visible.append({"role": "assistant", "text": reply})
        return self.view(convo)

    def _client_context(self, session: Session) -> str:
        """The person's own diary, given with every message: upcoming
        appointments with their real numbers, waiting requests, and open
        questions. Fetched through the gate as the assistant, so it is exactly
        — and only — what the tools would return. Without it, a small model
        guessed date ranges and invented appointment numbers."""
        def read(name, args):
            try:
                return self.gate.execute(session, control.build_command(name, args, session)).result
            except control.GateError:
                return []
        now = datetime.now()
        window = {"start": now.isoformat(timespec="minutes"),
                  "end": (now.replace(hour=0, minute=0) + timedelta(days=29)).isoformat(timespec="minutes")}
        fmt = lambda iso: datetime.fromisoformat(iso).strftime("%a %d %b %H:%M")
        lines = ["Your upcoming appointments:"]
        lines += [f"- appointment_id {a['appointment_id']}: {fmt(a['start'])}–{a['end'][11:16]} ({a['service_id']})"
                  for a in read("appointment.read", {"window": window})] or ["- none"]
        lines.append("Your requests waiting for a time:")
        lines += [f"- request_id {r['request_id']}: {r['service_id']}, wanted {fmt(r['wanted'])}"
                  for r in read("request.list", {})] or ["- none"]
        questions = [q for q in read("approval.list", {}) if q.get("status") == "pending"]
        lines.append("Questions waiting for your answer:")
        lines += [f"- approval_id {q['approval_id']}: " + (
                  f"move from {fmt(q['from'])} to {fmt(q['to'])}" if q["kind"] == "reschedule"
                  else f"offered {fmt(q['to'])}") for q in questions] or ["- none"]
        return "\n".join(lines)

    def _tool(self, session: Session, convo: Conversation, by_name: dict, call: Call):
        """Run one tool call through the gate; (text for the model, is_error)."""
        if call.name == HANDOFF_TOOL["name"] and session.principal.role is Role.CLIENT:
            convo.handoff_offered = True
            return ("The person now sees a button to send this conversation to their "
                    "clinician, and the emergency contacts. Nothing has been sent; do not "
                    "say it has.", False)
        command = by_name.get(call.name)
        if command is None:
            return f"There is no tool called {call.name}.", True
        try:
            outcome = self.gate.execute(session, control.build_command(command, call.args, session))
        except control.GateError as exc:
            return str(exc), True
        if outcome.status == "needs_confirmation":
            # The token stays here: the model cannot confirm what it prepared.
            return (f'Prepared, not done. The person now sees a card reading "{outcome.summary}" '
                    f"and must confirm it in the app themselves."), False
        return json.dumps(outcome.result, default=str), False

    # -- names, for the provider's conversations ----------------------------------

    def _pseudonymise(self, session: Session, text: str) -> str:
        """Swap any client name the provider typed for this conversation's
        pseudonym, before it leaves the practice."""
        aliases = self.gate._pseudonyms(session)
        for cid, client in sorted(self.world.clients.items(), key=lambda kv: -len(kv[1].name)):
            pattern = re.compile(rf"\b{re.escape(client.name)}\b", re.IGNORECASE)
            if pattern.search(text):
                text = pattern.sub(aliases.of(cid), text)
        return text

    def _restore_names(self, session: Session, text: str) -> str:
        aliases = self.gate._pseudonyms(session)
        for label, cid in sorted(aliases.real.items(), key=lambda kv: -len(kv[0])):
            client = self.world.clients.get(cid)
            if client:
                text = re.sub(rf"\b{re.escape(label)}\b", client.name, text)
        return text

    # -- the hand-off --------------------------------------------------------------------

    def handoff(self, principal: Principal, conversation_id: str) -> dict:
        """The person, not the model, sending this conversation to their
        clinician. What is sent is their own words — never the model's."""
        convo = self.conversation(principal, conversation_id)
        # The person's own choice, whether or not anything offered it: the
        # "Send this to my clinician" link is always there for them.
        if principal.role is not Role.CLIENT or not convo.typed:
            raise control.Forbidden("nothing to pass on here")
        if convo.handoff_sent:
            return self.view(convo)
        text = ("Passed on from the scheduling assistant, at my request. What I wrote:\n\n"
                + "\n\n".join(f"“{t}”" for t in convo.typed))
        sent = self.gate.execute(Session(principal), control.build_command(
            "message.send", {"text": text[: self.world.MAX_MESSAGE]}, Session(principal)))
        for message in self.world.messages:
            if message.id == sent.result["message_id"]:
                message.handoff = True                # so the clinician sees it may need attention
        convo.handoff_sent = True
        convo.visible.append({"role": "assistant", "text":
                              "Sent to your clinician. " + self.world.chat.reply_expectation})
        return self.view(convo)

    def view(self, convo: Conversation) -> dict:
        return {"conversation_id": convo.id, "messages": convo.visible,
                "handoff_offered": convo.handoff_offered and not convo.handoff_sent,
                "handoff_sent": convo.handoff_sent,
                "emergency_note": self.world.chat.emergency_note if convo.handoff_offered else None}
