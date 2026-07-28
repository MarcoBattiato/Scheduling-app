# Rescheduling Optimization Engine — Spec

## 1. Overview

Given a client's request to reschedule an existing appointment into a
preferred time window, this module searches for the minimally disruptive set
of changes to *other* clients' appointments (on the same single-provider
calendar) that makes the request possible, or determines that no acceptable
solution exists.

Scope: **single provider, single calendar.** No multi-staff/multi-resource
reassignment.

**Tech stack:** plain Python module living inside the existing FastAPI
backend codebase — not a separate service, no network/RPC boundary between
the API layer and the engine. It's called in-process from route handlers
(or background tasks). All reads/writes to real data go through an injected
persistence interface (repository pattern, expressed as `typing.Protocol`
classes) so the engine stays decoupled from whichever ORM/DB the backend
uses, and is unit-testable against an in-memory fake.

Domain types are plain `@dataclass`es, independent of any Pydantic/FastAPI
schema — if the API layer needs Pydantic models for request/response
validation, those are thin converters at the route boundary, not the types
the engine reasons over.

**Execution model note:** the search (§5) is CPU-bound and can run up to
~1.5s (sync path) or ~20s (async path). For now it runs inline within the
async request handler / background task, accepting that it will block the
event loop for that duration. This is a known limitation, not an oversight
— see §11 (Future Extensions) for the offload strategy (ProcessPoolExecutor
over a pure-function search core) to adopt if/when this becomes a real
bottleneck under load.

---

## 2. Domain Model

```python
from dataclasses import dataclass, field
from datetime import date, datetime, time
from enum import Enum
from typing import Optional

AppointmentId = str
ClientId = str
PlanId = str


@dataclass(frozen=True)
class TimeRange:
    start: datetime
    end: datetime


@dataclass(frozen=True)
class RecurringBlackout:
    weekday: int          # 0 = Monday ... 6 = Sunday
    start_time: time
    end_time: time


@dataclass
class Unavailability:
    recurring: list[RecurringBlackout] = field(default_factory=list)
    one_off: list[TimeRange] = field(default_factory=list)


@dataclass
class Client:
    id: ClientId
    priority: int = 0                     # higher = more costly to disturb
    unavailability: Unavailability = field(default_factory=Unavailability)


@dataclass(frozen=True)
class ServiceType:
    id: str
    duration_minutes: int


@dataclass
class Appointment:
    id: AppointmentId
    client_id: ClientId
    service_type_id: str
    range: TimeRange
    locked: bool = False           # staff-pinned, hard-immovable regardless of notice
    is_recurring_instance: bool = False
    # Recurring series metadata (if any) is NOT touched by this engine —
    # only this single instance is ever considered movable.


@dataclass(frozen=True)
class DayHours:
    start: time
    end: time
    breaks: list[TimeRange] = field(default_factory=list)


@dataclass
class WeeklyWorkingHours:
    hours_by_weekday: dict[int, list[DayHours]]   # 0=Monday ... 6=Sunday


@dataclass
class ProviderCalendar:
    working_hours: WeeklyWorkingHours
    days_off: list[TimeRange]                     # holidays, vacation, one-off closures
    appointments: list[Appointment]
    # Note: no mandatory inter-appointment buffer is modeled in v1.
    # (See §11, Future Extensions.)
```

### Reschedule request input

The requesting client does **not** give a single exact target time — they
give a preference window and the engine searches within it.

```python
@dataclass
class RescheduleRequest:
    request_id: str                # idempotency key
    appointment_id: AppointmentId  # the requester's appointment to move
    preferred_window: TimeRange    # e.g. "Tue-Thu afternoon this week"
    # Optional finer-grained candidate slots within the window may be
    # supplied by the caller (e.g. UI already filtered to service-length
    # multiples); if omitted, the engine enumerates candidates itself.
    candidate_slots: Optional[list[TimeRange]] = None
```

---

## 3. Hard Constraints (never violated, not just penalized)

A candidate move is infeasible — not merely costly — if it violates any of:

1. **Provider working hours / breaks / days off.** Every appointment must
   fit entirely within an open working window, not overlapping a break.
2. **Notice cutoff.** An appointment cannot be moved (as a bumped move) if
   its *current* start time is within `notice_cutoff_hours` (default 24h)
   of "now." It's simply excluded from the search, not scored.
3. **Locked/pinned appointments.** `appointment.locked is True` is
   immovable regardless of notice or cost. Independent flag from the notice
   cutoff (staff can lock something that's weeks out; and, separately, the
   cutoff still applies even to unlocked ones close in).
4. **Client unavailability.** A candidate new slot for any client (requester
   or bumped) must not intersect that client's recurring or one-off
   blackout windows.
5. **No time overlap.** Standard calendar consistency — no two appointments
   occupy overlapping time on the provider's calendar post-plan.
6. **Duration/gap feasibility.** Appointments have variable, service-defined
   durations. A gap only accepts an appointment if the gap length ≥ that
   appointment's duration. Leftover slack (gap > duration) is allowed and
   simply becomes idle time — it is *not* itself a cost, but see §4 for how
   fragmentation is implicitly discouraged via time-shift cost.

---

## 4. Disruption Cost Model

Candidate plans are compared **lexicographically** across five tiers, most
important first. A plan is strictly better than another if it wins on the
first tier where they differ.

| Tier | Metric | Direction |
|---|---|---|
| 0 | Number of cancellations in the plan | minimize (0 is required unless no zero-cancellation plan exists at all) |
| 1 | Number of distinct *other* clients affected (moved or cancelled) | minimize |
| 2 | Total time-shift magnitude, summed over *other* affected clients only (`abs(new_start - old_start)` in minutes) | minimize |
| 3 | Total priority-weighted disturbance (`sum(client.priority)` over *other* affected clients, or `sum(priority * shift_minutes)` — see note) | minimize |
| 4 | Notice given to *other* affected clients (`sum(affected_appointment.start - now)`, i.e. more lead time is better) | maximize |

