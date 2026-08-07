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
- `static/calendar.js` — a week-column calendar drawn from a plain
  description: availability to tint, blocks to place, ghosts for where
  something used to be, arrows between them. The same component renders the
  real schedule and a proposed one, which is the point — a proposal is only
  legible against the calendar it would land in.
- `static/app.js` — state, panels, actions. No build step, no framework.

Availability is edited in two places on purpose. The weekly grid is the
*pattern*; dragging on the calendar overrides a *single date* (shift-drag marks
time away). That mirrors calendar_store's own split between recurring rules and
exceptions.

## The thing most worth not breaking

This mock is what generates the artificial history that
`scheduling_engine/SPEC.md` §10.1 (habitual-slot anchoring) will be judged
against, so **`origin` has to be right**:

- a slot the client asked for, or moved themselves to → `Origin.CLIENT`
- a move the scheduler imposed and the client merely accepted →
  `Origin.DISPLACED`

The second is easy to get wrong. An accepted displacement is still a
displacement — the client agreed, but they did not *choose* the slot, and
recording it as a preference is precisely the failure the field exists to
prevent. `test_flows.py` pins this.

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
and **each answer is applied on its own**, per engine SPEC §7.4. The one thing
that cannot be independent is a booking that only exists because somebody was
going to vacate the slot — that dependency is derived from the overlap between
a placement and a displacement's old range, since the engine does not report
the link. When the engine grows `AcceptedChange`, this should lose the
responsibility rather than keep a second implementation of it.

Run tests: `../scheduling_engine/.venv/bin/pytest tests/` (this package shares
the engine's venv — one interpreter can import all three).
