"""What belongs in each person's own calendar app, and how to say it.

One function decides it, `events_for`, and every channel reads from it: the
subscription feed serves it whole, and the push channels to come (email
invitations, Google, Outlook) will send only what `changes` says moved since
they last looked. Deciding once is what keeps an event identical however it
arrives.

Three rules, from docs/INTEGRATIONS_PLAN.md:

- **Only settled facts leave the app.** An offer, or a change agreed but not
  yet confirmed, is not an event — an entry that later vanishes is worse than
  none.
- **A move is a move.** The store records a reschedule as a new appointment
  linked to the old one by `supersedes`, so an event's identity is the *root*
  of that chain, and its `sequence` is how many times it has moved. Calendars
  then show one event that changed, not a cancellation and a stranger.
- **A client's event says as little as possible.** Their calendar is often
  shared with a partner, a family or an employer, so it carries the practice
  name and nothing about what the appointment is for. The provider's own
  calendar may name the client and the service; neither carries notes.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Dict, Iterable, List, Optional, Tuple

from calendar_store import AppointmentStatus

PROVIDER = "provider-self"

# The appointments are naive local times; the practice is in Singapore, which
# has had no daylight saving since 1982, so one fixed offset describes it.
TZID = "Asia/Singapore"
VTIMEZONE = [
    "BEGIN:VTIMEZONE", f"TZID:{TZID}",
    "BEGIN:STANDARD", "DTSTART:19700101T000000",
    "TZOFFSETFROM:+0800", "TZOFFSETTO:+0800", "TZNAME:+08",
    "END:STANDARD", "END:VTIMEZONE",
]

# How far either side of today a feed reaches. Backwards so last week's
# sessions stay visible; forwards well past the scheduler's own horizon.
LOOK_BACK = timedelta(days=60)
LOOK_AHEAD = timedelta(days=365)

# Kept in the calendar, as history, once they have happened.
_SHOWN = (AppointmentStatus.BOOKED, AppointmentStatus.COMPLETED, AppointmentStatus.NO_SHOW)


@dataclass(frozen=True)
class CalendarEvent:
    uid: str
    start: datetime
    end: datetime
    summary: str
    description: str = ""
    sequence: int = 0          # how many times it has moved


def _every_appointment(world) -> Dict[int, object]:
    """All appointments, cancelled and superseded included — the chain to a
    booking's root runs through rows that are no longer live."""
    span = (datetime(2000, 1, 1), datetime(2100, 1, 1))
    found = {}
    for person in list(world.clients) + [PROVIDER]:
        for appointment in world.store.appointment_history(person, *span):
            found[appointment.id] = appointment
    return found


def _root_and_depth(appointment, everything: Dict[int, object]) -> Tuple[int, int]:
    current, depth, seen = appointment, 0, set()
    while current.supersedes is not None and current.supersedes in everything:
        if current.id in seen:       # a cycle would be a store bug; do not hang on it
            break
        seen.add(current.id)
        current = everything[current.supersedes]
        depth += 1
    return current.id, depth


def events_for(world, person: str, now: Optional[datetime] = None) -> List[CalendarEvent]:
    """Everything that should be in `person`'s calendar right now.

    `person` is a client id, or "provider" for the practice's own calendar,
    which holds every client's sessions and the provider's own blocks.
    """
    now = now or datetime.now()
    everything = _every_appointment(world)
    practice = world.practice_name
    provider = person in ("provider", PROVIDER)
    whose = list(world.clients) + [PROVIDER] if provider else [person]

    events = []
    for appointment in everything.values():
        if appointment.client_id not in whose or appointment.status not in _SHOWN:
            continue
        if not (now - LOOK_BACK <= appointment.range.start <= now + LOOK_AHEAD):
            continue
        root, depth = _root_and_depth(appointment, everything)
        service = world._service_name(appointment.service_type_id)
        if not provider:
            summary, description = f"Appointment — {practice}", ""
        elif appointment.client_id == PROVIDER:
            summary, description = service, ""
        else:
            name = world.clients[appointment.client_id].name
            summary, description = f"{name} — {service}", ""
            if appointment.status is AppointmentStatus.NO_SHOW:
                description = "Did not attend"
        events.append(CalendarEvent(
            uid=f"appointment-{root}@scheduling-app",
            start=appointment.range.start, end=appointment.range.end,
            summary=summary, description=description, sequence=depth,
        ))
    return sorted(events, key=lambda e: e.start)


