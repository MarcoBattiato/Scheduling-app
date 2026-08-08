# mock_ui

A throwaway browser front end for driving `scheduling_engine` and
`calendar_store` by hand. See SPEC.md for what it is for and what is
deliberately crude about it.

Run it:

```bash
mock_ui/run.sh          # --port, --reload
```

Or in VS Code: **Run and Debug** panel (Cmd+Shift+D) → pick *"Mock UI: start
the server"* → F5. That configuration is file-independent, unlike the Playground
ones which run whichever `.json` is open.

**If it exits the instant you start it, it is the interpreter.** `python -m
mock_ui` under a Python without the packages installed prints `No module named
mock_ui` and returns immediately, which reads as a crash. `run.sh` picks the
venv itself, and every launch configuration pins `"python"` for the same
reason — relying on VS Code's selected interpreter has been the cause every
time this has happened.

Then open a tab per person — `?as=alice`, `?as=bob`, `?as=provider`. **All tabs
share one server process, hence one store and one engine**; that is the entire
reason this is a server rather than a static page. Tabs poll every 2.5s, so one
tab's action shows up in the others.

Layers, and the rule that keeps them apart:

- `state.py` — the `World`: every behaviour lives here, and it is the only
  module that knows both packages.
- `app.py` — HTTP only. If a route contains logic, it is in the wrong file;
  the flow tests drive `World` directly and never go through HTTP.
- `persistence.py` — session save/load. **Deliberately reaches into
  `calendar_store`'s private lists**, because real persistence does not exist
  yet and rebuilding through the public API would issue fresh appointment ids,
  breaking the `supersedes` chain. Confined to this one file so it is obvious
  what to delete later.

  It saves the **workflow** as well as the calendar — pending requests, drafts
  awaiting approval, approvals awaiting an answer, refusals, and the scheduler
  policy. A session restored with the bookings but an empty queue would be
  missing most of what there is to play with. Saving happens automatically
  after any successful mutation; the file lives beside the package rather than
  beside the shell, so a restart cannot silently start empty because you
  launched from elsewhere. **Reset deletes it**, since a reset that undoes
  itself on the next restart is not a reset.
- `static/calendar.js` — a week-column calendar drawn from a plain
  description: availability to tint, blocks to place, ghosts for where
  something used to be, arrows between them. The same component renders the
  real schedule and a proposed one, which is the point — a proposal is only
  legible against the calendar it would land in.
- `static/app.js` — state, panels, actions. No build step, no framework.

Availability is edited in two places on purpose, mirroring calendar_store's own
split: the side grid is the **weekly pattern** (generic weekdays, what every
week looks like), and dragging on the **dated calendar** overrides a *single
date* — shift-drag marks that time away. Overrides are listed as chips under
the calendar with a clear button, because otherwise they are invisible once
made: you can see their effect on the tint but not what caused it.

Note an exception matching the weekly pattern normalises away to nothing.
calendar_store stores only real deviations, so "make me available when I
already am" correctly does nothing at all.

New clients get the provider's weekly hours by default. A client with no
availability can be *booked* — a request states what they want regardless — but
can never be offered a *rescheduling*, so a calendar populated with such clients
would quietly never exercise displacement.

## The thing most worth not breaking

This mock is what generates the artificial history that
`scheduling_engine/SPEC.md` §10.1 (habitual-slot anchoring) will be judged
against, so **`origin` has to be right**:

- a slot the client asked for, or moved themselves to → `Origin.CLIENT`
- a move the scheduler imposed and the client merely accepted →
  `Origin.DISPLACED`

`provider_away` is the newest way to get this wrong. A client rehoused because
the provider fell ill will accept the slot they are offered, and the booking is
`Origin.DISPLACED` from the moment it is made (`Request.origin` carries it
through the queue) — the old slot is cancelled by the *provider* too, since
they did not give their hour up. `place_manually` is the opposite case and
stays `CLIENT`: the request was theirs, and the provider choosing the hour is
no different from the solver choosing it.

