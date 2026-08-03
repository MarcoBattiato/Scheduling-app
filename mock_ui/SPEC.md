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

## 4. The negotiation loop (mock-owned, temporary)

The engine does not model negotiation yet — `AcceptedChange`, cold-asks and the
`PROPOSED → CONFIRMED → EXERCISED` lifecycle of engine SPEC §6–§9 do not exist.
`solve_placements` returns a *plan*: placements, plus displacements it would
like to make. Nobody has agreed to those.

So this mock owns the loop, crudely and on purpose:

1. A plan containing displacements is held **pending**; nothing is written.
2. Each displacement becomes an approval request for the affected client.
3. All accepted → the whole plan is applied at once.
4. Any declined → the plan is discarded, that appointment is marked unmovable,
   and the scheduler runs again without it.

**This is deliberately cruder than the engine's own design.** Engine SPEC §7.4
applies each confirmed move independently and immediately, rather than gating
the plan on unanimity, and §7.3 supersedes stale proposals rather than
discarding a whole plan. When the engine grows a real lifecycle, this section
is what it replaces — the mock should lose this responsibility, not keep a
second implementation of it.

---

## 5. Writing history correctly

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

- Whole-plan approval rather than independent application (§4).
- A declined move marks the appointment unmovable for the rest of the session,
  rather than blocking that date/time specifically as engine SPEC §9 describes.
- The scheduler runs on submission and on demand, not on the five triggers of
  engine SPEC §5.
- `alpha` and `max_displacements` are global controls, not per-provider
  settings.
- No validation that a client's requested window is one they are available
  for — the engine deliberately does not enforce that either, since a request
  states what the client wants regardless of their standing availability.
- Reschedule bounds are one global default rather than per client.