def changes(before: Iterable[CalendarEvent],
            after: Iterable[CalendarEvent]) -> List[Tuple[str, CalendarEvent]]:
    """What a push channel must send to get from `before` to `after`:
    ("created" | "updated" | "cancelled", event), by uid.

    Derived by comparing what a calendar should hold, not by hooking every
    place a booking can change — there are a dozen of those, and a missed one
    is a calendar that silently drifts. A comparison cannot miss one.
    """
    was = {e.uid: e for e in before}
    now = {e.uid: e for e in after}
    out = []
    for uid, event in now.items():
        if uid not in was:
            out.append(("created", event))
        elif event != was[uid]:
            out.append(("updated", event))
    for uid, event in was.items():
        if uid not in now:
            out.append(("cancelled", event))
    return out


# -- iCalendar text --------------------------------------------------------


def _escape(text: str) -> str:
    return (text.replace("\\", "\\\\").replace(";", "\\;")
                .replace(",", "\\,").replace("\n", "\\n"))


def _fold(line: str) -> str:
    """RFC 5545 §3.1: lines longer than 75 octets continue on the next line,
    which starts with a space. Folded on bytes, never inside a character."""
    raw = line.encode("utf-8")
    if len(raw) <= 75:
        return line
    parts, chunk = [], b""
    for char in line:
        encoded = char.encode("utf-8")
        if len(chunk) + len(encoded) > (75 if not parts else 74):
            parts.append(chunk.decode("utf-8"))
            chunk = b""
        chunk += encoded
    parts.append(chunk.decode("utf-8"))
    return "\r\n ".join(parts)


def _local(moment: datetime) -> str:
    return moment.strftime("%Y%m%dT%H%M%S")


def to_ics(events: Iterable[CalendarEvent], calendar_name: str,
           stamp: Optional[datetime] = None) -> str:
    stamp = (stamp or datetime.now(timezone.utc)).strftime("%Y%m%dT%H%M%SZ")
    lines = [
        "BEGIN:VCALENDAR", "VERSION:2.0",
        "PRODID:-//scheduling-app//mock_ui//EN",
        "CALSCALE:GREGORIAN", "METHOD:PUBLISH",
        f"X-WR-CALNAME:{_escape(calendar_name)}",
        f"X-WR-TIMEZONE:{TZID}",
        # Hints, honoured by Apple and Outlook. Google refreshes subscribed
        # calendars on its own schedule whatever these say.
        "REFRESH-INTERVAL;VALUE=DURATION:PT15M",
        "X-PUBLISHED-TTL:PT15M",
        *VTIMEZONE,
    ]
    for event in events:
        lines += [
            "BEGIN:VEVENT",
            f"UID:{event.uid}",
            f"DTSTAMP:{stamp}",
            f"DTSTART;TZID={TZID}:{_local(event.start)}",
            f"DTEND;TZID={TZID}:{_local(event.end)}",
            f"SUMMARY:{_escape(event.summary)}",
            f"SEQUENCE:{event.sequence}",
            "STATUS:CONFIRMED",
            "TRANSP:OPAQUE",
        ]
        if event.description:
            lines.append(f"DESCRIPTION:{_escape(event.description)}")
        lines.append("END:VEVENT")
    lines.append("END:VCALENDAR")
    return "\r\n".join(_fold(line) for line in lines) + "\r\n"
