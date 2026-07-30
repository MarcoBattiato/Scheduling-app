# calendar_store

Client availability storage: effective-dated recurring rules + single-date
exceptions, materialized on query into `portion.Interval` internally and
`list[TimeSegment]` at the public boundary (`get_availability_segments`). See
SPEC.md for the rationale and precedence rules.

Not yet implemented: `Appointment` (booked-appointment storage) and any real
persistence layer — `AvailabilityStore` is in-memory only so far.

Run tests: `.venv/bin/pytest tests/` (venv created via
`python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"`).
