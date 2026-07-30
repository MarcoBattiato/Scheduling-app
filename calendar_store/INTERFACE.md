# calendar_store — INTERFACE.md

The public contract: what other packages (specifically `scheduling_engine`) can
rely on. **Changing anything on this page can break a consumer** — before
editing, check what imports `calendar_store` and read the relevant part of
`SPEC.md` first. Rationale, algorithms, and internal invariants live in
`SPEC.md`, not here; this page only states current fact.

## Scope

`calendar_store` knows about clients, recurring availability rules, single-date
exceptions, and booked appointments. It has no concept of requests, offers,
negotiation, or disruption cost — one-way dependency, `calendar_store` must
never import from `scheduling_engine`.

All times are naive `datetime`s (single implicit timezone per client, assumed
consistent by the caller). All windows are half-open: `[window_start,
window_end)`. A rule/exception/appointment never spans midnight.

## Types

```python
class Kind(str, Enum):
    ADD = "add"
    REMOVE = "remove"


@dataclass(frozen=True)
class ClientAvailabilityRule:
    id: int
    client_id: str
    weekday: int  # Monday=0 .. Sunday=6, matches date.weekday()
    start_time: time
    end_time: time
    effective_from: date
    effective_until: Optional[date]  # None = indefinitely active


@dataclass(frozen=True)
class AvailabilityException:
    id: int
    client_id: str
    date: date
    start_time: time
    end_time: time
    kind: Kind


@dataclass(frozen=True)
class Appointment:
    id: int
    client_id: str  # "provider-self" is a valid pseudo-client (e.g. lunch breaks)
    service_type_id: str
    range: TimeSegment
    locked: bool
    notes: Optional[str] = None  # free-text for end users; never read by any solver logic


@dataclass(frozen=True)
class TimeSegment:
    start: datetime
    end: datetime
```

`ClientAvailabilityRule` and `AvailabilityException` are read-only results of
`rules_for`/`exceptions_for` — availability is only ever changed through the
store methods below, never by constructing or editing these directly.

## `AvailabilityStore`

### Recurring rules

```python
def add_recurring_availability(
    client_id: str, weekday: int, start_time: time, end_time: time,
    effective_from: date, effective_until: Optional[date] = None,
) -> list[ClientAvailabilityRule]: ...

def remove_recurring_availability(
    client_id: str, weekday: int, start_time: time, end_time: time,
    effective_from: date, effective_until: Optional[date] = None,
) -> list[ClientAvailabilityRule]: ...

def rules_for(client_id: str, weekday: Optional[int] = None) -> list[ClientAvailabilityRule]: ...
```

### Single-date exceptions

```python
def add_exception_availability(
    client_id: str, on_date: date, start_time: time, end_time: time,
) -> list[AvailabilityException]: ...

def remove_exception_availability(
    client_id: str, on_date: date, start_time: time, end_time: time,
) -> list[AvailabilityException]: ...

def exceptions_for(client_id: str, on_date: Optional[date] = None) -> list[AvailabilityException]: ...
```

### Availability queries

```python
def get_availability(client_id: str, window_start: date, window_end: date) -> portion.Interval: ...

def get_availability_segments(client_id: str, window_start: date, window_end: date) -> list[TimeSegment]: ...
```

**Use `get_availability_segments`, not `get_availability`, from outside this
package.** `get_availability` returns the internal `portion.Interval`
representation and exists for composing multiple calendars (e.g. client ∩
provider) before converting to segments — see `queries.py` below. Depending on
it directly outside `calendar_store` means depending on `portion` too.

Neither reflects `Appointment`s — see "Guarantees," below.

### Booked appointments

```python
def book_appointment(
    client_id: str, service_type_id: str, start: datetime, end: datetime,
    *, locked: bool = False, notes: Optional[str] = None,
) -> Appointment: ...

def cancel_appointment(appointment_id: int) -> Appointment: ...          # raises KeyError if unknown

def reschedule_appointment(appointment_id: int, start: datetime, end: datetime) -> Appointment: ...
    # raises KeyError if unknown; updates `range` only, all other fields unchanged

def appointments_for(client_id: str, window_start: datetime, window_end: datetime) -> list[Appointment]: ...
    # matches on overlap with the window, not containment
```

`book_appointment` performs no double-booking check — see "Guarantees," below.

## Query helpers (`queries.py`)

Operate on `portion.Interval` (i.e. `get_availability`'s return type, not
`list[TimeSegment]`) — for combining multiple calendars before converting to
segments with `to_segments`.

```python
def crop(calendar: portion.Interval, window_start: date, window_end: date) -> portion.Interval: ...
def union(*calendars: portion.Interval) -> portion.Interval: ...
def intersect(*calendars: portion.Interval) -> portion.Interval: ...
def negate(calendar: portion.Interval, window_start: date, window_end: date) -> portion.Interval: ...
def to_segments(calendar: portion.Interval) -> list[TimeSegment]: ...
```

## Guarantees a consumer can rely on

- `get_availability`/`get_availability_segments` reflect rules and exceptions
  only. They never subtract existing `Appointment`s — checking for
  double-booking against booked appointments is the caller's responsibility.
- Segments from `get_availability_segments`/`to_segments` are sorted and
  non-overlapping.
- `Appointment` rows are never merged or normalized — two back-to-back
  bookings stay two separate rows.
- Unknown IDs (`cancel_appointment`, `reschedule_appointment`) raise
  `KeyError`, never return `None` or silently no-op.
