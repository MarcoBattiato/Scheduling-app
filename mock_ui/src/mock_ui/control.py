"""The gate: the one way a command reaches the World.

Everything an assistant will ever do comes through `Gate.execute`, and the
model never decides what it may do — this does, from the session, not from
anything in the conversation. In order:

1. **Parse.** `build_command` turns untrusted JSON into one of the typed
   commands in `scheduling_contracts`. Unknown fields, missing fields and
   wrong types are refused before anything else looks at them.
2. **Identity.** Who is asking comes from the `Session`, never from the
   command. A client's commands always mean *them*: a missing `client_id` is
   filled in, and a different one is treated as something that does not
   exist. An assistant acting for the provider may only name clients by the
   pseudonyms it was shown in this conversation.
3. **Ownership.** The gate looks up who owns the target (an appointment, a
   request, an approval…) — `owner_of` — so the policy can stay pure.
4. **Policy.** `scheduling_contracts.decide`: allowed, needs a human's
   confirmation, or refused. Refusing something that belongs to someone
   else reads exactly like asking for something that does not exist, so a
   refusal can never be used to learn that another client's booking exists.
5. **Drafts.** "Needs confirmation" executes nothing. The exact command is
   stored behind a single-use token with a plain-language summary written
   *here*, from the command and the calendar — never by the model — and
   only the person themselves, outside the assistant, can confirm it.
6. **Views.** What comes back to an assistant is cut down for it: a client's
   assistant gets no identifiers at all (everything is "you"); the
   provider's sees per-conversation pseudonyms instead of names; neither
   sees service names, notes or prices attached to a person.
7. **Audit.** Every attempt — done, drafted, refused, confirmed — is
   recorded with who, through which channel, and in which conversation.

The screens do not go through here yet; they will, and then signing in is a
matter of filling in the session from a login instead of "view as".
"""
from __future__ import annotations

import collections.abc
import dataclasses
import hashlib
import secrets
import typing
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Any, Callable, Dict, List, Optional

from calendar_store import Party
import scheduling_contracts as sc
from scheduling_contracts import (
    ASSISTANT, FORBIDDEN, MATRIX, Access, Deny, NeedsConfirmation, Principal,
    Role, Session, Target, TargetKind, TimeSpan, WeeklyWindow, by_name, decide,
    spec_of, unresolved_target,
)


PROVIDER_SELF = "provider-self"


# -- errors ---------------------------------------------------------------


class GateError(Exception):
    status = 400


class Invalid(GateError):
    status = 400


class NotFound(GateError):
    """Also what a refusal looks like when the target is someone else's."""
    status = 404


class Forbidden(GateError):
    status = 403


# -- outcomes -------------------------------------------------------------


@dataclass
class Outcome:
    status: str                       # "done" | "needs_confirmation"
    result: Any = None
    token: Optional[str] = None
    summary: Optional[str] = None


@dataclass
class Draft:
    token: str
    principal: Principal
    command: object
    summary: str
    created: datetime
    conversation_id: Optional[str]
    fingerprint: str


# -- untrusted input → typed command --------------------------------------


def _hints(cls) -> Dict[str, Any]:
    return typing.get_type_hints(cls, vars(sc.commands))


def _unwrap_optional(tp):
    args = typing.get_args(tp)
    if typing.get_origin(tp) is typing.Union and type(None) in args:
        rest = [a for a in args if a is not type(None)]
        return rest[0], True
    return tp, False