**The requester's own move is excluded from every tier (0–4).** All five
metrics are computed only over the *other* clients displaced by the plan —
never the requesting client, who consented to being moved by making the
request in the first place. `Plan.moves` still includes the requester's own
move (it's needed to actually execute the plan, §7), but `PlanCost` is a
function of `moves` with the requester's own move filtered out first.

**Cancellation as last resort:** if, within the search bounds, no plan can
resolve the requester's window without permanently cancelling a bumped
client's appointment (rather than relocating it), the engine may include a
cancellation as a move. This is tier 0 and dominates everything else — a
plan with 1 cancellation is worse than *any* plan with 0 cancellations, no
matter how much smaller its count/time-shift/priority costs are.

**Tier 3 weighting note:** default implementation uses
`sum(client.priority)` (count-weighted by priority tier) rather than
`priority * shift_minutes`, to keep tier 2 and tier 3 orthogonal (tier 2
already captures shift magnitude). This is a configurable strategy —
see `CostWeights` below — should the business want priority to scale with
how far someone is moved.

```python
class PriorityWeightMode(str, Enum):
    COUNT = "count"
    COUNT_TIMES_SHIFT = "count_times_shift"


@dataclass
class CostWeights:
    # Only used to break ties *within* a tier when the primary lexicographic
    # comparator is exactly equal (rare) — not used to blend tiers.
    priority_weight_mode: PriorityWeightMode = PriorityWeightMode.COUNT


@dataclass(order=True)
class PlanCost:
    # All fields below are computed over the *other* clients displaced by
    # the plan — the requester's own move is always excluded.
    cancellations: int
    clients_affected: int
    total_shift_minutes: int
    priority_cost: int
    total_notice_minutes: int   # compared inverted — see compare_costs


def plan_cost(plan: "Plan", requester_client_id: ClientId) -> PlanCost:
    """Computes PlanCost from plan.moves, filtering out the move belonging
    to requester_client_id before aggregating any tier."""


def compare_costs(a: "PlanCost", b: "PlanCost") -> int:
    """Lexicographic compare in tier order above; tier 4 direction inverted
    (higher total_notice_minutes is better). Returns -1/0/1."""
```

---

## 5. Search Algorithm

**Strategy:** bounded branch-and-bound over chains of bumps, pruning any
branch whose cost already exceeds the best-known solution's cost at the tier
being explored.

A "chain" starts from the requester's appointment wanting to occupy a
candidate slot. If that slot is occupied, the occupying appointment ("B")
must itself be relocated to a still-vacant, feasible slot elsewhere; if that
requires displacing another appointment ("C"), the chain extends, up to
`max_chain_depth`.

```python
@dataclass
class SearchBounds:
    max_chain_depth: int   # number of *other* appointments displaced in one chain
    day_window: int        # days to look +/- from the requester's current appointment date
    time_budget_ms: int
    top_n: int              # how many ranked alternative plans to return


SYNC_BOUNDS = SearchBounds(max_chain_depth=2, day_window=7, time_budget_ms=1500, top_n=3)
ASYNC_BOUNDS = SearchBounds(max_chain_depth=4, day_window=30, time_budget_ms=20000, top_n=5)
```

**Execution mode:** the engine first attempts a synchronous search using
`SYNC_BOUNDS`. If it exhausts the time budget without a full guarantee of
optimality within those bounds (calendar too dense/large), OR the caller
explicitly requests a wider search, it falls back to an **async job** using
`ASYNC_BOUNDS`, identified by a job id, with progress/result delivered via
event (`plan_search_completed`) — see §7. In the current inline execution
model (§1), "async job" means a FastAPI `BackgroundTasks` job or equivalent
in-process async task, not a separate worker service.

The search is exhaustive *within its bounds* (true optimum guaranteed inside
`max_chain_depth`/`day_window`), not globally exhaustive — a solution outside
those bounds may exist but won't be found by the sync path, and only up to
the wider async bounds even in the fallback.

**Output:** top-N distinct feasible plans, ranked by `compare_costs`, deduped
so alternatives differ by at least one actual move (not just internal search
order).

---

## 6. Plan Lifecycle & Confirmation Flow

A found plan is not committed immediately — every *other* client whose
appointment moves or is cancelled must explicitly opt in.

```
find_plans()
   │
   ▼
[proposed]  ──(caller selects one of the top-N)──▶  [pending_confirmation]
                                                          │  holds are placed (§8)
                                                          │  confirmation requests sent to
                                                          │  every affected client
                                                          ▼
                        ┌──────────────────────────────────────────────┐
                        │ all confirm before their individual timeout  │──▶ [committed]
                        ├──────────────────────────────────────────────┤
                        │ any one declines, or times out (= decline)   │──▶ [retrying]
                        ├──────────────────────────────────────────────┤
                        │ caller calls abort_plan()                    │──▶ [aborted]
                        └──────────────────────────────────────────────┘
```

**On decline/timeout — auto-retry-once rule:**
The engine automatically re-runs the search **exactly once**, treating the
declining client's current appointment as if it were `locked` (excluded from
being moved), keeping every other already-confirmed move from the original
chain fixed where possible. The retry produces a new top-N; the caller is
notified of the new proposal via event. If the retry also fails to find any
plan, the engine surfaces final failure (`no_solution_found`) — it does not
retry a second time automatically; a further attempt requires a fresh
`find_plans()` call from the caller.

**Per-client confirmation timeout:** configurable, default 2 hours from when
that client's confirmation request was sent. Expiry is treated identically
to an explicit decline.

**Cancellation-as-move confirmation:** a client whose appointment is being
cancelled (not relocated) as part of a plan is still sent a
"confirm/decline" request (framed as cancellation, not reschedule) before
the plan commits — cancellation is never silently auto-applied.

---

## 7. API Surface

Class-based core API (`ReschedulingEngine`), all lifecycle methods `async
def` to match the FastAPI backend's calling convention, plus a lightweight
event-callback registry for async/state-change notifications (caller can
poll `get_plan_status` or subscribe to events).

