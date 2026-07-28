# calendar_store — Design Notes (starting point, not a finished spec)

Captures the design discussion so far, before it exists only in a chat history.
This is a reference for a fresh Claude Code interview to build on — it should still
propose test cases and surface gaps, not treat this as finished.

## Problem

Represent each client's (and the provider's) availability over long time periods,
plus their actual booked appointments, in a way that's both compact and preserves
history — without repeating a recurring pattern indefinitely, and without losing
what was true in the past when a pattern changes.

## Resolution: effective-dated rules, not in-place edits

This is the classic **valid-time versioning** problem from temporal database design
(a "Type 2 slowly changing dimension," in data-warehouse terms). The failure mode to
avoid: if a client's recurring pattern changes and the existing rule row is edited
in place, there's no way to later reconstruct what the system believed was true
before the change — which matters for debugging past scheduling decisions or
disputes about what was offered when.

**Rule:** changing a recurring pattern never mutates an existing row. It closes out
the old rule (sets `effective_until`) and inserts a new one starting from the change
date. Exceptions are similarly immutable once created — a revoke sets `revoked_at`
rather than deleting the row.

This keeps the storage layer compact (you're storing rule-*change events*, which are
rare, not materialized weekly instances) while fully preserving history — querying
"what was this client's availability rule on date X" is just
`effective_from <= X < effective_until`.

## Schema (starting point)

```
ClientAvailabilityRule:
  id, client_id, weekday, start_time, end_time,
  effective_from, effective_until (nullable = still active),
  created_at

AvailabilityException:
  id, client_id, time_range, kind, created_at, revoked_at (nullable)

Appointment:
  id, client_id, service_type_id, range, locked, created_at
```

## Query layer: materialize on demand, not in storage

Given `scheduling_engine/SPEC.md` §3 already bounds the solver to a rolling window
(e.g. 30 days), there's no need to ever expand a recurring rule into concrete flat
intervals beyond what's currently being queried. Rules stay compressed at the
storage layer; a single function expands them into concrete intervals for whatever
window is asked about:

```
get_availability(client_id, window, as_of=None) -> list[TimeRange]
```

`as_of` defaults to "now" (current rules) but, given rules are effective-dated,
historical reconstruction ("what was true on a past date") falls out almost for
free — worth keeping as a real parameter, not an afterthought.

## Boundary with scheduling_engine

One-way dependency: `calendar_store` must never import from `scheduling_engine`.
This package only knows about clients, rules, exceptions, and booked appointments —
it has no concept of requests, offers, negotiation, or disruption cost.