def _coerce(name: str, tp, value):
    tp, optional = _unwrap_optional(tp)
    if value is None:
        if optional:
            return None
        raise Invalid(f"{name} is required")
    origin = typing.get_origin(tp)
    try:
        if tp is bool:
            if not isinstance(value, bool):
                raise TypeError
            return value
        if tp is int:
            if isinstance(value, bool) or not isinstance(value, (int, str)):
                raise TypeError
            return int(value)
        if tp is float:
            if isinstance(value, bool):
                raise TypeError
            return float(value)
        if tp is str:
            if not isinstance(value, str):
                raise TypeError
            return value
        if tp is datetime:
            return datetime.fromisoformat(value)
        if tp is date:
            return date.fromisoformat(value)
        if tp is time:
            return time.fromisoformat(value)
        if tp is TimeSpan:
            return TimeSpan(datetime.fromisoformat(value["start"]),
                            datetime.fromisoformat(value["end"]))
        if tp is WeeklyWindow:
            return WeeklyWindow(int(value["weekday"]), time.fromisoformat(value["start"]),
                                time.fromisoformat(value["end"]))
        if origin in (list, tuple, collections.abc.Sequence):
            (inner,) = typing.get_args(tp)
            if not isinstance(value, list):
                raise TypeError
            return tuple(_coerce(name, inner, v) for v in value)
    except GateError:
        raise
    except Exception:
        raise Invalid(f"{name} has the wrong type") from None
    # Type aliases (ClientId = str, AppointmentId = int, …) resolve to the
    # underlying type above; anything else is a declaration this does not know.
    raise Invalid(f"{name}: unsupported field type {tp!r}")


def build_command(name: str, args: Dict[str, Any], session: Optional[Session] = None):
    """A typed command from untrusted JSON. Strict: unknown fields refused.

    For a client's session, a missing `client_id` is that client: their tools
    never ask who they are acting for. A *given* one is left for the gate to
    judge, which treats anybody else's as not existing.
    """
    try:
        cls = by_name(name)
    except KeyError:
        raise Invalid(f"no command called {name!r}") from None
    if not isinstance(args, dict):
        raise Invalid("arguments must be an object")
    fields = {f.name: f for f in dataclasses.fields(cls)}
    if (session is not None and session.principal.role is Role.CLIENT
            and "client_id" in fields and "client_id" not in args):
        args = {**args, "client_id": session.principal.client_id}
    unknown = set(args) - set(fields)
    if unknown:
        raise Invalid(f"{name} has no field {sorted(unknown)}")
    hints = _hints(cls)
    values = {}
    for fname, f in fields.items():
        if fname in args:
            values[fname] = _coerce(fname, hints[fname], args[fname])
        elif f.default is dataclasses.MISSING and f.default_factory is dataclasses.MISSING:
            values[fname] = None if _unwrap_optional(hints[fname])[1] else _coerce(fname, hints[fname], None)
    return cls(**values)


# -- pseudonyms -------------------------------------------------------------


class Pseudonyms:
    """Client ↔ "Client A", for one conversation only.

    Labels are handed out in a random order, so "Client A" says nothing about
    who signed up first or whose name sorts first, and a fresh conversation
    gets a fresh mapping, so nothing links one conversation to another.
    """

    def __init__(self) -> None:
        letters = [chr(c) for c in range(ord("A"), ord("Z") + 1)]
        letters += [a + b for a in letters for b in letters]
        head = letters[:26]
        secrets.SystemRandom().shuffle(head)
        self._labels = iter(head + letters[26:])
        self.alias: Dict[str, str] = {}
        self.real: Dict[str, str] = {}

    def of(self, client_id: str) -> str:
        if client_id == PROVIDER_SELF:
            return "you"
        if client_id not in self.alias:
            label = f"Client {next(self._labels)}"
            self.alias[client_id], self.real[label] = label, client_id
        return self.alias[client_id]

    def resolve(self, label: str) -> Optional[str]:
        return self.real.get(label)


# -- the gate -----------------------------------------------------------------

# Results stripped of anything that identifies or describes a person, before
# an assistant sees them. Scheduling needs times and durations, not this.
_PERSONAL = ("name", "service", "notes", "price", "preferred_by")


