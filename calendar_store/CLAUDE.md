# calendar_store

Client availability storage: effective-dated recurring rules + single-date
exceptions, materialized into `portion.Interval` calendars on query. See
DESIGN_NOTES.md for the rationale and precedence rules.

Not yet implemented: `Appointment` (booked-appointment storage) and any real
persistence layer — `AvailabilityStore` is in-memory only so far.

Run tests: `.venv/bin/pytest tests/` (venv created via
`python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"`).
