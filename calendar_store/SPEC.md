# calendar_store — SPEC.md

Why the implementation looks the way it does — algorithms, invariants, and the
decisions behind them, captured before they exist only in a chat history. For
the current public contract (types, method signatures, what a consumer can
rely on), see `INTERFACE.md` instead; this file never redeclares a signature,
only explains one that lives there. Rules, exceptions, and appointments are all
implemented and tested; anything not yet built is called out explicitly rather
than presented as settled.

## Problem

Represent each client's (and the provider's) availability over long time periods,
plus their actual booked appointments, in a way that's both compact and preserves
history — without repeating a recurring pattern indefinitely, and without losing
what was true in the past when a pattern changes.

## Resolution: preserve availability history, not edit history

What needs preserving is *availability* over time — "what was true on date X" —
not a record of the edits that produced it. There's no requirement to keep evidence
of what a rule looked like before a change, or when the change happened. Rows are
not an append-only audit trail: when an add or remove overlaps existing rows, those
rows are replaced outright by whatever new rows correctly represent availability
afterward. The old row doesn't survive in closed-out form; it's just gone.

The only real requirement is that the *current* set of rows answers "what was
available on date X" correctly for any X, past or future — `effective_from <= X <
effective_until` on whichever row(s) cover that date. That's a much weaker (and
simpler) guarantee than a temporal-database audit trail, and it's what the
sweep-and-merge algorithm below produces.

Example — old state has one rule, Wednesday 3pm-8pm, effective from 2025-01-01
onward. A new operation says: remove Wednesday 5pm-6pm, effective 2025-12-01 to
2026-03-01 (excluded). The old row is deleted outright and replaced by:

```
2025-01-01 – 2025-12-01 (excl):  Wed 3pm-8pm   (before the remove's date range, untouched)
2025-12-01 – 2026-03-01 (excl):  Wed 3pm-5pm   (during, split by the removed slice)
2025-12-01 – 2026-03-01 (excl):  Wed 6pm-8pm   (during, split by the removed slice)
2026-03-01 – None:               Wed 3pm-8pm   (after, resumes the original pattern)
```

This keeps the storage layer compact (you're storing normalized coverage, not
materialized weekly instances) while still answering historical queries correctly.

## Storage invariant: rules are always positive, and always normalized

Earlier drafts gave `ClientAvailabilityRule` a `kind` ("add"/"remove") and computed
availability as `(all adds) - (all removes)` at query time. That's wrong: it's a
static set operation blind to *when* each rule was written, so a remove permanently
masks any add covering the same slot — even one added later, intending to restore
availability. "Add Tuesday 2-3pm, remove it, re-add it" would incorrectly still show
no availability, because subtraction doesn't know the second add came after the
remove.

**Fix:** the store never keeps a negative rule around. A "remove" is resolved
immediately against the currently-stored positive rules — splitting/truncating
whatever it overlaps — so only positive availability is ever persisted. Symmetrically,
an "add" is also resolved against existing positive rules on write (not just
appended), so the stored set never accumulates overlapping or needlessly fragmented
rows for the same weekday. Both operations maintain one invariant:

> For a given `(client_id, weekday)`, stored rules never overlap each other in the
> 2D space of (date range × time-of-day range), and no two rules that could be
> merged into one (identical time range, touching/overlapping date ranges) are left
> unmerged.

This is what "continuity" means here — in both axes independently, since a change on
one axis (e.g. a remove's date range) can only be evaluated against the same axis on
existing rows, and likewise for the time-of-day axis.

`AvailabilityException` still needs `kind`, unlike rules — a single-date remove has
to carve into a recurring rule's occurrence on just that one date without
fragmenting the recurring rule itself (that would explode row count and defeat the
compactness this design exists for), so it can't resolve away into an edited rule
row the way a recurring remove does. It gets the same order-dependence fix, just
applied on the date axis instead — see "Exception normalization algorithm" below.

`kind` and `created_at` were dropped from `ClientAvailabilityRule`, and
`created_at`/`revoked_at` from `AvailabilityException` too — same reason in both
cases: this schema has no bitemporal/audit-trail concept anywhere in it. `kind` is
gone from rules because only positive availability is ever stored now (see
invariant above) — "add"/"remove" are store operations, not row properties. The
`created_at`/`revoked_at` fields existed to answer "what did the system believe as
of time Y," which isn't a question this store needs to answer — only "what was
available on date X" matters, and valid-time fields (`effective_from`/
`effective_until` on rules, `date` on exceptions) already answer that fully.
`as_of` is gone from `get_availability` as a result — not just unused for now.

## Booked appointments are not availability

Rules/exceptions answer "when could this client be booked at all" (standing
capacity); `Appointment` answers "what's actually booked" (concrete occupied
time). These are deliberately separate concerns, not layered into one
calculation: `get_availability`/`get_availability_segments` reflect rules and
exceptions only, never subtracting existing appointments. Checking for
double-booking against `Appointment` records is left to whoever queries this
store (scheduling_engine's `SPEC.md` §7.2 already lists "no double-booking" and
"availability" as two separate hard constraints — this preserves that split
rather than collapsing it). No double-booking check on `book_appointment`
itself follows from the same split.

Also unlike rules/exceptions, `Appointment` rows are never normalized or merged.
Two back-to-back appointments stay two separate rows even though they're
contiguous — they're distinct bookings, not a span of availability, so nothing
here treats adjacency as something to collapse.

`Appointment` has no `created_at`, unlike the original early sketch of this
schema: unlike rules/exceptions this was never about reconstructing historical
belief, and nothing built so far needs it. `notes` is free-text for end users,
never read by any solver logic — added specifically so users can attach personal
notes to a booking. `range` reuses `TimeSegment` rather than a new dataclass:
scheduling_engine's own (soon to be superseded) `Appointment.range: TimeRange`
already used that exact shape, so there's one canonical (start, end) type doing
this job instead of two independently-defined ones drifting apart.

## Rule normalization algorithm (sweep-and-merge)

Both `add_recurring_availability` and `remove_recurring_availability` call one
shared routine. It treats the date axis as a 1D coordinate-compression sweep, and
reuses `portion` for the time-of-day axis within each resulting segment.

Inputs: `weekday`, `time_range` (a `portion.Interval` over `datetime.time`),
`date_from`, `date_until` (`None` = open-ended), `op` (ADD | REMOVE), and the
store's current list of rules.

```
UNBOUNDED = date.max   # sentinel standing in for "no end", for sweep purposes only

def apply_change(rules, weekday, time_range, date_from, date_until, op):
    existing = [r for r in rules if r.weekday == weekday]
    new_until = date_until or UNBOUNDED

    # 1. Coordinate-compress the date axis: every existing rule's
    #    effective_from/effective_until, plus the new range's own bounds,
    #    become breakpoints. Because of this, no rule's boundary can fall
    #    strictly inside any resulting segment — a rule either fully
    #    covers a segment or is fully disjoint from it. Same for the new
    #    range itself. This is what makes the per-segment logic below a
    #    simple binary "covers / doesn't cover" check.
    breakpoints = sorted({date_from, new_until} |
                          {r.effective_from for r in existing} |
                          {r.effective_until or UNBOUNDED for r in existing})

    # 2. Walk consecutive breakpoint pairs as half-open segments
    #    [seg_start, seg_end), compute each one's resulting time-of-day
    #    coverage, and drop segments that end up empty.
    segments = []  # list of (seg_start, seg_end, coverage: portion.Interval)
    for seg_start, seg_end in zip(breakpoints, breakpoints[1:]):
        if seg_start == seg_end:
            continue  # duplicate breakpoint, zero-width

        coverage = union(
            r.time_range for r in existing
            if r.effective_from <= seg_start
            and (r.effective_until or UNBOUNDED) >= seg_end
        )

        if date_from <= seg_start and new_until >= seg_end:
            coverage = (coverage | time_range) if op is ADD else (coverage - time_range)

        if not coverage.empty:
            segments.append((seg_start, seg_end, coverage))

    # 3. Restore continuity: merge date-adjacent segments whose coverage
    #    came out identical. (Segments skipped in step 2 for being empty
    #    break adjacency on purpose — a real gap must not be merged over.)
    merged = []
    for seg in segments:
        prev = merged[-1] if merged else None
        if prev and prev.end == seg.start and prev.coverage == seg.coverage:
            merged[-1] = prev.with_end(seg.end)
        else:
            merged.append(seg)

    # 4. Unroll: one new rule per (segment, atomic time-of-day range)
    #    inside that segment's coverage — a segment can decompose into
    #    more than one rule if a remove split its time range in two.
    new_rules = []
    for seg_start, seg_end, coverage in merged:
        until = None if seg_end == UNBOUNDED else seg_end
        for atomic in coverage:  # portion.Interval iterates its atomic pieces
            new_rules.append(Rule(weekday, atomic.lower, atomic.upper, seg_start, until))

    # 5. Replace this weekday's rules wholesale with the freshly computed set.
    return [r for r in rules if r.weekday != weekday] + new_rules
```

Worked cases this handles without special-casing:
- **Fresh add, no prior rules**: one segment, one new rule — no fragmentation.
- **Add overlapping/adjacent to an existing rule**: coverage unions, then step 3
  merges it back into one row spanning both, instead of leaving two touching rows.
- **Remove exactly matching an existing rule**: that segment's coverage empties out
  and is dropped — the row disappears, nothing negative is stored.
- **Remove a middle time slice** (e.g. 5-6pm out of 3-7pm): coverage becomes two
  atomic ranges (3-5pm, 6-7pm) → two rules for that segment.
- **Remove overlapping only part of a rule's date range**: splits into up to three
  segments (before/during/after the remove's date range); "before" and "after" keep
  the original time range untouched, "during" gets the time-of-day subtraction.
- **Remove that doesn't overlap anything**: every segment's coverage is unchanged
  (subtracting a disjoint range is a no-op) — matches "extra time in the negative
  rule beyond what's covered has no effect."
- **Re-add after remove**: since nothing negative is ever stored, a later add simply
  unions back in — the bug that started this redesign.

## Exception normalization algorithm

Exceptions are simpler than rules on one axis and share the same risk on another.
Simpler: an exception is pinned to one calendar date, so there's no date-range sweep
— "does this affect date D" is a single boolean check, not a breakpoint walk.
Same risk: a same-date add/remove pair can suffer the identical order-dependence bug
rules had if handled as a static formula, so it gets the same write-time resolution.

Unlike rules, exceptions can't fully resolve away into positive-only storage —
`kind` stays, because a remove has to carve into a *rule's* occurrence on one date
without touching the rule row itself. What can still be minimized is redundancy: an
exception only needs to exist where it actually changes something relative to what
the rule already provides.

For a given `(client_id, date)`, define `rule_coverage` as what the rules alone
provide that day, and treat whatever's currently stored as two disjoint pieces —
`stored_adds` (availability beyond the rule) and `stored_removes` (carve-outs from
it), with the invariant `stored_removes ⊆ rule_coverage` and
`stored_adds ∩ rule_coverage = ∅`. That makes the day's actual current truth
unambiguous:

```
current_truth = (rule_coverage - stored_removes) | stored_adds
```

When a new op (ADD or REMOVE, time range T) comes in for that date:

```
def apply_exception_change(client_id, on_date, time_range, op):
    rule_coverage = rule_coverage_for(client_id, on_date)
    stored_adds, stored_removes = split_by_kind(existing_exceptions(client_id, on_date))
    current_truth = (rule_coverage - stored_removes) | stored_adds

    new_truth = (current_truth | time_range) if op is ADD else (current_truth - time_range)

    new_adds = new_truth - rule_coverage
    new_removes = rule_coverage - new_truth

    replace all exceptions for (client_id, on_date) with one row per atomic
    range in new_adds (kind=ADD) and new_removes (kind=REMOVE)
```

Two decisions this depends on, both made explicitly rather than assumed:

- **Checked against current truth, not rule coverage alone.** A remove can cancel
  out an earlier add-exception even where no rule is involved at all (add 8-9am,
  later remove 8-9am, same date, no rule that day → both vanish). Checking only
  against the rule would leave the add in effect, since the remove "doesn't overlap
  a rule" — technically true, but not what issuing that remove meant.
- **Write-time only, matching rules.** Sanitization uses the rule set as it exists
  *when the exception is written*. If a rule affecting that date changes afterward,
  previously-stored exceptions are not retroactively re-derived.

This formula reproduces the redundancy rules directly as special cases: a positive
exception fully inside `rule_coverage` leaves `new_truth` unchanged, so `new_adds`
comes out empty and it's discarded; a negative exception fully outside
`rule_coverage` (and outside any existing exception) is the same story in reverse.
Partial overlap isn't a separate case — the set difference trims to the effective
portion automatically. And because `stored_removes ⊆ rule_coverage` and
`stored_adds ∩ rule_coverage = ∅` always hold, `get_availability` can safely apply
`(rule_calendar - exception_removes) | exception_adds` in that fixed order with no
precedence ambiguity, unlike the earlier kind-based-precedence attempt for rules
that turned out to be order-unsafe.

**Trim-as-you-go vs. trim-once, and why they can't diverge.** This algorithm
re-derives `new_adds`/`new_removes` after *every* op, not once after a whole
sequence of exceptions has been combined. That's equivalent to trimming once at
the end, not just a shortcut that happens to work: for any sets `R` (rule coverage)
and `S` (current truth), `(R - (R - S)) | (S - R) == S` — trimming factors `S` into
"explained by `R`" and "not explained by `R`" without discarding anything about `S`
itself, only the (irrelevant, per the Resolution section above) history of how `S`
was built. So it doesn't matter whether trimming happens after each op or once at
the end of a sequence — same final state either way, provably.

## Query layer: materialize on demand, not in storage

Given `scheduling_engine/SPEC.md` §3 already bounds the solver to a rolling window
(e.g. 30 days), there's no need to ever expand a recurring rule into concrete flat
intervals beyond what's currently being queried. Rules stay compressed at the
storage layer; `get_availability` (see `INTERFACE.md`) expands them into concrete
intervals for whatever window is asked about: recurring rules are expanded
per-weekday with `dateutil.rrule` (bounded to `window ∩ [effective_from,
effective_until)`) and unioned directly — no add/remove distinction needed for
rules at query time, since storage is already normalized to positive-only.
Exceptions in the window are layered on top as `(rule_calendar -
exception_removes) | exception_adds`, safe in that fixed order per the invariant
described above.

Returning a `portion.Interval` rather than a flat list was a deliberate choice —
it's already a queryable object: `crop`/`intersect`/`negate`/`union` in
`queries.py` are thin wrappers over `&`, `&`, bounded `-`, and `|` on that type,
so no separate query engine was needed. This is why `get_availability` still
exists as a distinct, lower-level function from `get_availability_segments`
rather than being folded into it — see "Boundary with scheduling_engine," below.

## Boundary with scheduling_engine

**Interface shape: flat segments, not the store object.** scheduling_engine
consumes availability as a plain list of concrete time ranges — not the
`AvailabilityStore` itself, and not `portion.Interval` either. Handing over the
store would couple scheduling_engine to calendar_store's internal representation
(rule schema, `portion` as a required dependency) and expose far more than it
needs — it only ever wants "what's actually available in this window," never the
rules/exceptions that produced it. This isn't a lossy simplification: since
scheduling_engine already only operates over a bounded rolling window (its
`SPEC.md` §3), a recurring rule and its concrete occurrences within that window
carry identical information — there's no compression benefit left to preserve by
handing over rule structure instead of the already-flattened result.

`portion.Interval` stays the *internal* currency — it's what makes
crop/intersect/negate/union composable (e.g. combining a client's and the
provider's calendars before handing anything over). `TimeSegment`/`to_segments`/
`get_availability_segments` (see `INTERFACE.md`) are the separate, minimal
boundary conversion.

`TimeSegment` is deliberately a separate type from scheduling_engine's own
`TimeRange` (identical shape, distinct identity) rather than something
scheduling_engine imports from here. `TimeRange` in scheduling_engine's spec covers
things that have nothing to do with calendar_store — request windows,
accepted-change windows — so importing calendar_store's type for all of those
would be coupling in the wrong direction for no benefit. Only availability
segments actually flow from calendar_store to scheduling_engine, so only that one
value shape crosses the boundary; scheduling_engine is free to adapt it into its
own `TimeRange` at the point of use. (`Appointment.range` is the one exception —
it reuses `TimeSegment` directly, since that field lives in calendar_store itself;
see "Booked appointments are not availability," above.)