class Gate:
    DRAFT_LIFETIME = timedelta(minutes=15)

    def __init__(self, world, clock: Callable[[], datetime] = datetime.now) -> None:
        self.world = world
        self.clock = clock
        self.drafts: Dict[str, Draft] = {}
        self.pseudonyms: Dict[str, Pseudonyms] = {}

    # -- the front door ---------------------------------------------------

    def execute(self, session: Session, command) -> Outcome:
        try:
            spec = spec_of(command)
        except LookupError:
            raise Invalid("not a command") from None
        if session.via_assistant and not session.conversation_id:
            raise Invalid("an assistant session needs a conversation id")

        command = self._as_seen_by(session, command)
        target = self._resolve(command)
        decision = decide(session, command, target)

        if isinstance(decision, Deny):
            self._audit(session, spec.name, target, "refused", decision.reason)
            if decision.disguise_as_missing:
                raise NotFound(self._missing(target))
            raise Forbidden("that is not something you can do here")
        if target.kind is not TargetKind.NONE and target.ident is not None \
                and not self._exists(target):
            self._audit(session, spec.name, target, "refused", "no such target")
            raise NotFound(self._missing(target))
        if type(command) not in HANDLERS:
            self._audit(session, spec.name, target, "refused", "no handler")
            raise Invalid(f"{spec.name} is not available here yet")

        if isinstance(decision, NeedsConfirmation):
            draft = Draft(
                token=secrets.token_urlsafe(18), principal=session.principal,
                command=command, summary=self.describe(command, session.principal),
                created=self.clock(), conversation_id=session.conversation_id,
                fingerprint=_fingerprint(command),
            )
            self.drafts[draft.token] = draft
            self._audit(session, spec.name, target, "drafted", draft.summary)
            return Outcome("needs_confirmation", token=draft.token, summary=draft.summary)

        result = self._run(session, command, spec.name, target)
        return Outcome("done", result=self._for(session, result))

    def confirm(self, session: Session, token: str) -> Outcome:
        """The human turning a draft into an act. Never through an assistant."""
        if session.via_assistant:
            self._audit(session, "draft.confirm", None, "refused", "assistant tried to confirm")
            raise Forbidden("only you can confirm this, on your own screen")
        draft = self.drafts.get(token)
        if draft is None or draft.principal != session.principal:
            raise NotFound("no such request to confirm")
        del self.drafts[token]                                  # single use, whatever happens
        if self.clock() - draft.created > self.DRAFT_LIFETIME:
            raise NotFound("that request has expired; ask again")
        if _fingerprint(draft.command) != draft.fingerprint:   # defence in depth
            raise Forbidden("that request was altered")
        spec = spec_of(draft.command)
        target = self._resolve(draft.command)
        # Asked afresh, as the person themselves: permissions may have changed
        # since it was drafted, and the person may no longer own the target.
        decision = decide(session, draft.command, target)
        if isinstance(decision, Deny):
            self._audit(session, spec.name, target, "refused", decision.reason)
            raise NotFound(self._missing(target)) if decision.disguise_as_missing \
                else Forbidden("that is not something you can do here")
        result = self._run(session, draft.command, spec.name, target,
                           outcome="confirmed", conversation=draft.conversation_id)
        return Outcome("done", result=result)

    def discard(self, session: Session, token: str) -> None:
        draft = self.drafts.get(token)
        if draft is None or draft.principal != session.principal:
            raise NotFound("no such request")
        del self.drafts[token]
        self._audit(session, spec_of(draft.command).name, None, "discarded", draft.summary)

    def pending(self, principal: Principal) -> List[Draft]:
        now = self.clock()
        for token in [t for t, d in self.drafts.items() if now - d.created > self.DRAFT_LIFETIME]:
            del self.drafts[token]
        return sorted((d for d in self.drafts.values() if d.principal == principal),
                      key=lambda d: d.created)

    # -- identity -----------------------------------------------------------

    def _pseudonyms(self, session: Session) -> Pseudonyms:
        return self.pseudonyms.setdefault(session.conversation_id, Pseudonyms())

    def _as_seen_by(self, session: Session, command):
        """Put the session's identity into the command; never take it from it."""
        names = {f.name for f in dataclasses.fields(command)}
        if "client_id" not in names:
            return command
        given = command.client_id
        if session.principal.role is Role.CLIENT:
            me = session.principal.client_id
            if given is None:
                return dataclasses.replace(command, client_id=me)
            if given != me:
                raise NotFound("no such client")
            return command
        if session.via_assistant and given is not None:
            # The provider's assistant only ever saw pseudonyms; a real id
            # here was not learned from this conversation, so it is refused.
            real = self._pseudonyms(session).resolve(given)
            if real is None:
                raise NotFound("no such client")
            return dataclasses.replace(command, client_id=real)
        return command

    # -- ownership ------------------------------------------------------------

    def _resolve(self, command) -> Target:
        t = unresolved_target(command)
        if t.kind is TargetKind.NONE or t.ident is None:
            return t
        return Target(t.kind, t.ident, self.owner_of(t))

    def owner_of(self, target: Target) -> Optional[str]:
        w, ident = self.world, target.ident
        try:
            if target.kind is TargetKind.CLIENT:
                return ident if ident in w.clients else None
            key = int(ident)
            if target.kind is TargetKind.APPOINTMENT:
                return w.store.get_appointment(key).client_id
            table = {TargetKind.REQUEST: w.requests, TargetKind.APPROVAL: w.approvals,
                     TargetKind.SERIES: w.series, TargetKind.CHARGE: w.charges}.get(target.kind)
            if table is not None:
                return table[key].client_id
        except (KeyError, ValueError, TypeError):
            return None
        return None                                   # plans are practice-wide

    def _exists(self, target: Target) -> bool:
        if target.owner is not None:
            return True
        if target.kind is TargetKind.PLAN:
            try:
                return int(target.ident) in self.world.plans
            except (TypeError, ValueError):
                return False
        return False

    @staticmethod
    def _missing(target: Optional[Target]) -> str:
        kind = target.kind.value if target and target.kind is not TargetKind.NONE else "thing"
        return f"no such {kind}"

    # -- running ----------------------------------------------------------------

    def _run(self, session, command, name, target, outcome="done", conversation=None):
        try:
            result = HANDLERS[type(command)](self.world, command, session.principal)
        except KeyError as exc:
            self._audit(session, name, target, "failed", f"not found: {exc}")
            raise NotFound(self._missing(target)) from None
        except ValueError as exc:
            self._audit(session, name, target, "failed", str(exc))
            raise Invalid(str(exc)) from None
        self._audit(session, name, target, outcome, "", conversation=conversation)
        return result

    def _for(self, session: Session, result):
        """What an assistant may see of a result. People using the screens
        get it as is; an assistant gets it without identities."""
        if not session.via_assistant:
            return result
        if session.principal.role is Role.CLIENT:
            return _strip(result, alias=None)
        return _strip(result, alias=self._pseudonyms(session))

    def _audit(self, session, name, target, outcome, detail, conversation=None):
        self.world.audit.append({
            "at": self.clock().isoformat(timespec="seconds"),
            "role": session.principal.role.value,
            "client": session.principal.client_id,
            "via_assistant": session.via_assistant,
            "conversation": conversation or session.conversation_id,
            "command": name,
            "target": (f"{target.kind.value}:{target.ident}"
                       if target is not None and target.kind is not TargetKind.NONE else None),
            "outcome": outcome,
            "detail": detail,
        })

    # -- the words on a confirmation card ---------------------------------------

    def describe(self, command, principal: Principal) -> str:
        """Plain language for what will happen — from the command and the
        calendar, never from whatever asked for it."""
        w = self.world
        mine = principal.role is Role.CLIENT
        who = (lambda cid: "you" if mine else (w.clients[cid].name if cid in w.clients else "you"))
        your = (lambda cid: "your" if mine else f"{who(cid)}'s")

        def when(moment):
            return f"{moment:%a %d %b}, {moment:%H:%M}"

        def service(sid):
            return w._service_name(sid) or "appointment"

        c = command
        if isinstance(c, sc.SubmitRequest):
            return f"Ask for a {service(c.service_id)} around {when(c.preferred_start)}"
        if isinstance(c, sc.WithdrawRequest):
            r = w.requests[int(c.request_id)]
            return f"Withdraw the request for a {service(r.service_id)} around {when(r.preferred_start)}"
        if isinstance(c, sc.RequestReschedule):
            a = w.store.get_appointment(int(c.appointment_id))
            text = f"Ask to move {your(a.client_id)} {service(a.service_type_id)} on {when(a.range.start)}"
            if c.preferred_start:
                text += f", ideally to around {when(c.preferred_start)}"
            text += (". The current time is given up now." if c.release_slot
                     else ". The current time is kept until a new one is agreed.")
            return text
        if isinstance(c, sc.CancelAppointment):
            a = w.store.get_appointment(int(c.appointment_id))
            text = f"Cancel {your(a.client_id)} {service(a.service_type_id)} on {when(a.range.start)}"
            notice = a.range.start - self.clock()
            if mine and notice < timedelta(hours=w.billing.late_cancel_window_hours):
                text += (f". This is within {w.billing.late_cancel_window_hours} hours, "
                         f"so the practice may charge a late-cancellation fee")
            return text
        if isinstance(c, sc.RespondToApproval):
            a = w.approvals[int(c.approval_id)]
            what = (f"moving from {when(a.was_start)} to {when(a.now_start)}" if a.kind == "reschedule"
                    else f"the offered time, {when(a.now_start)}")
            verb = {"accept": "Accept", "decline": "Decline",
                    "refuse": "Keep the current slot and refuse"}.get(c.answer, c.answer)
            return f"{verb} {what}"
        if isinstance(c, sc.SetAvailabilityException):
            state = "free" if c.available else "away"
            return f"Mark {who(c.client_id)} as {state} on {c.on_date:%a %d %b}, {c.start:%H:%M}–{c.end:%H:%M} (that day only)"
        if isinstance(c, sc.ClearAvailabilityException):
            return f"Undo the change on {c.on_date:%a %d %b}, {c.start:%H:%M}–{c.end:%H:%M}"
        if isinstance(c, sc.SetWeeklyAvailability):
            days = "Mon Tue Wed Thu Fri Sat Sun".split()
            spans = "; ".join(f"{days[x.weekday]} {x.start:%H:%M}–{x.end:%H:%M}" for x in c.windows) or "none"
            return f"Set {your(c.client_id)} usual weekly hours to: {spans}"
        if isinstance(c, sc.StartSeries):
            every = "every week" if c.every_weeks == 1 else f"every {c.every_weeks} weeks"
            until = f", until {c.until:%d %b}" if c.until else ""
            return f"Book a {service(c.service_id)} {every} from {when(c.first_start)}{until}"
        if isinstance(c, sc.StopSeries):
            x = w.series[int(c.series_id)]
            return f"Stop the repeating {service(x.service_id)}; sessions already booked stay"
        if isinstance(c, sc.ProposePlan):
            return (f"Make a schedule (priority {c.alpha if c.alpha is not None else w.alpha}, "
                    f"up to {c.max_displacements if c.max_displacements is not None else w.max_displacements} moves)")
        if isinstance(c, sc.ApprovePlan):
            return f"Send plan {c.plan_id} to the clients it affects"
        if isinstance(c, sc.SettlePlan):
            how = {"agreed": "Confirm what has been agreed", "agreed_and_drop":
                   "Confirm what has been agreed and stop waiting for the rest",
                   "reject": "Drop the whole plan and re-plan"}.get(c.how, c.how)
            return f"{how} (plan {c.plan_id})"
        if isinstance(c, sc.DiscardPlan):
            return f"Discard proposal {c.plan_id}"
        if isinstance(c, sc.MarkAttendance):
            a = w.store.get_appointment(int(c.appointment_id))
            return f"Record that {who(a.client_id)} {'attended' if c.attended else 'did not come'} on {when(a.range.start)}"
        if isinstance(c, sc.PlaceManually):
            r = w.requests[int(c.request_id)]
            return f"Book {who(r.client_id)} yourself at {when(c.start)}"
        if isinstance(c, sc.SetClientLimits):
            return (f"Allow {who(c.client_id)} at most {c.per_day or 'any number of'} a day"
                    f" and {c.per_week or 'any number'} a week")
        return sc.summarise(c)


