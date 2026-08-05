# Scheduling Engine — SPEC.md (v2)

Supersedes the original interview-generated `SPEC.md` in full. Written in Python
(not TypeScript — confirmed: the rest of the backend is Python/FastAPI, this module
lives in-process, no service boundary needed). Consolidates every decision made
across the architecture-review pass; anything not explicitly discussed and reasoned
through together is marked **[DRAFT — not yet reviewed]** rather than presented as
settled.

---

## 1. Overview

This is a **scheduling** engine, not a rescheduling engine. A fresh booking and a
reschedule are the same operation:

- Fresh booking: run the allocator against the current timetable.
- Reschedule: virtually remove the originating appointment from the timetable, then
  run the exact same allocator as if it were a fresh request.

Scope: single provider, single calendar. No multi-staff/multi-resource reassignment.

The engine's core is pure and in-memory per invocation; all reads/writes to real data
go through an injected persistence interface (repository pattern). It owns more than
search — it owns the negotiation lifecycle (offer → confirmed → exercised) and
triggers notifications, but delegates actual delivery (email/SMS/push) to an
injected `NotificationPort`.

---

## 2. Domain Model

```python
from dataclasses import dataclass, field
from datetime import datetime, time
from enum import Enum
from typing import Optional

ClientId = str
AppointmentId = str
RequestId = str
ChangeId = str


@dataclass(frozen=True)
class TimeRange:
    start: datetime
    end: datetime
    # Deliberately not shared with calendar_store's TimeSegment, despite the
    # identical shape: TimeRange here covers request windows, accepted-change
    # windows, and appointment ranges — none of which are calendar_store's
    # concern — so importing its type for all of those would be coupling in
    # the wrong direction. Only availability segments actually flow from
    # calendar_store; see §3.


@dataclass
class RescheduleBounds:
    """Business-configured, not client-editable. See §4."""
    max_days_earlier: int
    max_days_later: int


@dataclass
class Client:
    id: ClientId
    priority: float                        # higher = more costly to disturb
    reschedule_bounds: RescheduleBounds     # e.g. {2, 4} default; {0, 0} for
                                             # provider-self blocks (§4)
    # No `availability` field here — recurring rules and exceptions are
    # calendar_store's domain entirely now, not carried on this object. See §3.


class ChangeStatus(Enum):
    PROPOSED = "proposed"     # cold-ask sent, awaiting first response
    OPEN = "open"             # standing offer, not currently claimed by any
                               # outstanding proposal — no lock; see §8
    CONFIRMED = "confirmed"   # client said yes, not yet applied to BookedSlots
    EXERCISED = "exercised"   # applied for real; entry retired
    DECLINED = "declined"     # client said no, in full or in part


@dataclass
class AcceptedChange:
    id: ChangeId
    client_id: ClientId
    original_slot: TimeRange
    acceptable_window: TimeRange   # must fit within client.reschedule_bounds, §4
    duration_minutes: int
    status: ChangeStatus


# `Appointment` (id, client_id, service_type_id, range, locked, notes) is
# calendar_store's canonical type now, not redefined here — see its
# INTERFACE.md. `AppointmentId` above stays as a plain `str` alias for
# referencing one in this engine's own protocol methods (§3, §11).


class RequestKind(Enum):
    NECESSARY = "necessary"   # original booking cannot stand; cancelled
                               # immediately at creation, §6
    DESIRED = "desired"       # original booking stays live; no forced fallback


class RequestStatus(Enum):
    UNSATISFIED = "unsatisfied"
    PENDING_CONFIRMATION = "pending_confirmation"
    SATISFIED = "satisfied"
    FAILED = "failed"


@dataclass
class ActiveRequest:
    id: RequestId
    client_id: ClientId
    kind: RequestKind
    duration_minutes: int
    preferred_window: TimeRange
    status: RequestStatus
    submitted_at: datetime
```

### 2.1 Provider as pseudo-client

The provider's own blocked time (lunch, errands) is an ordinary `Appointment` for
`client_id = "provider-self"`, with its own `Client` record — typically low
`priority` (cheap to disturb relative to a paying client) and a tight
`reschedule_bounds` (§4). No separate mechanism needed for provider scheduling.

---

## 3. Data access: windowed, not global

The engine never operates on the full calendar. Every `resolve()` pass (§5) works
against a **bounded rolling window** (e.g. the next 30 days), fetched via a
date-ranged repository query:

