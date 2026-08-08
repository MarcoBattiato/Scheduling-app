# mock_ui — SPEC.md

A throwaway front end for driving `scheduling_engine` and `calendar_store` by
hand. Its job is to make the system **playable**: open a few browser tabs, act
as different people, and accumulate enough real history to judge whether the
scheduling behaves sensibly — particularly the habitual-slot anchoring that
`scheduling_engine/SPEC.md` §10.1 defers until there is history to check it
against.

Not a product. Not a design for one. Where a decision here differs from the
engine's spec, the engine's spec wins and this is the thing that changes.

---

## 1. What it is for

- Exercise the engine through its real API, not a mock of it.
- Generate artificial history — bookings, cancellations, reschedules — with
  correct `origin` provenance, so the anchoring work has data.
- Make the negotiation loop visible: a client sees a rescheduling request
  addressed to them and answers it.

Explicitly **not** goals: authentication, multi-provider, styling worth
keeping, mobile layout, or any persistence design intended to survive.

---

## 2. Shape

One Python process holds one `AvailabilityStore` and one `World`. Every browser
tab talks to it over HTTP, so several tabs share one database and one engine —
that is the whole reason this is a server rather than a static page.

```
browser tab (client: alice)  ─┐
browser tab (client: bob)    ─┼──▶  mock_ui HTTP server  ──▶  scheduling_engine
browser tab (provider)       ─┘         (one World)      ──▶  calendar_store
```

Tabs poll for state on a timer. No websockets: polling is a few lines, and
staleness of a second does not matter for something driven by hand.

---

## 3. Roles

Chosen by URL — `/?as=alice` is a client view, `/?as=provider` is the
provider's. No login; the role is a claim, not a credential.

### Client can

- Set their weekly availability (the grid), which is what constrains where one
  of their bookings may be **moved to**.
- See their upcoming and past bookings.
- Ask for a booking: duration plus one or more desired windows.
- Cancel a booking, or ask to move one.
- **Answer a rescheduling request** — the provider's scheduler wanting to move
  one of their appointments to fit someone else in.

### Provider can

- See the whole schedule, and each client's availability and history.
- Run the scheduler.
- See which rescheduling requests are outstanding and what each unblocks.
- Set their own availability, and block time as `provider-self`.

---

## 4. When the scheduler runs

**Never on submission.** A request arriving is not a reason to re-plan the
week; it is a reason to consider re-planning at the next point the provider
has said they want to look. Firing on every request would also make the
calendar churn under clients who are watching it.

`policy.py` decides, as a pure function of (policy, clock, queue) so the
question "why did it run?" always has an answer and so it can be tested
without waiting for real time. Four ways in:

1. **Weekly runs** at configured (weekday, time) marks — "plan the week on
   Monday at 08:00".
2. **Urgency**: a waiting request whose window opens within
   `urgency_hours` cannot wait for the weekly run, so it earns one.
3. **Retry**: unsatisfied work is reconsidered after `retry_after_minutes`,
   so a queue that could not be solved neither spins nor sits forever.
4. **The provider**, explicitly — including immediately after rejecting a
   proposal.

Evaluated on each state poll, which is how a timed run happens without a
background thread.

### 4.1 ON_HOLD

A request the engine could not place is **parked**. Parked requests still take
part in runs that happen for other reasons — being parked is not being
abandoned — but they no longer *trigger* runs of their own.

That distinction is the whole point. Without it, an unplaceable request whose
wanted date is approaching satisfies the urgency condition on every single
tick, and the scheduler runs forever, achieving nothing each time.

## 5. Who agrees to what

The engine does not model negotiation yet — `AcceptedChange`, cold-asks and the
`PROPOSED → CONFIRMED → EXERCISED` lifecycle of engine SPEC §6–§9 do not exist.
`solve_placements` returns a *plan*: placements, plus displacements it would
like to make. Nobody has agreed to any of it.

So this mock owns the loop:

1. The scheduler runs and produces a **draft** plan. Nothing is written.
   Several drafts may sit side by side under different optimiser settings —
   proposing costs nothing because nothing is reserved — so the provider can
   compare arrangements rather than be told about one. Each draft carries what
   it was asked for (`alpha`, `max_displacements`) and what it achieved
   (booked, unplaced, moved, waste, delay).