def _fingerprint(command) -> str:
    return hashlib.sha256(repr(command).encode()).hexdigest()


def _strip(value, alias: Optional[Pseudonyms]):
    """Remove what identifies or describes a person.

    `alias` None: a client's own assistant — every client reference is "you",
    so ids are dropped outright. Otherwise the provider's: ids become this
    conversation's pseudonyms.
    """
    if isinstance(value, list):
        return [_strip(v, alias) for v in value]
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            if k in _PERSONAL:
                continue
            if k == "client_id":
                if alias is not None:
                    out["client"] = alias.of(v)
                continue
            out[k] = _strip(v, alias)
        return out
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    return value


# -- what each command does to the World ------------------------------------


def _appointment(a) -> dict:
    return {"appointment_id": a.id, "client_id": a.client_id,
            "start": a.range.start, "end": a.range.end,
            "status": a.status.value, "service_id": a.service_type_id}


def _request(r) -> dict:
    return {"request_id": r.id, "client_id": r.client_id, "service_id": r.service_id,
            "duration_minutes": r.duration_minutes, "wanted": r.preferred_start,
            "status": r.status, "repeating": r.series_id is not None}


def _approval(a) -> dict:
    return {"approval_id": a.id, "client_id": a.client_id, "kind": a.kind,
            "status": a.status, "applied": a.applied,
            "appointment_id": a.appointment_id, "from": a.was_start,
            "to": a.now_start, "to_end": a.now_end}