```python
from typing import Awaitable, Callable, Literal, Protocol

FailureReason = Literal[
    "no_feasible_plan_within_bounds",
    "all_retry_attempts_exhausted",
    "aborted_by_caller",
    "concurrent_modification",
]

PlanState = Literal[
    "proposed", "pending_confirmation", "committed",
    "retrying", "failed", "aborted",
]


@dataclass
class Move:
    appointment_id: AppointmentId
    client_id: ClientId
    kind: Literal["reschedule", "cancel"]
    from_range: TimeRange
    to_range: Optional[TimeRange] = None   # absent if kind == "cancel"


@dataclass
class Plan:
    id: PlanId
    moves: list[Move]        # includes the requester's own move
    cost: PlanCost


@dataclass
class FindPlansResult:
    request_id: str
    mode: Literal["sync", "async"]
    status: Literal["ready", "no_solution", "searching"]
    plans: list[Plan]        # top-N, empty if no_solution
    job_id: Optional[str] = None   # present if mode == "async"


@dataclass
class PendingConfirmation:
    appointment_id: AppointmentId
    client_id: ClientId
    expires_at: datetime


@dataclass
class PlanStatus:
    plan_id: PlanId
    state: PlanState
    pending_confirmations: list[PendingConfirmation]


class ReschedulingEngine(Protocol):
    # Search
    async def find_plans(self, request: RescheduleRequest) -> FindPlansResult: ...

    # Lifecycle
    async def select_plan(self, plan_id: PlanId) -> None:
        """Moves [proposed] -> [pending_confirmation], places holds,
        sends confirmation requests."""
        ...

    async def confirm_slot(self, plan_id: PlanId, appointment_id: AppointmentId, client_id: ClientId) -> None: ...
    async def decline_slot(self, plan_id: PlanId, appointment_id: AppointmentId, client_id: ClientId) -> None: ...
    async def abort_plan(self, plan_id: PlanId, reason: Optional[str] = None) -> None: ...
    async def get_plan_status(self, plan_id: PlanId) -> PlanStatus: ...

    # Events — synchronous or async callbacks, invoked in-process (no message broker in v1)
    def on(self, event: str, callback: Callable[..., Awaitable[None] | None]) -> None: ...
```

**Events emitted** (`event` name → payload): `plan_search_completed` (
`FindPlansResult`), `client_confirmed` (`plan_id`, `appointment_id`),
`client_declined` (`plan_id`, `appointment_id`, `reason`), `plan_retrying`
(`plan_id`, `new_plan_id`), `plan_committed` (`plan_id`), `plan_failed`
(`plan_id`, `reason`), `plan_aborted` (`plan_id`).

---

## 8. Concurrency & Slot Holding

Because confirmations are asynchronous (up to hours), every slot touched by
a plan — the requester's new slot, every bumped client's new slot, and every
vacated slot — is put under a **hard hold** the moment the plan enters
`pending_confirmation`. Held slots are unbookable by any other operation
(new bookings, other reschedule searches) until the plan resolves
(committed/failed/aborted), at which point holds release.

Holds are implemented via the injected persistence layer (not in-process
memory), since the backend may run multiple worker processes/instances:

```python
from typing import Protocol

HoldToken = str


class SchedulingRepository(Protocol):
    async def get_calendar(self, date_range: TimeRange) -> ProviderCalendar: ...
    async def get_client(self, client_id: ClientId) -> Client: ...

    async def acquire_hold(self, ranges: list[TimeRange], plan_id: PlanId) -> HoldToken:
        """Atomic; fails if any range already held or booked."""
        ...

    async def release_hold(self, token: HoldToken) -> None: ...

    async def apply_plan(self, plan: Plan) -> None:
        """Atomic commit of all moves; releases holds."""
        ...


class NotificationPort(Protocol):
    async def request_confirmation(self, client_id: ClientId, move: Move, expires_at: datetime) -> None: ...
    async def notify_committed(self, client_id: ClientId, move: Move) -> None: ...
    async def notify_plan_failed(self, requester_id: ClientId, reason: FailureReason) -> None: ...
```