```python
class SchedulingRepository(Protocol):
    def get_window(self, date_range: TimeRange) -> "TimetableSnapshot": ...
    def get_client(self, client_id: ClientId) -> Client: ...
    def apply_move(self, change_id: ChangeId, new_range: TimeRange) -> None: ...
    def cancel_appointment(self, appointment_id: AppointmentId) -> None: ...
```

**Availability comes from `calendar_store`, as flat segments.** `TimetableSnapshot`
carries each relevant client's (and the provider's) availability as
`list[calendar_store.TimeSegment]` for the requested window — this engine never
sees a rule, an exception, or the store itself, only the already-resolved result.
How that's produced is entirely `calendar_store`'s concern; see its
`INTERFACE.md` for the contract (`SPEC.md` is calendar_store's own internals).

---

## 4. Hard bounds

Two independent scoping mechanisms:

- **Window** (§3): how far ahead the engine looks at all.
- **Per-client `reschedule_bounds`**: how far *any* proposed move — the
  requester's own stated preference, a standing `AcceptedChange.acceptable_window`,
  or a fresh cold-ask — may fall from the appointment's *current* date. Read from
  `Client.reschedule_bounds`, not a global constant, specifically so
  `provider-self` (`{0, 0}`) can never have a lunch break pushed to a different
  calendar day — that's a hard constraint violation, not a high-cost option the
  optimizer merely avoids. Ordinary clients get the business default (e.g. `{2, 4}`)
  unless individually overridden.

  **Exempt for the requester's own ask.** `reschedule_bounds` exists to cap
  *involuntary* disruption — it constrains where a third party can be relocated to
  or a fresh cold-ask can target, and it constrains a standing `AcceptedChange`'s
  `acceptable_window`. It does **not** constrain `ActiveRequest.preferred_window`
  for the client who submitted that request: an explicit, informed ask to move
  further than their own configured bounds is the client's own choice, not
  disruption imposed on them, and should not be silently clipped. If that same
  client is separately displaced as a third party by a *different* request in the
  same batch, `reschedule_bounds` applies normally to that displacement — the
  exemption only covers fulfilling their own request.

**[DRAFT — not yet reviewed]** Carried forward unchanged from the original v1 spec,
not revisited in this design pass — worth an explicit sanity check before
implementation:
- `locked` appointments (staff-pinned) are immovable regardless of notice.
- A notice cutoff (default 24h) excludes appointments too close to "now" from being
  offered as movable via cold-ask.

---

## 5. Solver: persistent state, joint batch resolution

Not a stateless `resolve(requests) -> plans` call — there's no "done," only
"currently." The engine holds live state (all calendars, all `AcceptedChange`
entries, the `ActiveRequest` queue) and a `resolve()` step gets **triggered**, not
polled, by exactly five external events:

1. a scheduling request added to the queue
2. a reply to a moving enquiry sent to a client returned (accept / decline / shrink)
3. a change of mind on a `CONFIRMED`-but-not-yet-`EXERCISED` move — invalid once
   the move has actually been applied
4. a scheduling request itself being cancelled (client withdraws the ask)
5. a pure cancellation — an appointment cancelled outright, independent of this
   negotiation machinery entirely

No periodic sweep. Provider availability changes aren't an explicit trigger; since
every pass reconsiders the entire current batch jointly, such a change is picked up
by the next trigger of any kind. Acceptable given continuous request flow.

**Each `resolve()` pass treats every currently-unsatisfied request as one joint
problem**, not a priority queue processed one at a time — it looks for the best
available assignment across the whole batch, which may satisfy several requests
simultaneously by sharing freed capacity, and may leave some unsatisfied. **A
partial solution is an expected, valid outcome.**

This is an online algorithm: a `resolve()` pass only knows about requests that
exist so far. "Jointly optimal across every request that will ever arrive" isn't a
coherent target; "jointly optimal across the current snapshot" is.

**Implementation note:** many decision variables (assignment of each active request
and each candidate offer to a slot or "unchanged"), hard constraints (no
double-booking, availability, §4's bounds, notice cutoff), and a multi-term
objective with partial satisfaction allowed — this is a natural fit for a
constraint solver (OR-tools CP-SAT) rather than hand-rolled search. At this problem
scale (bounded window, one provider, a small client base) it should solve to
optimality in milliseconds.

---

## 6. Request kinds: necessary vs. desired

`ActiveRequest.kind` distinguishes exactly two behaviors, both otherwise identical
in solver treatment:

- **`DESIRED`**: no forced action ever. Submitting the request registers an `OPEN`
  `AcceptedChange` for the client's current slot and lets the normal joint solve
  pick it up opportunistically — including a *different* client's request ending
  up using the vacated slot while this client lands in their own preferred window,
  in the same solve.
- **`NECESSARY`**: the original booking is **cancelled immediately, at request
  creation time** — no deadline, no waiting to see if a solution turns up first. By
  definition the client cannot use the slot regardless of outcome; if the slot
  could still be kept as a fallback, the request isn't `NECESSARY`. This reduces to
  the existing pure-cancellation trigger (§5.5) firing immediately, followed by an
  ordinary fresh `ActiveRequest` — indistinguishable from a brand-new booking from
  that point on. Cancellation fires a client notification via `NotificationPort`.

  Because the original slot is already gone, an unbounded wait isn't acceptable
  for `NECESSARY` the way it is for `DESIRED`: if a `NECESSARY` request has gone a
  configurable number of `resolve()` passes, or a configurable elapsed duration
  (business-tunable, e.g. "48 hours"), with no feasible assignment materializing,
  it transitions to `FAILED` and triggers a client notification via
  `NotificationPort` — surfacing the situation for manual handling rather than
  leaving the client silently unbooked forever. `DESIRED` requests are **not**
  subject to this timeout: their original booking stays live throughout, so
  indefinite `UNSATISFIED` persistence is an acceptable, expected outcome (see
  above) — they only ever leave the queue via satisfaction or an explicit
  `cancel_request`.

---

## 7. Per-batch algorithm

1. **Fetch scope.** Pull the current windowed state (§3): calendars, booked slots,
   `OPEN`/`PROPOSED`/`CONFIRMED` `AcceptedChange`s, every unsatisfied
   `ActiveRequest`.
2. **Joint solve.** Find the assignment maximizing (urgency-weighted, §10) requests
   satisfied, subject to all hard constraints, using existing `OPEN` offers and
   newly-proposable cold-asks as available moves, minimizing disruption cost as a
   secondary objective among equally-good satisfaction levels. **Cancellation is
   never part of the solver's action space for a third party.** A client's
   appointment can only ever be cancelled via that same client's own `NECESSARY`
   request (§6) or a standalone cancellation (§5.5) — never chosen by the solver to
   relieve pressure on someone else's request. Third parties can be relocated
   (subject to §4) but never involuntarily cancelled.
3. **Propose.** For every move the solve wants to use: a fresh cold-ask creates a
   new `PROPOSED` entry and sends it. **Nothing is locked while awaiting a reply.**
   If the pass reassigns a slot that already has a live, unanswered `PROPOSED` ask
   attached to it — whether that ask originated from reusing an `OPEN` offer or
   from a previous fresh cold-ask — **supersede** it: retract the stale message and
   send the newly-computed one, possibly to a different client than the one
   originally asked. An expressed willingness to move never blocks a subsequently
   discovered, more efficient arrangement; there is deliberately no `RESERVED`
   status (§8). The only race this can create is a client's response landing at
   nearly the same instant a pass supersedes their ask — ordinary persistence-layer
   concurrency (§12). A confirmation for an ask that was just retracted must be
   rejected, and `respond_to_proposal` (§11) needs to surface that distinctly (e.g.
   a `stale_proposal` result) rather than silently no-op'ing, so the caller can tell
   the client their accepted offer is no longer available.
4. **Apply independently.** As responses arrive, each confirmed move is applied to
   `BookedSlots` on its own, immediately — never gated on the rest of that solve's
   proposals also succeeding. A durable, reusable option pool is the point: a
   partial success still benefits future passes even if it doesn't fully resolve
   the request that triggered it.

---

## 8. `AcceptedChange` lifecycle

```
PROPOSED ──(client responds)──▶ CONFIRMED ──▶ EXERCISED
   │                                │  (change of mind, §5.3,
   │                                │   only valid here)
   ▼                                ▼
DECLINED                        DECLINED
   │
   ▼
(§9: partial decline narrows acceptable_window and/or blocks that
 specific date/time in calendar_store, entry may re-open)

OPEN ──(solver selects it)──▶ CONFIRMED ──▶ EXERCISED   (same as above)
```

No `RESERVED` status, for any entry regardless of origin. Recomputing the joint
problem from scratch on every trigger makes a persistent lock unnecessary for
correctness; offer collisions are handled procedurally at the propose step (§7.3),
not via state.

---

## 9. Decline / shrink handling

A decline, or the excluded portion of a shrink, is recorded as a **single-date
block** in calendar_store, scoped to that specific date/time instance — not a
recurring rule change. This reuses the existing feasibility-check machinery and
keeps the risk of over-blocking a genuinely unrelated future booking low, given
typical weekly single-session booking cadence. **Business rule, confirm with your
customer** — easy to change later without touching the surrounding design.

---

## 10. Cost model — tuning deliberately deferred

**Deferred to a later development stage, as a scheduling decision rather than an
omission.** Two reasons, and they reinforce each other:

- **It is decoupled from everything else.** Provided the history carries the
  data a term might need (§10.0), changing how candidate solutions are *ranked*
  touches nothing but the objective function. Nothing else in the engine, the
  store, or the app depends on which weights win.
- **It is the part the customer must have a say in.** How much fragmentation is
  worth how much delay, how far a client may be pushed to fit someone in — these
  are business judgements, not engineering ones, and they are far easier to
  settle by pointing at a working app than by discussing them in the abstract.

So the intended order is: build something demonstrable, run it, gather real
history, *then* tune against that conversation.

What already exists is provisional and marked as such in the code — the α blend
of fragmentation against earliness (§7), the displacement-count tier, and the
choice to weight earliness and displacement shift alike. None of it is a
settled answer to this section.

### 10.0 What is NOT deferred: what the history must capture

Weights can be changed whenever. **Data that was never recorded cannot be
recovered**, so the capture requirements are settled now even though their use
is not. `calendar_store` therefore keeps, and must go on keeping:

- **Cancelled appointments.** A cancelled booking is still evidence that a
  client once held that slot. Deletion destroys it.
- **Where a booking originally sat.** Rescheduling writes a new row and retires
  the old one rather than overwriting the range, so the whole trail survives.
- **Whether a slot was chosen or imposed** (`Origin.CLIENT` vs
  `Origin.DISPLACED`). Without this, anything learning from history learns where
  the *solver* put people, and the engine would launder its own rescheduling
  into a client's apparent preferences — see §10.1.

**Known gaps**, recorded so the decision to leave them is visible rather than
accidental. Each is irrecoverable after the fact, so each should be settled
before any history worth keeping is generated:

- **No timestamps.** Rows carry no record of *when* a booking was made,
  cancelled or moved — only when the appointment itself is. A "notice given"
  term (below) cannot be computed without it, and recency-weighting of history
  is impossible.
- **No attendance.** `AppointmentStatus` has no `COMPLETED` / `NO_SHOW`, because
  nothing can currently set them. A client who does not turn up is weak evidence
  that they like that slot.
- **No cancellation provenance.** A cancellation does not record whether the
  client or the provider initiated it — the same distinction `origin` draws for
  moves, missing for cancellations.
- **Declined reschedule offers are not persisted.** A client refusing to move is
  a strong signal about that slot; today it lives only in `mock_ui`'s memory.

### 10.1 Habitual-slot anchoring — agreed in principle, not yet implemented

Clients book weekly, so the point is **predictability**: a client should be able
to plan around "my usual Tuesday at 15:00" even before the booking exists, and
be moved off it only when something actually forces it. Left alone, the current
objective works against this — `earliness` pulls every request to the first
feasible slot, so a weekly client gets shuffled around the calendar for no
reason they can see.

Agreed so far:

- **Anchor rule.** Three appointments a client *chose* at the same
  `(weekday, time-of-day)` establish that as their anchor. Deliberately a
  countable rule rather than a statistical mode: it is explainable to a client,
  and stable with the handful of appointments a real client will have.
- **Distance in cyclic minutes-within-the-week.** Tue 15:00 → Tue 15:30 is 30;
  Tue 15:00 → Wed 15:00 is 1440. A different day therefore costs roughly 48× a
  half-hour slip with no hand-tuned weighting, and the term stays in the same
  unit as `earliness` and displacement shift.
- **Anchor replaces earliness, per request.** Where a client has an anchor, it
  substitutes for the earliness term rather than competing with it — earliness
  is a proxy for "do not make people wait needlessly", and for an established
  client "their usual slot" states that better. Clients without an anchor (new,
  or pattern not yet formed) keep earliness as today. This also avoids adding a
  fourth term to the unresolved composition in §10.2.
- **Self-reinforcement is intended here.** For preference *learning* it would be
  a flaw; for stability it is the product.
- **Forced moves must not become the pattern.** A displaced booking is the
  exception, not new evidence — and after one, the client should be pulled
  *back* toward their anchor rather than anchored to where they were pushed.
  This is why `calendar_store` records `origin` (client-chosen vs displaced) and
  retains cancelled and superseded rows: without that distinction the engine
  would launder its own rescheduling into a client's apparent habits.

Still open: how many weeks of history count, whether the anchor decays when a
client's pattern shifts, and what a `provider-self` pseudo-client's anchor means.

### 10.2 Carried forward from v1, still unresolved

The material below predates this pass and is a placeholder, not a decision:

- The original v1 five-tier lexicographic idea was cancellations > clients
  affected > time-shift > priority-weighted disturbance > notice given. The
  **cancellations tier no longer applies as originally conceived**: §7 now rules
  that the solver never chooses to cancel a third party, so "number of
  cancellations in *this* candidate solution" isn't a variable the solver is
  weighing — a `NECESSARY` request's cancellation already happened, unconditionally,
  at creation time (§6), before any solve runs. The remaining four tiers (clients
  affected > time-shift > priority-weighted disturbance > notice given) are a
  reasonable starting point for ranking candidate joint solutions; revisit whether
  "number of still-unsatisfied `NECESSARY` requests" deserves to be its own
  dominant tier instead, given it's the closest analogue to the old cancellation
  concern.
- §5/§7 additionally require an urgency-weighted "requests satisfied" term, since
  the solver now jointly optimizes over a batch with partial solutions allowed.
  How these two combine (is satisfaction lexicographically above disruption, or
  blended?) is explicitly unresolved.

Do not implement against this section as-is — revisit it as its own focused pass
before writing the actual objective function.

---

## 11. API surface **[DRAFT — not yet reviewed]**

Not discussed in detail during the design conversation this spec is based on —
sketched here as a starting point, not a derived decision. Needs its own review
pass before implementation.

```python
class SchedulingEngine(Protocol):
    def submit_request(
        self, client_id: ClientId, kind: RequestKind,
        duration_minutes: int, preferred_window: TimeRange,
    ) -> RequestId: ...

    def cancel_request(self, request_id: RequestId) -> None: ...

    def respond_to_proposal(
        self, change_id: ChangeId,
        response: Literal["accept", "decline", "shrink"],
        narrowed_window: Optional[TimeRange] = None,
    ) -> Literal["ok", "stale_proposal"]: ...
    # Raises (does not return a typed result) if change_id is unknown or already
    # resolved (not PROPOSED) — that's an invalid call, not a business outcome.
    # stale_proposal is reserved for the one legitimate race: the ask was
    # superseded (§7.3) between being sent and this response arriving.

    def revoke_confirmation(self, change_id: ChangeId) -> None: ...
    # only valid while status == CONFIRMED, per §5.3 / §8

    def report_cancellation(self, appointment_id: AppointmentId) -> None: ...

    def get_request_status(self, request_id: RequestId) -> RequestStatus: ...
```

---

## 12. Concurrency

Ordinary transactional integrity at the persistence layer (atomic writes,
optimistic concurrency on `AcceptedChange`/`Appointment` rows) — not a
business-logic concern, and specifically not something the removal of `RESERVED`
(§8) creates a gap for. A race between a `resolve()` pass reading state and a
response landing at nearly the same instant is the same class of problem any
concurrent system has, independent of this feature.

---

## 13. Explicitly out of scope (v1)

- Multi-provider/multi-resource reassignment.
- Mandatory buffer/cleanup time between appointments.
- Resizing appointment durations.
- Rescheduling recurring series as a whole (only single-instance moves).
- Actual delivery mechanics of notifications — delegated to `NotificationPort`.
- Cost model tuning (§10) — deliberately deferred.
- API surface finalization (§11) — deliberately unreviewed.