def _windows(windows) -> list:
    return [{"weekday": x.weekday, "from": f"{x.start:%H:%M}", "to": f"{x.end:%H:%M}"}
            for x in windows]


def _read_appointments(w, c, p):
    return [_appointment(a) for a in w.store.appointment_history(c.client_id, c.window.start, c.window.end)
            if c.include_history or a.status.value == "booked"]


def _read_availability(w, c, p):
    return [{"start": s.start, "end": s.end} for s in
            w.availability_segments(c.client_id, c.window.start.date(), c.window.end.date())]


def _list_requests(w, c, p):
    return [_request(r) for r in w.requests.values()
            if (c.client_id is None or r.client_id == c.client_id)
            and r.status in ("pending", "on_hold", "awaiting_client")]


def _list_approvals(w, c, p):
    mine = (lambda a: a.client_id == p.client_id) if p.role is Role.CLIENT else (lambda a: True)
    return [_approval(a) for a in w.approvals.values()
            if mine(a) and w._outstanding(a)]


def _list_services(w, c, p):
    return [{"service_id": s.id, "label": s.name, "duration_minutes": s.duration_minutes}
            for s in w.catalogue.services(include_inactive=c.include_inactive)
            if s.client_bookable or p.role is not Role.CLIENT]


def _list_plans(w, c, p):
    return [{"plan_id": x.id, "status": x.status, "params": x.params,
             "metrics": {k: v for k, v in x.metrics.items() if k != "warnings"},
             "placements": [{"client_id": y["client_id"], "start": y["start"]} for y in x.placements],
             "moves": [{"client_id": y["client_id"], "from": y["was_start"], "to": y["now_start"]}
                       for y in x.displacements]}
            for x in w.plans.values() if x.status in ("draft", "awaiting_clients", "answered")]