The second is easy to get wrong. An accepted displacement is still a
displacement — the client agreed, but they did not *choose* the slot, and
recording it as a preference is precisely the failure the field exists to
prevent. `test_flows.py` pins this.

A request names a **service** and a **wished-for time**. Where a client may
actually be booked is their availability, resolved afresh at every solve — the
same constraint that decides where one of their bookings may be moved to. The
wish is only a cost, so naming a slot cannot make someone unplaceable elsewhere
and cannot turn a narrow ask into a claim on an hour somebody else holds. The
wish is stored on the booking, so a later reschedule is judged against what the
client wanted rather than wherever they were last put.

A consequence worth knowing: a client with no availability cannot be booked at
all now, where previously the ask itself supplied the window. Deliberate for the
moment — surfacing it to the provider as something to chase is left for later.

The **planning horizon** (`SchedulingPolicy.horizon_days`, a week by default,
settable in the header) crops everyone's availability from the day a run
happens, so it bounds the whole problem rather than just the answer. It is also
the dominant cost: a request may be placed anywhere in its client's
availability, so the horizon multiplies candidate slots for every request in the
queue — 7 days ~2s against 21 days ~11s at 25 clients and 20 requests.
`World.snapshot` deliberately reaches further (four weeks) so the calendar view
does not go blank beyond the planning window.

The horizon bounds *where* an appointment may go; **`scope_to_horizon` decides
which requests are in play at all**, and the two are not the same. Without the
second, a client wishing for a date next month has their availability cropped
to the coming week like everyone else, so the earliest free slot is the only
slot and they are booked into next week — the wish was made unreachable before
the solver saw it. `propose(request_ids=[...])` overrides both directions. A
request left out of a run keeps its status rather than being parked: parked
means tried and not placed.

The scheduler **never runs on submission** — see `policy.py` and SPEC.md §4.
It fires on weekly marks, on urgency, on a retry timer, or when the provider
says so, and the decision is a pure function so it is testable without waiting
for a clock. A request the engine cannot place is parked (`on_hold`): still
reconsidered by runs that happen anyway, but no longer triggering runs of its
own, which is what stops an unplaceable request whose date is approaching from
firing the urgency trigger forever.

## Negotiation is owned here, temporarily

The engine has no negotiation lifecycle yet, so `solve_placements` returns a
plan nobody has agreed to. The scheduler produces a draft; the provider approves it (which only means it
is worth asking about); each affected client is then asked about their own part
and **each answer is applied on its own**, per engine SPEC §7.4.

**Agreeing is an answer, not an action.** A client accepting records agreement
and writes nothing; `World.settle_plan` is where a plan becomes a calendar, and
the provider chooses between applying what is agreed, applying it and dropping
whoever has not answered, or rejecting the lot and re-planning. Two gates,
because a half-applied rearrangement is often worse than none. A slot stays
held from the moment it is offered until it is written down.

Being asked to move has **three** answers — accept, *decline* ("not that time",
blocks the slot, stays movable), *refuse* ("not at all", pins the appointment).
Only a reschedule can be refused; an offer has nothing to refuse to move.
Collapsing decline into refuse was the earlier behaviour and it made the
calendar seize up, so do not reintroduce the inference: only the client knows
which they mean.

The one thing that cannot be independent is a booking that only exists because
somebody was going to vacate the slot. **The engine reports that link**
(`depends_on`), so this follows real dependencies rather than guessing from
overlapping times, and follows them transitively — with chains, a move can
itself be waiting on another. When a move falls through, everything resting on
it is withdrawn, whether or not those clients had already answered.

When the engine grows `AcceptedChange`, this should lose the responsibility
rather than keep a second implementation of it.

Run tests: `../scheduling_engine/.venv/bin/pytest tests/` (this package shares
the engine's venv — one interpreter can import all three).