2. The **provider** approves it, in whole or in part — meaning it is worth
   asking about, not that it has happened. Still nothing is written.
   Approving one draft discards the others, which were computed against a
   calendar this one is about to change.

   A part that rests on a move is pulled in with it: sending on a booking
   while holding back the move that frees its slot would promise something
   that cannot happen. The dependency comes from the engine
   (`Placement.depends_on`), so it is followed transitively rather than
   guessed from overlapping times. It runs one way only — a move may be sent
   on without the booking that wanted it.
3. Each affected client is asked about their own part: a new booking, or a move
   of one they already have. Both are the same question — "is this time all
   right?" — so both are the same object.
4. Each answer is applied **on its own** (engine SPEC §7.4). One client
   declining does not undo what another has already agreed to.

The single thing that cannot be independent is a booking that only exists
because somebody was going to vacate the slot. The engine reports placements
and displacements separately with no link between them, but with chains
forbidden a placement can depend on at most one move — the one whose old slot
it overlaps — so the dependency is derived rather than requiring an engine
change.

### 5.1.0 Two gates, not one

A client agreeing does **not** change the calendar. Their answer is recorded;
the provider then decides what to do with the answers as a whole, because a
half-applied rearrangement is often worse than none — three people moved so a
fourth could be booked, and then the fourth says no.

So a plan out with the clients accumulates agreement and changes nothing, and
the provider settles it in one of three ways (`World.settle_plan`):

| how | applies | outstanding asks | plan ends |
|---|---|---|---|
| `agreed` | everything agreed and unblocked | left outstanding | still `awaiting_clients`, or `applied` if none remain |
| `agreed_only` | everything agreed and unblocked | **withdrawn**; requests go back to `on_hold` | `applied` |
| `reoptimise` | nothing | **withdrawn**, agreed ones too | `rejected`, and the scheduler runs again |

`agreed` may be called repeatedly as answers trickle in. Application respects
the engine's dependencies and is a loop rather than a sorted pass: writing one
move down can unblock another, chains included, so it repeats until nothing
more moves.

An agreed change stays **on the calendar** until it is written down, drawn in
the proposed colour with a solid outline rather than the dashed one an
unanswered question gets, and the alert stays up saying whose wait it now is.
Dropping it from view the moment a client says yes would leave them unable to
tell "it went through" from "it was dropped".

A slot counts as spoken for from the moment it is offered until it is written
down — `pending_holds` includes accepted-but-unapplied asks. Handing an agreed
hour to somebody else while the provider is still deciding would be worse than
never having asked.

### 5.1.1 Three answers, not two

Being asked to **move** has three honest answers, and the difference between
the last two is worth a great deal to the scheduler:

| answer | what it says | what it does |
|---|---|---|
| accept | yes | applies the move |
| decline | "not that time" | blocks that slot on that date; the appointment stays movable |
| refuse | "not at all" | pins the appointment; it is never offered up again |

A rejected slot becomes a single-date block on that client's availability
(engine SPEC §9): saying no to next Tuesday at three says nothing about
Tuesdays in general. That single statement is the whole of a decline.

Pinning is the extra thing a *refusal* says, and it is the client's choice
rather than something inferred. Inferring it — the earlier behaviour — made the
calendar seize up: every "not Tuesday at three" permanently removed an
appointment from consideration, so the search space only ever shrank.

An **offer** of a new booking has only the first two answers. There is nothing
to refuse to move.

Whichever of the two rejections is given, this move is not happening *now*, so
everything resting on it falls through — including asks still out with other
clients, which are **withdrawn**. A slot that was only going to be free because
somebody was going to vacate it is not a real question any more, and leaving it
outstanding would both ask a client to confirm the impossible and stop the plan
ever settling.

### 5.1.2 A client asking to be moved

Distinct from a client who has found a slot themselves (`move_appointment`).
Here they say only "not this time, ideally around then" and the scheduler
looks, so it goes through the queue as an ordinary request and comes back to
them as an offer. Either way `Origin.CLIENT`: they asked to move, so wherever
they land is a choice of theirs.

The one decision they have to make is whether to give the slot up now:

- **release it** — it frees for everybody else immediately, and they carry the
  risk of no replacement being found.
- **keep it** — `Request.replaces_appointment_id` links the two, and the old
  booking is cancelled *at the moment the replacement is booked*, never before.
  They cannot end up with neither. The hour stays blocked meanwhile, and the
  replacement must therefore be found elsewhere.

A booking on its way out is not offered up for displacement, and a booking we
have asked about cannot be moved by its client until they have answered —
otherwise the replacement would try to cancel an appointment the accepted move
had already superseded.