def _preview(w, c, p):
    return w.propose(alpha=c.alpha, max_displacements=c.max_displacements,
                     request_ids=[int(r) for r in c.request_ids] if c.request_ids else None,
                     horizon_days=c.horizon_days, dry_run=True, reason="preview")


def _list_charges(w, c, p):
    # A proposed fee is the provider's to decide on; until then it is not the
    # client's to see, through the screens or an assistant.
    show_proposed = p.role is not Role.CLIENT
    return [{"charge_id": x.id, "client_id": x.client_id, "kind": x.kind,
             "amount_cents": x.amount, "status": x.status, "description": x.description}
            for x in w.charges.values()
            if (c.client_id is None or x.client_id == c.client_id)
            and (show_proposed or x.status != "proposed")]


def _summary(w, c, p):
    return {"client_id": c.client_id, **w.client_summary(c.client_id)}


def _set_weekly(w, c, p):
    w.set_weekly_availability(c.client_id, _windows(c.windows))
    return {"ok": True}


def _set_exception(w, c, p):
    w.set_exception(c.client_id, c.on_date, c.start, c.end, c.available)
    return {"ok": True}


def _clear_exception(w, c, p):
    # Undoing applies the opposite, so it matters which kind it was — and the
    # store, not the caller, is the one that knows.
    match = [e for e in w.store.exceptions_for(c.client_id)
             if e.date == c.on_date and e.start_time == c.start and e.end_time == c.end]
    if not match:
        raise KeyError("no such change on that date")
    w.clear_exception(c.client_id, c.on_date, c.start, c.end, match[0].kind.value == "add")
    return {"ok": True}


