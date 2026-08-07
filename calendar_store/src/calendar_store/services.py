"""What the provider sells: name, how long it takes, what it costs.

Separate from `AvailabilityStore` because it answers a different question —
that one knows when time exists, this one knows what may be booked into it.
`Appointment.service_type_id` points here.

Services are **deactivated, never removed**, for the same reason appointments
are never deleted: an appointment booked last month against a service
discontinued this morning still has to be readable, rescheduled and invoiced.
Deactivating stops it being offered; the record stays resolvable forever.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Dict, List, Optional, Tuple


@dataclass(frozen=True)
class Service:
    """One bookable thing.

    `price_minor_units` is an integer in the currency's smallest unit — cents,
    pence — never a float. Money in binary floating point accumulates rounding
    error that eventually shows up on an invoice, and an invoice that does not
    add up is worse than one that is hard to compute. The currency itself is
    not stored here: a single-provider business has one, and repeating it per
    service only invites them to disagree.
    """
    id: str
    name: str
    duration_minutes: int
    price_minor_units: int
    active: bool = True
    # Provider-only entries — a lunch break, a standing personal commitment —
    # occupy the calendar exactly as a session does and so need a service to
    # be booked against, but must never appear in a client's list of options.
    client_bookable: bool = True
    description: Optional[str] = None


class ServiceCatalogue:
    def __init__(self) -> None:
        self._services: Dict[str, Service] = {}

    # -- mutations ---------------------------------------------------

    def add_service(
        self,
        service_id: str,
        name: str,
        duration_minutes: int,
        price_minor_units: int,
        *,
        client_bookable: bool = True,
        description: Optional[str] = None,
    ) -> Service:
        if service_id in self._services:
            raise ValueError(f"service {service_id!r} already exists")
        if duration_minutes <= 0:
            raise ValueError("duration must be positive")
        if price_minor_units < 0:
            raise ValueError("price cannot be negative")

        service = Service(
            id=service_id,
            name=name,
            duration_minutes=duration_minutes,
            price_minor_units=price_minor_units,
            client_bookable=client_bookable,
            description=description,
        )
        self._services[service_id] = service
        return service

    def update_service(self, service_id: str, **changes) -> Service:
        """Edit a service in place.

        Note this rewrites the *current* definition: an appointment booked at
        the old price still resolves, but resolves to the new price. Invoicing
        must therefore record what was charged at the time rather than looking
        it up afterwards — see INTERFACE.md.
        """
        allowed = {"name", "duration_minutes", "price_minor_units",
                   "client_bookable", "description"}
        unknown = set(changes) - allowed
        if unknown:
            raise ValueError(f"cannot change {sorted(unknown)}")
        if changes.get("duration_minutes", 1) <= 0:
            raise ValueError("duration must be positive")
        if changes.get("price_minor_units", 0) < 0:
            raise ValueError("price cannot be negative")

        service = replace(self.get_service(service_id), **changes)
        self._services[service_id] = service
        return service

    def deactivate_service(self, service_id: str) -> Service:
        """Withdraw a service from sale without losing it."""
        service = replace(self.get_service(service_id), active=False)
        self._services[service_id] = service
        return service

    def reactivate_service(self, service_id: str) -> Service:
        service = replace(self.get_service(service_id), active=True)
        self._services[service_id] = service
        return service

    # -- queries -----------------------------------------------------

    def get_service(self, service_id: str) -> Service:
        """Resolve a service by id, active or not.

        Deliberately indifferent to `active`: an old appointment must stay
        readable and reschedulable after its service is withdrawn.
        """
        try:
            return self._services[service_id]
        except KeyError:
            raise KeyError(f"no service with id {service_id!r}") from None

    def services(
        self, *, include_inactive: bool = False, client_bookable_only: bool = False
    ) -> List[Service]:
        """The catalogue, on sale first and alphabetical within that."""
        found = [
            s for s in self._services.values()
            if (include_inactive or s.active)
            and (not client_bookable_only or s.client_bookable)
        ]
        return sorted(found, key=lambda s: (not s.active, s.name.lower()))

    def bookable_durations(self) -> Tuple[int, ...]:
        """Distinct durations currently on sale, ascending.

        This is what `scheduling_engine`'s `CostConfig.service_durations`
        wants: gap usability is measured against what can still be sold, so a
        withdrawn service's length must not keep a gap looking useful.
        """
        return tuple(sorted({s.duration_minutes for s in self._services.values()
                             if s.active}))
