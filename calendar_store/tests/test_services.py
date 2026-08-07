import pytest

from calendar_store import ServiceCatalogue


@pytest.fixture
def catalogue() -> ServiceCatalogue:
    c = ServiceCatalogue()
    c.add_service("session-60", "Standard session", 60, 8000)
    c.add_service("session-90", "Extended session", 90, 11000)
    return c


def test_a_service_records_what_it_is_how_long_and_what_it_costs(catalogue):
    service = catalogue.get_service("session-60")

    assert service.name == "Standard session"
    assert service.duration_minutes == 60
    assert service.price_minor_units == 8000      # 80.00 in whatever currency
    assert service.active
    assert service.client_bookable


def test_ids_are_not_reused():
    catalogue = ServiceCatalogue()
    catalogue.add_service("session", "Session", 60, 8000)

    with pytest.raises(ValueError, match="already exists"):
        catalogue.add_service("session", "Something else", 90, 9000)


@pytest.mark.parametrize("duration,price,message", [
    (0, 8000, "duration"),
    (-30, 8000, "duration"),
    (60, -1, "price"),
])
def test_nonsense_is_refused(duration, price, message):
    with pytest.raises(ValueError, match=message):
        ServiceCatalogue().add_service("x", "X", duration, price)


# -- deactivation ----------------------------------------------------


def test_a_discontinued_service_stops_being_offered(catalogue):
    catalogue.deactivate_service("session-90")

    on_sale = [s.id for s in catalogue.services()]
    assert on_sale == ["session-60"]


def test_a_discontinued_service_is_still_resolvable(catalogue):
    """The whole reason for deactivating rather than deleting: an appointment
    booked before the service was withdrawn must still be readable, and
    reschedulable, afterwards.
    """
    catalogue.deactivate_service("session-90")

    service = catalogue.get_service("session-90")
    assert service.name == "Extended session"
    assert service.duration_minutes == 90
    assert not service.active


def test_discontinuing_can_be_undone(catalogue):
    catalogue.deactivate_service("session-90")
    assert catalogue.reactivate_service("session-90").active
    assert {s.id for s in catalogue.services()} == {"session-60", "session-90"}


def test_the_full_catalogue_can_be_listed_when_asked_for(catalogue):
    catalogue.deactivate_service("session-90")

    assert len(catalogue.services()) == 1
    assert len(catalogue.services(include_inactive=True)) == 2


def test_withdrawn_services_sort_after_the_ones_on_sale(catalogue):
    catalogue.add_service("aaa", "A first alphabetically", 60, 5000)
    catalogue.deactivate_service("session-60")

    listed = [s.id for s in catalogue.services(include_inactive=True)]
    assert listed[-1] == "session-60"


# -- provider-only entries -------------------------------------------


def test_provider_only_entries_are_hidden_from_clients(catalogue):
    """A lunch break occupies the calendar like anything else and so needs a
    service to be booked against, but must never appear as an option.
    """
    catalogue.add_service("break", "Break", 60, 0, client_bookable=False)

    assert "break" in {s.id for s in catalogue.services()}
    assert "break" not in {
        s.id for s in catalogue.services(client_bookable_only=True)
    }


# -- editing ---------------------------------------------------------


def test_price_and_name_can_be_corrected(catalogue):
    updated = catalogue.update_service(
        "session-60", price_minor_units=8500, name="Standard session (55 min)"
    )

    assert updated.price_minor_units == 8500
    assert updated.name == "Standard session (55 min)"
    assert updated.duration_minutes == 60, "unmentioned fields are left alone"


def test_the_id_cannot_be_edited(catalogue):
    with pytest.raises(ValueError, match="cannot change"):
        catalogue.update_service("session-60", id="something-else")


def test_editing_an_unknown_service_says_so(catalogue):
    with pytest.raises(KeyError, match="no service with id"):
        catalogue.update_service("nope", price_minor_units=1)


# -- what the engine needs -------------------------------------------


def test_bookable_durations_are_what_the_solver_measures_gaps_against(catalogue):
    assert catalogue.bookable_durations() == (60, 90)


def test_a_withdrawn_service_stops_making_gaps_look_useful(catalogue):
    """Gap usability is measured against what can still be sold. A 90-minute
    hole is only worth keeping while there is a 90-minute service to sell.
    """
    catalogue.deactivate_service("session-90")

    assert catalogue.bookable_durations() == (60,)


def test_durations_are_deduplicated(catalogue):
    catalogue.add_service("other-60", "Another hour", 60, 9000)

    assert catalogue.bookable_durations() == (60, 90)
