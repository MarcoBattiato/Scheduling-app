# Scheduling app

A constraint-solver scheduling engine for a single-provider appointment calendar.
It takes a queue of pending booking requests and decides, **jointly**, where all of
them go — and when the calendar is too full to fit them, it works out which
already-agreed bookings could move to make room. Placing a queue one request at a
time does not work here, because free time is a shared resource: two bookings
landing in the same open block interact, and the cost of one depends on where the
other went. So the whole queue is expressed as a single CP-SAT model
(Google OR-Tools) and solved in one pass, with a lexicographic objective that
fills as many requests as possible first, disturbs as few settled clients as
possible second, and only then trades off calendar fragmentation against how far
everyone landed from the time they actually wanted.

The interesting part is what happens when a change invalidates existing bookings.
The engine may **relocate** an already-accepted appointment, but it may never
**cancel** one to relieve pressure on someone else's request — a booking either
stays or moves (`stay[A] + Σ y[A,t] == 1`), and cancellation is simply not in the
solver's action space for a third party. Displacement is off by default, and when
enabled it is a genuine last resort rather than one option among many: because
"accepted bookings disturbed" is its own objective tier above the cost blend, any
arrangement that fits a request without moving anybody always wins. A hard ceiling
(`max_displacements`) sits on top of that, which is both a business limit and the
main thing keeping the solve fast.

Conflicts are resolved by making both sides of the contest pay the same currency.
An earlier version made a request's asked-for window a *hard* constraint while an
existing booking could be relocated anywhere in its owner's much wider
availability — and that asymmetry was exploitable. Naming a single slot made a
request impossible to place anywhere else, so a newcomer with a narrow ask could
evict a settled client almost for free, and that client would then be repositioned
using availability the newcomer never had to offer. Now availability bounds both
sides and preference costs both sides: where a client *may* be booked is a hard
constraint, where they would *like* to be is only a cost. A booking carries the
slot it was originally made against, so a later move is judged against what its
owner actually wanted rather than against wherever the solver last put them — which
means a booking already displaced can be pulled back toward its preferred slot at
no cost.

---

## What the system does

A provider (one person, one calendar) offers services of known durations. Clients
have their own availability and submit requests for an appointment, optionally
naming a time they would like. Requests accumulate in a queue. Periodically — or
when the provider says so — the scheduler runs, and the engine proposes a plan:
which requests get placed where, which existing bookings would need to move, and
which requests could not be fitted at all.

**A partial solution is a valid answer, not a failure.** Leaving requests unplaced
is an expected outcome and is reported as such.