### 5.1.3 What a run is about

A run has a *scope* as well as a horizon, and they are not the same thing. The
horizon bounds where an appointment may be put; the scope decides which
requests are in play at all.

Without a scope, the horizon quietly does both jobs and does the second badly.
A client who asked for a date next month has their availability cropped to the
coming week like everybody else, so the earliest free slot is not merely a poor
match for their wish — it is the only slot there is, and they get booked into
next week. That is not the optimiser being eager; it is the wish having been
made unreachable before the optimiser saw it.

So:

- `World.propose(request_ids=[...])` runs over exactly those requests. This is
  the provider saying what this run is about.
- With none named, `SchedulingPolicy.scope_to_horizon` (on by default) keeps
  only requests whose wished-for time falls inside the window. Inclusive at the
  near end — an overdue request is the most in-scope thing there is.
- Turning it off restores the old behaviour, deliberately reachable, because a
  provider with an empty week may well prefer to fill it early.

A request left out is left **alone**: it keeps its status rather than being
parked, because being parked means *tried and not placed* and this one was
never tried. `metrics.out_of_scope` records how many, so a plan that looks
light because half the queue was not in it does not read as a quiet week.

### 5.2 Planning around what is already promised

A slot out with a client is neither free nor booked, and the scheduler must
not offer it to somebody else while they think. This needs no reservation
mechanism: `solve_placements` is a pure function of the world it is described,
so a promised slot is simply described as taken (`World.pending_holds`).

Three things follow, and they are the same mechanism seen from different
angles:

- **Re-planning while an answer is outstanding is safe**, so the earlier
  "one plan out with the clients" rule is gone.
- **Lock-and-reoptimise is not a separate feature.** Approving part of a plan
  makes those slots holds; the next run works around them. Locking and
  promising differ only in why the time is spoken for.
- **Both ends of a pending move matter**, though only one needs work here. The
  destination is held explicitly; the origin is still occupied by the booking
  that has not moved yet, so `free_time` already excludes it. What the origin
  needs instead is that the appointment stops being offered as movable, or the
  same client would be asked twice about it.

The price is that an unanswered ask sterilises capacity — which is what makes
expiry (§7) matter rather than being a nicety.

---

## 5.1 Writing history correctly

The reason `calendar_store` retains cancelled and superseded rows is that
history is evidence for anchoring, and the provenance matters (see engine
SPEC §10.1). This mock is the thing generating that history, so it must get
`origin` right:

- A booking the client asked for → `Origin.CLIENT`.
- A move the client requested → `Origin.CLIENT`.
- A move the scheduler imposed and the client merely accepted →
  `Origin.DISPLACED`.

The third is the one that is easy to get wrong. An accepted displacement is
still a displacement: the client agreed to it, but they did not *choose* the
slot, and recording it as a preference is exactly the failure mode the
provenance field exists to prevent.

---

## 6. Persistence

A snapshot to JSON, restorable on startup, so a session's accumulated history
survives a restart.

**This reaches past `calendar_store`'s public API** and serialises its internal
lists directly. That is a deliberate shortcut for a mock: real persistence
belongs inside `calendar_store` and does not exist yet, and reconstructing
state through the public API would not preserve appointment ids, which the
`supersedes` chain depends on. It is confined to one module so it is obvious
what to delete later.

---

## 7. Known crudeness

Recorded so nobody mistakes any of it for a decision:

- A *refused* move pins that appointment for the rest of the session: nothing
  lifts it, not even the client later freeing up. Deliberate for now — it is
  what the client said — but there is no way back short of a reset.
- Nothing caps how many times a client may be asked. Each decline eats one slot
  of their availability, so it is self-limiting, but only eventually.
- No expiry on an outstanding ask, so an unanswered one holds its slot
  indefinitely. Defaults agreed for when real time exists: a booking within the
  client's preferred times is accepted by default after 1 day if the
  appointment is more than 5 days away, or 6 hours otherwise; a rescheduling
  request defaults to **declined** after 6 hours. All provider-configurable.
  The asymmetry is deliberate — silence should not cost somebody an
  appointment they already hold.
- `alpha` and `max_displacements` are global controls, not per-provider
  settings.
- No expiry on a client's approval: a client who never answers holds their part
  of a plan open indefinitely.
- No validation that a client's requested window is one they are available
  for — the engine deliberately does not enforce that either, since a request
  states what the client wants regardless of their standing availability.
- Reschedule bounds are one global default rather than per client.