def _submit(w, c, p):
    r = w.submit_request(c.client_id, c.service_id, c.preferred_start.isoformat())
    return {"request_id": r.id}


def _withdraw(w, c, p):
    w.withdraw_request(int(c.request_id))
    return {"ok": True}


def _start_series(w, c, p):
    x = w.start_series(c.client_id, c.service_id, c.first_start.isoformat(), c.every_weeks,
                       c.until.isoformat() if c.until else None)
    return {"series_id": x.id}


def _stop_series(w, c, p):
    return {"withdrawn": w.stop_series(int(c.series_id))}


def _cancel(w, c, p):
    # Who cancelled is the session's, never the caller's to say: it is the
    # evidence fees and habitual-slot anchoring read.
    by = Party.CLIENT if p.role is Role.CLIENT else Party.PROVIDER
    w.cancel_appointment(int(c.appointment_id), by=by)
    return {"ok": True}


def _reschedule(w, c, p):
    a = w.store.get_appointment(int(c.appointment_id))
    wish = (c.preferred_start or a.range.start).isoformat()
    r = w.request_reschedule(int(c.appointment_id), wish, c.release_slot)
    return {"request_id": r.id}


def _respond(w, c, p):
    w.respond_to_approval(int(c.approval_id), c.answer)
    return {"ok": True}


def _propose(w, c, p):
    out = w.propose(alpha=c.alpha, max_displacements=c.max_displacements,
                    request_ids=[int(r) for r in c.request_ids] if c.request_ids else None,
                    horizon_days=c.horizon_days, reason=c.reason or "manual")
    return {k: out.get(k) for k in ("ran", "plan_id", "placements", "displacements", "reason")}


def _approve(w, c, p):
    w.provider_approve(int(c.plan_id), list(c.items) if c.items else None)
    return {"ok": True}


def _settle(w, c, p):
    how = {"agreed": "agreed", "agreed_and_drop": "agreed_only", "reject": "reoptimise"}.get(c.how)
    if how is None:
        raise ValueError("how is agreed, agreed_and_drop or reject")
    out = w.settle_plan(int(c.plan_id), how)
    return {k: v for k, v in out.items() if k != "rerun"}


def _discard(w, c, p):
    w.discard_plan(int(c.plan_id))
    return {"ok": True}


def _attendance(w, c, p):
    w.mark_attendance(int(c.appointment_id), c.attended)
    return {"ok": True}


def _place(w, c, p):
    a = w.place_manually(int(c.request_id), c.start, lock=c.lock)
    return {"appointment_id": a.id}


def _limits(w, c, p):
    w.set_client_limits(c.client_id, c.per_day, c.per_week)
    return {"ok": True}