Nothing in that plan reaches the calendar until the people affected agree to it.
Displacing someone is a *proposal*, and the negotiation around it — the provider
approving a draft, each affected client answering for their own part, and each
answer being applied independently — is currently handled by the mock UI rather
than the engine (see [Current state](#current-state-and-known-limitations)).

---

## The scheduling model

### Hard constraints

| Constraint | Meaning |
|---|---|
| Provider free time | Availability with existing appointments already subtracted. Candidate slots exist only inside it. |
| Client availability (`desired`) | Where a client may be booked at all, cropped to the planning horizon. |
| No double-booking | Standard disjunctive resource constraint over the provider's single calendar. |
| Stay-or-move | Every movable booking satisfies `stay + Σ moves == 1`. It is never cancelled to make room. |
| Reschedule bounds | How far an *involuntary* move may shift a booking, per client. `provider-self` blocks use `{0, 0}`, so a lunch break can never be pushed to another day. |
| `max_displacements` | Hard ceiling on how many accepted bookings a single solution may disturb. Defaults to `0` (displacement disabled). |
| Chains (off by default) | A displaced booking may not take a slot another displaced booking is vacating, so each new placement depends on **one** client agreeing rather than a sequence of them. |

### The objective, in order

1. **Maximise requests placed.** Partial solutions are valid.
2. **Minimise accepted bookings disturbed.** This tier is what makes displacement a
   last resort rather than merely another move.
3. **Minimise `alpha · fragmentation/F + (1 − alpha) · preference_gap/E`.**

`alpha` is a provider-facing slider: `1.0` packs the calendar tightly,
`0.0` books everyone as early as they can go. Both terms are measured in minutes
and normalised (by one service-length of waste and one day of delay respectively)
so that `alpha` genuinely *mixes* the two rather than merely ranking them — raw
fragmentation runs to minutes-and-hours while earliness routinely runs to days.

**Fragmentation is derived from the service catalogue, not from round numbers.**
`waste(gap) = gap − the most service-time still packable into it`. A 30-minute
hole is waste only because nothing in the catalogue fits it; adding a 45-minute
service changes the cost model with no constants to chase.

**The preference gap** is the total distance between where things landed and where
they were wanted. Where no preference was stated, the earliest feasible slot stands
in — which reproduces "book as early as possible" exactly.

### Dependencies between moves

A placement that only fits because somebody else is vacating the slot reports that
link explicitly, as `Placement.depends_on` / `Displacement.depends_on`. This is
reported rather than left to be inferred: a consumer could guess by comparing
ranges for overlap, but that is only correct while chains are forbidden. With
chains enabled a placement can rest on a sequence of moves that overlap reveals
nothing about.

### Provenance, and why it matters

`calendar_store` records whether a slot was **chosen by the client** or **imposed by
the scheduler** (`Origin.CLIENT` vs `Origin.DISPLACED`), keeps cancelled rows rather
than deleting them, and links a rescheduled booking to its predecessor through
`supersedes`. This is not audit-trail instinct. The planned habitual-slot anchoring
work needs to learn a client's usual time from their history, and without that
distinction the engine would launder its own rescheduling into the client's
apparent preferences — learning where it put people rather than where they wanted
to be. Cancellations likewise record *who* cancelled, because a client dropping a
slot is evidence about that slot while the provider closing a day says nothing
about the client.

---

## Architecture

Three independent Python packages, each with its own `pyproject.toml`, tests, and
specification documents.

```
calendar_store  ──▶  scheduling_engine  ──▶  mock_ui
   (state)              (decisions)          (interface)
```

### `calendar_store` — what is true about the calendar

Availability storage and booked appointments. Availability is held as
**effective-dated recurring rules plus single-date exceptions**, materialised on
query into interval algebra (via `portion`) and handed out at the public boundary
as a flat `list[TimeSegment]`. Consumers never see a rule or an exception, only the
resolved result.

Appointments are stored separately from availability and are **never deleted**:
cancelling sets a status, rescheduling writes a new row linked to the old one. It
also holds the `ServiceCatalogue`, where services are deactivated rather than
removed, so an appointment booked against a discontinued service stays readable and
invoiceable.

Deliberately, it **does not subtract appointments from availability** — that netting
is the consumer's job, which keeps the store's contract simple.

Public contract: [`calendar_store/INTERFACE.md`](calendar_store/INTERFACE.md).
Internals and rationale: [`calendar_store/SPEC.md`](calendar_store/SPEC.md).

### `scheduling_engine` — what should happen

Pure functions over `TimeSegment`s; no store, no I/O, no database.
`availability.free_time` performs the netting step the store declines to do, and
`solve_placements` is the CP-SAT model described above. `fragmentation.py` derives
gap waste from the service catalogue, and `visualize.py` renders a calendar and
what the solver did to it — its gap report is built from the solver's own
grid-snapped blocks, so the chart can never disagree with the numbers.

Design and open questions: [`scheduling_engine/SPEC.md`](scheduling_engine/SPEC.md).

### `mock_ui` — a way to drive it by hand

A deliberately throwaway FastAPI server with a no-build-step vanilla-JS front end.
One process holds one store and one engine; you open a browser tab per person
(`?as=alice`, `?as=provider`) and tabs poll for shared state. It exists to make the
system *playable* — to accumulate realistic history with correct provenance, and to
make the negotiation loop visible.

It is explicitly **not** a product: no authentication (the role is a URL claim, not
a credential), no styling worth keeping, no persistence design intended to survive.

Notes on what is deliberately crude: [`mock_ui/SPEC.md`](mock_ui/SPEC.md).

---

## Running it

Requires **Python 3.9+**. The one external solver dependency is `ortools`.

The tooling (`mock_ui/run.sh`, the VS Code launch configurations) expects a single
shared virtualenv at **`scheduling_engine/.venv`** that has all three packages
installed. One interpreter can import all three:

```bash
git clone https://github.com/MarcoBattiato/Scheduling-app.git
cd Scheduling-app/scheduling_engine

python3 -m venv .venv
.venv/bin/pip install --upgrade pip          # see note below
.venv/bin/pip install -e ../calendar_store -e . -e ../mock_ui
.venv/bin/pip install pytest httpx           # for the test suites
```

> **The `pip install --upgrade pip` step is not optional.** These packages are
> `pyproject.toml`-only, and pip versions older than 21.3 reject editable installs
> of them with *"A `pyproject.toml` file was found, but editable mode currently
> requires a setup.py based build."* A stock `python3 -m venv` on an older
> interpreter can easily ship a pip from before that.

### The browser front end

```bash
./mock_ui/run.sh            # --port, --reload
```

Then open a tab per person: `http://127.0.0.1:8000/?as=provider`,
`?as=alice`, `?as=bob`. All tabs share the one server process, hence one store and
one engine, so an action in one tab shows up in the others within a couple of
seconds.

### The solver playground

The engine can be driven directly from a JSON scenario file, with no UI involved:

```bash
cd scheduling_engine
.venv/bin/python -m scheduling_engine.playground examples/scenario.json --text --open
```

This renders a browser-viewable chart of every object involved — provider
availability, existing bookings, the free time that leaves, each request's window,
where it landed, and the gaps left over. Passing `--alpha 0,0.5,1` renders one run
per value side by side, which is the quickest way to see the packing-versus-earliness
trade-off. `--text` also prints an ASCII chart:

```
           |09 |10 |11 |12 |13 |14 |15 |16
  free     ████████····████████████████████
  placed   aaaaaa······bbbbccccdddd········

           a  09:00–10:30   90m  r4 (dana)
           b  12:00–13:00   60m  r1 (alice)
           c  13:00–14:00   60m  r5 (erik)
           d  14:00–15:00   60m  r6 (marco)

           gap 10:30–11:00   30m  30m wasted
           gap 15:00–17:00  120m  reusable

placed 6/7 · unplaced: r7 · fragmentation 30m · off-preference 750m
```

Scenario file format is documented by `--help` and in
[`scheduling_engine/examples/README.md`](scheduling_engine/examples/README.md);
the worked examples in that directory each illustrate one behaviour (over-capacity,
displacement, a booking splitting a day, two requests contending for one block).

### Tests

Each package's suite runs from its own directory:

```bash
cd scheduling_engine && .venv/bin/python -m pytest tests/ -q
cd calendar_store   && ../scheduling_engine/.venv/bin/python -m pytest tests/ -q
cd mock_ui          && ../scheduling_engine/.venv/bin/python -m pytest tests/ -q
```

471 tests: 239 for the engine, 64 for the store, 168 for the mock UI. See the note
on the one known failure below.

---

## Current state and known limitations

This is a working solver with a playable front end, not a deployable product. What
follows is deliberately explicit, because the specs describe a larger design than
what is built.

**Known failing test.** `mock_ui/tests/test_booking_handler.py::test_a_run_says_how_much_of_the_queue_it_left_out`
fails **when run on a Monday**, and passes on the other six days. Its `monday()`
helper returns the Monday *seven days out* when today is already a Monday, which
pushes the fixture's request outside the seven-day horizon the test itself sets, so
no plan is produced and the assertion never gets a chance to run. It is a
date-dependent test, not an engine fault.

**No persistence.** `AvailabilityStore` is in-memory only. The mock UI works around
this with a JSON session snapshot that deliberately reaches into the store's private
lists — confined to one file, `persistence.py`, so it is obvious what to delete when
a real repository layer arrives.

**The negotiation lifecycle is not in the engine.** `scheduling_engine/SPEC.md`
§6–§9 specify `AcceptedChange`, cold-asks, and a
`PROPOSED → CONFIRMED → EXERCISED` state machine. None of that exists yet, so
`solve_placements` returns a plan nobody has agreed to. The mock UI owns the
negotiation loop in the meantime — draft, provider approval, per-client answers
(accept / decline / refuse), and settlement — and is intended to hand that
responsibility back rather than keep a second implementation of it. The API surface
sketched in SPEC §11 is marked draft and is not implemented.

**Habitual-slot anchoring is designed but not built.** SPEC §10.1 — recognising a
client's usual `(weekday, time-of-day)` and preferring it over raw earliness — is
agreed in principle and is the reason the store records provenance so carefully.
The data capture is in place; the objective term is not.

**Cost-model tuning is deliberately deferred.** The weights that exist (the `alpha`
blend, the displacement tier) are provisional and marked as such in the code.
SPEC §10 explains the reasoning: ranking is decoupled from everything else, and the
weights are a business judgement better settled against a working app and real
history than in the abstract.

**Notifications are not implemented.** The design delegates delivery to a
`NotificationPort`; no such port is wired up.

**Single provider only.** Multi-provider or multi-resource reassignment, mandatory
buffer time between appointments, resizing an appointment's duration, and moving a
recurring series as a whole are all out of scope in the current design.

**Solve time is dominated by the planning horizon.** Because a request may be placed
anywhere in its client's availability, widening the horizon multiplies candidate
slots for every request in the queue. Measured in the mock UI at 25 clients and 20
requests: roughly 2s at a 7-day horizon against roughly 11s at 21 days. Planning a
week at a time — which is what a provider wants anyway — keeps it cheap. On timeout
the solver returns the best solution found rather than a proven optimum, and does
not currently flag that it did so.

---

## Repository layout

```
calendar_store/      availability + appointments  (INTERFACE.md is the public contract)
scheduling_engine/   CP-SAT placement solver      (SPEC.md is the design)
  examples/          worked scenario files for the playground
mock_ui/             FastAPI + vanilla JS front end for driving it by hand
```

## Licence

[MIT](LICENSE).
