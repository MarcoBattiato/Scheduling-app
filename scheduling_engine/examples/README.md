# Trying scenarios by hand

One input format, no Python to edit. Change a `.json` file, run it, look at the
page.

**In VS Code:** open the `.json` you want to try and press **F5**, then pick
*"Playground: run the JSON I have open"* — it uses the `"alpha"` set in that
file. There is no Python file to run: the playground is a module, so the plain
▶ *Run Python File* button cannot launch it, and the F5 configurations exist to
cover that.

The second configuration, *"Playground: compare alpha 0 / 0.5 / 1"*,
deliberately **ignores** the file's own `"alpha"` — comparing is the whole
point of it. If the run you get is not at the alpha you set, you picked that
one.

**In a terminal:**

```bash
cd scheduling_engine
.venv/bin/python -m scheduling_engine.playground examples/scenario.json --open
```

`--alpha` on the command line overrides the file the same way; leave it off to
use what the file says.

Add `--alpha 0,0.5,1` to solve the same scenario at several slider positions and
get one run per value stacked in the same page — that side-by-side is usually
the point. Add `--text` to also print an ASCII chart in the terminal.

`.venv/bin/python -m scheduling_engine.playground --help` documents every field
of the format.

## The files

| File | What it is there to show |
|---|---|
| `scenario.json` | **The one to edit.** A full week — three open days, two slots already taken, five requests. Change it freely; it is a scratch file. |
| `scenario_template.json` | An unedited copy of the same week. Leave it alone, and copy it back over `scenario.json` when you want to start from a known state. |
| `where-does-it-go.json` | One request, one open morning. The smallest case where `alpha` visibly changes the answer. |
| `shared-block.json` | Two requests competing for one block — why the queue is solved jointly rather than one at a time. |
| `split-day.json` | An existing booking cutting a day in half, and requests fitting around it. |
| `over-capacity.json` | Three 90-minute requests into three hours. One cannot fit; a partial solution is the correct answer, not a failure. |

Rendered `.html` output is gitignored — regenerate it any time by re-running.

## Reading the chart

Lanes, top to bottom, in the order they matter:

- **Availability** — what the provider offers, straight from calendar_store.
- **Already booked** — appointments that existed before this solve.
- **Free** — availability minus bookings. This is all the solver ever sees.
- **Placed** — where each request landed.
- **Gaps left** — leftover free time. Green is reusable, red is structurally
  unsellable (too short for any service in the catalogue).
- **Per request** — the window it asked for, and dashed beneath it, that
  client's own availability.

Hover any bar for exact times.

One thing to know while reading it: a client's own availability is **shown but
not enforced**. This pass honours only what each request itself asked for.
Client availability becomes load-bearing later, when the engine starts asking
already-booked clients whether they would move.

## Calling it from Python instead

The JSON path is a front end over an ordinary function — for sweeps or
experiments, skip it:

```python
from scheduling_engine import BookingRequest, CostConfig, TimeRange, render, solve_placements

result = solve_placements(requests, provider_free, CostConfig(alpha=0.7))
print(render(provider_free, result))     # ASCII
```

`solve_placements` wants free time, not raw availability — use
`scheduling_engine.free_time(availability, booked)` to net out existing
appointments, which calendar_store deliberately never does for you.
