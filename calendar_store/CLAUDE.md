# calendar_store

Client availability storage: effective-dated recurring rules + single-date
exceptions, materialized on query into `portion.Interval` internally and
`list[TimeSegment]` at the public boundary (`get_availability_segments`). Also
holds `Appointment` (a booked slot — separate from availability, never
normalized; book/cancel/reschedule/query via `AvailabilityStore`).

**Appointment rows accumulate; they are never deleted.** Cancelling sets
`status`, and rescheduling writes a new row linked to the old one through
`supersedes`. Overwriting either would destroy the record of where a client's
bookings actually sat, which is the raw material for working out their
habitual slot — and `origin` distinguishes a slot the client chose from one
the scheduler moved them to, so the scheduler cannot launder its own
rescheduling into a client's apparent preferences. `appointments_for` returns
live rows only; `appointment_history` returns everything.

Docs are split by audience: **INTERFACE.md** is the public contract (types,
signatures, guarantees) — read this if you're consuming calendar_store from
elsewhere, and treat any change to it as a possible breaking change for
scheduling_engine. **SPEC.md** is why the implementation looks the way it does
(algorithms, invariants, rejected approaches) — read this if you're changing
calendar_store's internals; it never redeclares a signature, only explains one
that lives in INTERFACE.md.

Not yet implemented: any real persistence layer — `AvailabilityStore` is
in-memory only so far.

Run tests: `.venv/bin/pytest tests/` (venv created via
`python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"`).