`find_plans()` itself does **not** acquire holds — only `select_plan()` does,
since search may return multiple alternatives the caller hasn't chosen yet.
This means a plan returned by `find_plans` can theoretically become stale
before `select_plan` is called; `select_plan` re-validates feasibility
against current state and raises/returns `concurrent_modification` if the
underlying calendar changed incompatibly (e.g. someone else booked into a
slot the plan needed) since the search ran.

---

## 9. Configuration

```python
@dataclass
class EngineConfig:
    notice_cutoff_hours: int = 24
    confirmation_timeout_hours: int = 2
    sync_bounds: SearchBounds = field(default_factory=lambda: SYNC_BOUNDS)
    async_bounds: SearchBounds = field(default_factory=lambda: ASYNC_BOUNDS)
    cost_weights: CostWeights = field(default_factory=CostWeights)
```

---

## 10. Edge Cases Explicitly Handled

- **Requester's own appointment is locked or inside the notice cutoff:**
  the requester can still request — the constraint applies to *other*
  appointments being displaced, not to the requester's own move (they
  initiated it, so consent is implicit). If desired later, add a separate
  guard for staff-initiated reschedules on the requester's behalf; out of
  scope for v1.
- **No feasible slot anywhere in the preferred window, even with zero
  displacements:** treated the same as any other search — the trivial
  zero-move "just place the requester in an already-free slot" plan is
  always considered first/cheapest if it exists.
- **Recurring appointments:** only the single occurrence under
  consideration is ever moved; the recurring series and its future
  instances are untouched. A recurring instance is a normal `Appointment`
  for search purposes (movable unless locked or inside the notice cutoff),
  distinguished only by `is_recurring_instance` for UI/audit purposes.
- **Client blackout windows:** enforced as a hard constraint (§3.4) against
  every candidate slot, for both the requester and any bumped client — a
  chain cannot relocate someone into their own unavailable time.
- **Cancellation as last resort:** only ever proposed if no relocation
  exists for a displaced client within bounds; always requires explicit
  client confirmation (§6), never silently applied.
- **Declines mid-chain:** re-search once, excluding the declining client's
  appointment as movable; if that also fails, surface final failure rather
  than retrying indefinitely (§6).
- **Race conditions during pending confirmation:** prevented via hard holds
  on every slot the plan touches (§8); `select_plan` re-validates against
  the live calendar to catch drift between search-time and selection-time.
- **Idempotency:** `RescheduleRequest.request_id` lets the caller safely
  retry `find_plans` calls (e.g. on network retry) without triggering
  duplicate searches/holds.
- **Duration/gap mismatches:** a smaller appointment moved into a larger
  gap leaves idle time (feasible, no penalty beyond the time-shift already
  counted); a larger appointment cannot be moved into a smaller gap
  (infeasible, §3.6).

---

## 11. Explicitly Out of Scope (v1)

- Multi-provider/multi-resource reassignment (single provider only).
- Mandatory buffer/cleanup time between appointments — not modeled; can be
  added later as an extra fixed duration added to each appointment's
  effective length during gap-feasibility checks.
- Resizing appointment durations — durations are fixed per service type.
- Rescheduling recurring series as a whole (only single-instance moves).
- Actual delivery mechanics of notifications (email/SMS/push) — delegated
  to `NotificationPort`, implemented outside this module.
- A second automatic retry after a retry-once failure — further attempts
  are caller-initiated.
- **Offloading the search off the event loop.** The search core is written
  as a pure function of a calendar snapshot (`ProviderCalendar` in,
  `list[Plan]` out) with no I/O, specifically so it *can* later be run in a
  `concurrent.futures.ProcessPoolExecutor` via
  `loop.run_in_executor(...)` without restructuring — but this is not
  wired up in v1 per the decision in §1; the search runs inline and may
  block the event loop for its time budget.
