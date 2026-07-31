# scheduling_engine

Placement of queued booking requests into the provider's free time. See
SPEC.md for the full design; only the placement half of §7 is implemented so
far — nothing here moves or cancels an existing appointment, and the
negotiation lifecycle (`AcceptedChange`, cold-asks, §6–§9) does not exist yet.

`solve_placements(requests, provider_free, config)` solves the **whole queue
at once**, not one request at a time. That isn't an optimisation: leftover-gap
quality is a shared resource, so where one request lands changes what the next
one costs.

Objective is lexicographic — requests placed first, then
`alpha * fragmentation/F + (1 - alpha) * earliness/E`. Both terms are minutes,
normalised so `alpha` (the provider-facing slider) genuinely mixes them rather
than just ranking them: raw fragmentation is minutes-to-hours while earliness
is routinely days. Earliness is per request, measured from that request's own
earliest feasible start.

Gap waste is derived from the service catalogue, not from round numbers —
`waste(gap) = gap - most service-time still packable into it` (see
`fragmentation.py`). Adding a 45-minute service changes the cost model with no
constants to chase.

Two things worth knowing before changing the solver:

- **`provider_free` is availability with appointments already subtracted.**
  calendar_store returns those separately and deliberately does not net them
  (its INTERFACE.md, "Guarantees"), so the repository adapter does it.
- **`grid_minutes` defaults to 30, the catalogue's gcd.** Going finer roughly
  doubles the candidate count and quadruples solve time to reach the same
  answer, because sub-lattice start times can only produce gaps too small to
  sell. Measured, not assumed — see the note on `CostConfig`.

The client's *general* availability from calendar_store is intentionally not
consulted here. It becomes relevant only when asking whether an
already-booked client would move, which this pass never does.

**To try a scenario by hand there is exactly one way in** — edit a JSON file and
run `python -m scheduling_engine.playground my.json --alpha 0,0.5,1 --open`.
See `examples/README.md`; `--help` documents every field. It renders a
browser-viewable chart of every object involved — provider availability,
existing bookings, the free time that leaves, each request's window, where it
landed, and the gaps left over — one run per alpha so the trade-off is visible
side by side, and `--text` prints the ASCII chart too. Input goes through the
real pipeline (calendar_store holds availability, `free_time` nets out
bookings), so it shows what the engine actually did rather than a mock-up.

Keep it that way: an earlier `examples/manual_check.py` with scenarios hardcoded
in Python duplicated all of this and only created a "which one do I edit?"
question. If you want a scripted sweep, import `solve_placements` directly
rather than adding a second scenario format.

`availability.py` is that netting step: calendar_store deliberately never
subtracts appointments from availability, so somebody has to, and this is the
seam SPEC.md §3 assigns to the repository adapter. It is a pure function over
`TimeSegment`s rather than something holding a store, which keeps `portion` on
calendar_store's side of the boundary.

`visualize.py` renders a calendar and what the solver did to it — run
`.venv/bin/python examples/manual_check.py` to see it, and edit that file's
`SCENARIOS` to try your own. Its gap report is built from the solver's own
grid-snapped blocks rather than from the raw segments, so
`sum(gap.wasted_minutes)` always equals `PlacementResult.fragmentation_minutes`
— `test_visualize.py` pins that, because a chart that disagrees with the solver
is worse than no chart.

Run tests: `.venv/bin/pytest tests/` (venv via `python3 -m venv .venv &&
.venv/bin/pip install ortools pytest -e ../calendar_store -e .` — calendar_store
is a local path dependency, not on PyPI).

`tests/test_placement_reference.py` cross-checks the solver against exhaustive
enumeration; it's the guard on the run-length gap encoding, which is the one
part of `placement.py` that isn't correct-by-inspection.