HANDLERS: Dict[type, Callable] = {
    sc.ReadAppointments: _read_appointments,
    sc.ReadAvailability: _read_availability,
    sc.ListRequests: _list_requests,
    sc.ListApprovals: _list_approvals,
    sc.ListServices: _list_services,
    sc.ListPlans: _list_plans,
    sc.PreviewPlan: _preview,
    sc.ListCharges: _list_charges,
    sc.ReadClientSummary: _summary,
    sc.SetWeeklyAvailability: _set_weekly,
    sc.SetAvailabilityException: _set_exception,
    sc.ClearAvailabilityException: _clear_exception,
    sc.SubmitRequest: _submit,
    sc.WithdrawRequest: _withdraw,
    sc.StartSeries: _start_series,
    sc.StopSeries: _stop_series,
    sc.CancelAppointment: _cancel,
    sc.RequestReschedule: _reschedule,
    sc.RespondToApproval: _respond,
    sc.ProposePlan: _propose,
    sc.ApprovePlan: _approve,
    sc.SettlePlan: _settle,
    sc.DiscardPlan: _discard,
    sc.MarkAttendance: _attendance,
    sc.PlaceManually: _place,
    sc.SetClientLimits: _limits,
}


# -- what an assistant is offered --------------------------------------------


def _schema_for(tp) -> dict:
    tp, _ = _unwrap_optional(tp)
    if tp is bool:
        return {"type": "boolean"}
    if tp is int:
        return {"type": "integer"}
    if tp is float:
        return {"type": "number"}
    if tp is datetime:
        return {"type": "string", "description": "local date and time, e.g. 2026-10-13T09:00"}
    if tp is date:
        return {"type": "string", "description": "a date, e.g. 2026-10-13"}
    if tp is time:
        return {"type": "string", "description": "a time of day, e.g. 09:30"}
    if tp is TimeSpan:
        return {"type": "object", "properties": {"start": {"type": "string"}, "end": {"type": "string"}},
                "required": ["start", "end"], "additionalProperties": False}
    if tp is WeeklyWindow:
        return {"type": "object", "properties": {
            "weekday": {"type": "integer", "description": "0 = Monday"},
            "start": {"type": "string"}, "end": {"type": "string"}},
            "required": ["weekday", "start", "end"], "additionalProperties": False}
    args = typing.get_args(tp)
    if args and typing.get_origin(tp) is not None and typing.get_origin(tp) is not typing.Union:
        return {"type": "array", "items": _schema_for(args[0])}
    return {"type": "string"}


def tools_for(session: Session) -> List[dict]:
    """The commands an assistant acting for this person may ask for, as tool
    definitions. Cut to the person: a client's assistant is not even told
    provider commands exist, and is never asked who it is acting for —
    `client_id` is absent from its tools, because the gate always means "you".
    """
    role = session.principal.role
    tools = []
    for cls, handler in HANDLERS.items():
        name = _name_of(cls)
        if MATRIX[role].get(name) is Access.DENIED or ASSISTANT.get(name) == FORBIDDEN:
            continue
        hints = _hints(cls)
        props, required = {}, []
        for f in dataclasses.fields(cls):
            if f.name == "client_id" and role is Role.CLIENT:
                continue
            schema = _schema_for(hints[f.name])
            if f.name == "client_id":
                schema = {"type": "string", "description": 'a client as shown to you, e.g. "Client B"'}
            props[f.name] = schema
            if f.default is dataclasses.MISSING and f.default_factory is dataclasses.MISSING \
                    and not _unwrap_optional(hints[f.name])[1]:
                required.append(f.name)
        tools.append({
            "name": name.replace(".", "_"),
            "command": name,
            "description": " ".join((cls.__doc__ or name).split()),
            "needs_confirmation": ASSISTANT.get(name) != "allowed",
            "input_schema": {"type": "object", "properties": props, "required": required,
                             "additionalProperties": False},
        })
    return tools


_NAMES = {}


def _name_of(cls) -> str:
    if cls not in _NAMES:
        for name in sc.names():
            if by_name(name) is cls:
                _NAMES[cls] = name
                break
    return _NAMES[cls]
