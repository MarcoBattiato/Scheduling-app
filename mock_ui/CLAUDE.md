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
- `control.py` — **the gate**: the one way a command is meant to reach the
  World. Untrusted JSON → a typed `scheduling_contracts` command
  (`build_command`, strict) → identity from the `Session`, never the command →
  owner looked up (`owner_of`) → `scheduling_contracts.decide` → run, draft,
  or refuse. A refusal of someone else's thing reads exactly like "no such
  thing". "Needs confirmation" executes nothing: the command waits behind a
  single-use, expiring token with a summary written *by the server*
  (`describe`), and only the person, outside the assistant, can `confirm`.
  What an assistant gets back is cut down (`_strip`): a client's sees no
  identifiers at all — its tools have no `client_id`, which the gate always
  fills with "you" — and the provider's sees per-conversation pseudonyms and
  can only act through ones it was shown. Every attempt goes to
  `World.audit`. The screens do not go through the gate yet; when they do,
  `app.session_for` (today: the "as" claim) is where sign-in plugs in.
  `tests/test_gate.py` checks the guarantees across every handled command —
  and fails if the gate is made to act instead of draft, or to leak names.
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
- `static/phone.{html,js,css}` — the phone front end at `/phone`, over the
  same API. A separate page rather than a responsive version of the desktop
  one, because the desktop page is built on hover and drag, and neither exists
  on a phone. Views are pure functions from state to HTML; every tap goes
  through one delegated handler keyed on `data-act`, so a view can be rendered
  in Node (`tests/test_phone.py`) and a button with no handler fails a test.
  It polls like the desktop page but **redraws only when the state changed,
  never while a field has focus, and carries form values across a redraw** —
  without all three, a poll every 2.5s resets whatever is being typed.
  Settling a plan shows an agreed booking that is still waiting on somebody
  else's move as *waiting*, not *ready*; the server would hold it anyway, and
  a button promising to confirm it would confirm nothing.

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

**Changing availability never reschedules anybody.** Stranding is a query
(`orphaned_appointments`), not an event: bookings left in time the provider no
longer has are flagged and stay put, and acting on them is always explicit —
either the provider asks (`rehouse_orphans` / `cancel_orphans`) or they have
turned on `SchedulingPolicy.rehouse_orphans_on_run`. A run reports them either
way. One mistyped date must not put half the week through a rescheduling
nobody asked for.

`provider_away` is the newest way to get the *origin* wrong. A client rehoused because
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

**Repeating bookings** (`Series`, `start_series`) are standing *requests*, not
block bookings. Each session is raised as an ordinary request once it comes
inside the planning window — on every `tick`, idempotently, tracked in
`Series.raised` — and goes through the scheduler and the client's agreement
like any other. Booking a year of Tuesdays up front would hold those hours
against everyone else without the scheduler ever weighing them. A withdrawn
session stays withdrawn (it is in `raised`, so it is not raised again); a
session in time the provider has taken off is skipped, not raised; and one not
placed before its day **lapses** (`Request.status = "lapsed"`) rather than
being made up in a later week, where it would stack on that week's own session
— a one-off request, by contrast, stays queued until placed;
stopping a series withdraws what is not yet booked and leaves what is. The
series' slot is a real client preference — `Origin.CLIENT`, and exactly the
habitual slot SPEC §10.1 wants to learn.

**How often a client may be booked** is per client (`Client.per_day`, default
1; `Client.per_week`, default none), set by the provider and enforced by the
engine as a hard constraint (`ClientLimit`). The engine sees fixed bookings
only as time subtracted from the provider's day, with no name on them, so
`_client_limits` has to hand it every session it cannot move: bookings, offers
still out, and the earlier part of the week the planning window does not
reach. A booking being asked to move counts where it is going, not where it
is. A day already over the limit is left alone — nobody is moved to satisfy
it — and a repeating session is skipped, not raised, in a week already full.

**Calendar sync** (`calendar_sync.py`): `events_for(world, person)` is the one
place that decides what belongs in someone's calendar app — settled bookings
only (an offer is not an event), identity by the root of the `supersedes`
chain so a move updates one event instead of cancelling it, and client events
titled only "Appointment — {practice}" because their calendars are shared.
Push channels will send `changes(before, after)` rather than hook every place
a booking can change. The subscription feed is `/calendar/{token}.ics`; the
token is the only credential a calendar app can present. **Links are made only
by a POST** (`/api/calendar/{person}`), never during a read: the session is
saved after POSTs, and a link handed out but not saved would change on
restart and break every calendar subscribed to it.

**Money** (`billing.py`; plan stages 1–2). The app keeps its own ledger —
`Charge` (owed) and `Payment` (received) — and a processor only moves money.
A session is charged when the provider marks it attended; a no-show or a
client's late cancellation **proposes** a fee, and nothing is owed until the
provider confirms it. Fees key off facts already recorded: only
`cancel_appointment(by=CLIENT)` can propose one, and both move paths pass
`as_move=True`, so neither the clinic cancelling or moving someone nor a
client moving their own booking is ever charged. A payment counts only when
`check_payment` has asked the processor — the redirect back is never trusted.
`SimulatedProcessor` (default; its "hosted page" is `/pay/simulated/…`) and
`StripeProcessor` (Checkout, chosen when `STRIPE_SECRET_KEY` holds a **test**
key; a live key is refused) share one interface. Descriptions sent to the
processor and shown to clients carry no service name. GST is off by default;
when on, prices *include* it and the included part is recorded.

**The clinician chat** (`World.messages`, commands `message.*`) is the first
feature built on the gate from the start: the phone app reaches it only
through `POST /api/commands/{name}?as=…`, so its permissions are the
contract's. It is clinical, so it is deliberately **not** in `snapshot()`
(which anyone can read today) and not in the activity log beyond "X wrote in
Y's conversation"; each person fetches their own conversation through the
gate. Who wrote a message is derived from the session. Every `message.*`
command is FORBIDDEN to assistants, reads included — the assistant's
hand-off into the chat is a separate path, still to be built. The practice's
"reply expectation" and "emergency note" are shown above every client's
conversation, and the emergency note cannot be blanked.

**The assistant** (`assistant.py`) is a model — Claude (`ClaudeBrain`) or a
free local one through Ollama (`LocalBrain`), chosen by `ASSISTANT_PROVIDER` —
with the gate's tools and nothing else. A brain only knows how to talk to its
model; everything that keeps it safe lives in `Assistant` and is shared: every tool call is `Gate.execute` as the person via the assistant
channel, so changes come back as drafts and the model is told "prepared, not
done" — never given the token. The provider's typed client names are swapped
for the conversation's pseudonyms before sending and back on the way out.
History sent to the model is append-only (thinking blocks are bound to it);
the current time goes in a mid-conversation system note so the cached prefix
survives. Distress is `offer_clinician_handoff`, which only shows the client
a button; `Assistant.handoff` sends *their own words* to the clinician chat
when they press it, flagged server-side so the provider sees it may need
attention. The person can also send it themselves at any time ("Send this to
my clinician"), and a server-side phrase check (`DISTRESS`) shows the
hand-off card whatever the model does — a model noticing is not reliable
enough to be the only way to a person. Each client message carries their own
diary (`_client_context`: upcoming appointments with real ids, waiting
requests, open questions), read through the gate exactly as the tools would;
without it a small local model guessed date ranges and invented ids. **A client's assistant can do only what the client's own screens can,
over data the client can already see** (`CLIENT_SCREENS` in `test_gate.py`
pins the list). It cannot see the practice's diary, so it must never say a
time is free or would clash — it sends a request and says the practice will
offer a time. A tool answering "is Tuesday 13:00 free?" would leak other
people's bookings however it were worded; that is a privacy decision, not a
missing feature. The gate also refuses a booking for an unknown service before
drafting, so no card is shown that can only fail. Money (`charge.*`) is
forbidden to every assistant, reads included. Off
until the practice offers it; each client opts in after the notice. Conversations are in memory only. Model and effort from
`ASSISTANT_MODEL` / `ASSISTANT_EFFORT` (default Opus 5.5, medium), with
server-side refusal fallbacks on. `tests/test_assistant.py` replaces Claude
with a script and checks what leaves the practice.

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

A booking can need **more than one** move, and is written down only once all of
them are — every dependent goes through `_apply_approval`'s gate, never straight
to `_book`. The example calendars only ever need one move per booking, which is
how writing it down after the first of two went unnoticed;
`test_dependent_moves.py` runs every answer order. A booking that falls through
is withdrawn (final, so no later settle revives the "yes"), and any move made
only for it and not yet written down is withdrawn with it. Withdrawing a
request does the same to its outstanding offer.

When the engine grows `AcceptedChange`, this should lose the responsibility
rather than keep a second implementation of it.

Run tests: `../scheduling_engine/.venv/bin/pytest tests/` (this package shares
the engine's venv — one interpreter can import all three).
