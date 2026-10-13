"""Money: charges, fees, payments.

What matters most is that nobody is charged for something that was not their
doing, and nobody is charged twice: a fee for a client's own late
cancellation or no-show, never for the clinic cancelling or moving them or for
a client moving their own booking; every fee waits for the provider; a
payment counts only when the processor says so, not when a redirect claims it.
"""
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from calendar_store import Party

from mock_ui import billing, persistence, scenarios
from mock_ui.billing import BillingSettings, StripeProcessor, fee_amount, gst_included
from mock_ui.state import PROVIDER, World


@pytest.fixture
def world() -> World:
    w = World()
    w.catalogue.add_service("s60", "Standard session", 60, 8000)
    w.set_weekly_availability(PROVIDER, scenarios._hours(range(7), "08:00", "20:00"))
    w.add_client("dan", "Dan")
    w.set_weekly_availability("dan", scenarios._hours(range(7), "08:00", "20:00"))
    return w


def book(world, hours_from_now: float, client="dan"):
    start = (datetime.now() + timedelta(hours=hours_from_now)).replace(second=0, microsecond=0)
    return world.store.book_appointment(client, "s60", start, start + timedelta(hours=1))


def charges(world, kind=None):
    return [c for c in world.charges.values() if kind is None or c.kind == kind]


# -- sessions -------------------------------------------------------------


def test_attending_charges_the_session(world):
    appointment = book(world, -2)

    world.mark_attendance(appointment.id, attended=True)

    [charge] = charges(world)
    assert (charge.kind, charge.amount, charge.status) == ("session", 8000, "due")
    assert "session" not in charge.description.lower(), "what the client sees is not clinical"


def test_a_session_is_charged_once(world):
    appointment = book(world, -2)
    world.mark_attendance(appointment.id, attended=True)
    world._charge_for_session(world.store.get_appointment(appointment.id))

    assert len(charges(world)) == 1


def test_a_client_not_billed_through_the_app_is_not_charged(world):
    world.set_billing_mode("dan", "off")
    appointment = book(world, -2)

    world.mark_attendance(appointment.id, attended=True)

    assert charges(world) == []


def test_the_providers_own_time_is_never_charged(world):
    world.catalogue.add_service("admin", "Admin", 60, 5000, client_bookable=False)
    start = datetime.now() - timedelta(hours=2)
    block = world.store.book_appointment(PROVIDER, "admin", start, start + timedelta(hours=1))

    world.mark_attendance(block.id, attended=True)

    assert charges(world) == []


# -- fees: proposed, never charged on their own ------------------------------


def test_a_no_show_proposes_a_fee_and_charges_nothing(world):
    appointment = book(world, -2)

    world.mark_attendance(appointment.id, attended=False)

    [fee] = charges(world)
    assert (fee.kind, fee.status) == ("no_show", "proposed")
    assert not charges(world, "session")


def test_a_late_cancellation_proposes_a_fee_with_the_reason(world):
    appointment = book(world, 5)

    world.cancel_appointment(appointment.id, by=Party.CLIENT)

    [fee] = charges(world)
    assert (fee.kind, fee.status, fee.amount) == ("late_cancel", "proposed", 8000)
    assert "cancelled 4 h before" in fee.reason and "24 h" in fee.reason


def test_cancelling_in_good_time_costs_nothing(world):
    appointment = book(world, 72)

    world.cancel_appointment(appointment.id, by=Party.CLIENT)

    assert charges(world) == []


def test_the_clinic_cancelling_never_costs_the_client(world):
    appointment = book(world, 2)

    world.cancel_appointment(appointment.id, by=Party.PROVIDER)

    assert charges(world) == []


def test_a_client_moving_their_own_booking_is_not_a_cancellation(world):
    """They give the slot up early so it frees for others — and ask for
    another. That is a move, not a late cancellation."""
    appointment = book(world, 3)

    world.request_reschedule(appointment.id, (datetime.now() + timedelta(days=2)).isoformat(),
                             release_slot=True)

    assert charges(world) == []


def test_a_settled_move_does_not_charge_for_the_old_slot(world):
    appointment = book(world, 3)
    request = world.request_reschedule(
        appointment.id, (datetime.now() + timedelta(days=1)).replace(hour=10, minute=0).isoformat(),
        release_slot=False)
    plan_id = world.propose(request_ids=[request.id])["plan_id"]
    world.provider_approve(plan_id)
    world.respond_to_approval(next(iter(world.approvals.values())).id, "accept")
    world.settle_plan(plan_id, "agreed")

    assert world.store.get_appointment(appointment.id).status.value.startswith("cancelled")
    assert charges(world) == []


def test_confirming_a_fee_makes_it_owed(world):
    world.mark_attendance(book(world, -2).id, attended=False)
    fee = charges(world)[0]

    world.confirm_fee(fee.id)

    assert fee.status == "due"
    with pytest.raises(ValueError):
        world.confirm_fee(fee.id)


def test_a_waived_fee_is_never_owed(world):
    world.mark_attendance(book(world, -2).id, attended=False)
    fee = charges(world)[0]

    world.waive_charge(fee.id)

    assert fee.status == "waived"
    with pytest.raises(ValueError):
        world.start_checkout(fee.id, "http://x/phone")


@pytest.mark.parametrize("rule, cents", [("100%", 8000), ("50%", 4000), ("40.00", 4000),
                                         ("12.5", 1250), ("0", 0), ("0%", 0)])
def test_fee_rules(rule, cents):
    assert fee_amount(rule, 8000) == cents


@pytest.mark.parametrize("rule", ["half", "-10", "%"])
def test_nonsense_fee_rules_are_refused(rule):
    with pytest.raises(ValueError):
        fee_amount(rule, 8000)


def test_a_zero_fee_proposes_nothing(world):
    world.configure_billing(no_show_fee="0")

    world.mark_attendance(book(world, -2).id, attended=False)

    assert charges(world) == []


# -- GST ------------------------------------------------------------------


def test_unregistered_means_no_gst_anywhere(world):
    world.mark_attendance(book(world, -2).id, attended=True)

    assert charges(world)[0].gst == 0


def test_registered_gst_is_included_in_the_price_not_added(world):
    world.configure_billing(gst_registered=True, gst_number="M90000000X")

    world.mark_attendance(book(world, -2).id, attended=True)

    charge = charges(world)[0]
    assert charge.amount == 8000, "the price the client was shown is what they pay"
    assert charge.gst == 661, "S$80 including 9% GST holds S$6.61 of it"


def test_registering_needs_a_registration_number(world):
    with pytest.raises(ValueError):
        world.configure_billing(gst_registered=True)
    assert world.billing.gst_registered is False, "nothing changes unless all of it is valid"


def test_unknown_settings_are_refused(world):
    with pytest.raises(ValueError):
        world.configure_billing(currency_symbol="$")


# -- paying -----------------------------------------------------------------


def owed(world):
    world.mark_attendance(book(world, -2).id, attended=True)
    return charges(world)[0]


def test_a_payment_counts_only_when_the_processor_says_so(world):
    charge = owed(world)
    payment = world.start_checkout(charge.id, "http://app/phone?as=dan")

    assert world.check_payment(payment.id).status == "pending"
    assert charge.status == "due"

    world.processor.complete(payment.reference, "succeeded", "paynow")
    world.check_payment(payment.id)

    assert (payment.status, payment.method, charge.status) == ("succeeded", "paynow", "paid")
    assert charge.paid_by == payment.id


def test_checking_twice_does_not_pay_twice(world):
    charge = owed(world)
    payment = world.start_checkout(charge.id, "http://app/phone")
    world.processor.complete(payment.reference, "succeeded", "card")

    world.check_payment(payment.id)
    world.check_payment(payment.id)

    assert len([p for p in world.payments.values() if p.status == "succeeded"]) == 1


def test_a_failed_payment_leaves_it_owed_and_can_be_tried_again(world):
    charge = owed(world)
    first = world.start_checkout(charge.id, "http://app/phone")
    world.processor.complete(first.reference, "failed", "card")
    world.check_payment(first.id)

    assert (first.status, charge.status) == ("failed", "due")
    second = world.start_checkout(charge.id, "http://app/phone")
    world.processor.complete(second.reference, "succeeded", "grabpay")
    world.check_payment(second.id)

    assert charge.status == "paid"


def test_the_return_address_says_which_payment_to_check(world):
    payment = world.start_checkout(owed(world).id, "http://app/phone?as=dan")

    assert payment.return_url == f"http://app/phone?as=dan&paid={payment.id}"


def test_the_provider_can_record_money_taken_outside_the_app(world):
    charge = owed(world)

    payment = world.record_payment(charge.id, "paynow_direct")

    assert (payment.processor, charge.status) == ("manual", "paid")
    with pytest.raises(ValueError):
        world.record_payment(charge.id, "cash")             # already paid


def test_a_recorded_payment_is_cash_transfer_or_paynow_never_a_card(world):
    with pytest.raises(ValueError):
        world.record_payment(owed(world).id, "card")


def test_a_paid_charge_cannot_be_waived(world):
    charge = owed(world)
    world.record_payment(charge.id, "cash")

    with pytest.raises(ValueError):
        world.waive_charge(charge.id)


# -- Stripe, test mode only ---------------------------------------------------


def test_a_live_stripe_key_is_refused():
    with pytest.raises(ValueError):
        StripeProcessor("sk_live_abc", client=SimpleNamespace())


def test_a_live_key_in_the_environment_falls_back_to_the_simulation(monkeypatch):
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_live_abc")

    processor, warning = billing.processor_from_environment()

    assert processor.name == "simulated" and "test" in warning


def _fake_stripe(session):
    created = {}

    class Session:
        @staticmethod
        def create(**kwargs):
            created.update(kwargs)
            return SimpleNamespace(id="cs_test_1", url="https://checkout.stripe.test/cs_test_1")

        @staticmethod
        def retrieve(reference, expand=None):
            return session

    return SimpleNamespace(checkout=SimpleNamespace(Session=Session), api_key=None), created


def test_stripe_checkout_is_asked_for_the_right_amount_and_nothing_clinical(world):
    client, created = _fake_stripe(None)
    world.processor = StripeProcessor("sk_test_abc", client=client)
    charge = owed(world)

    payment = world.start_checkout(charge.id, "http://app/phone?as=dan")

    line = created["line_items"][0]["price_data"]
    assert (line["currency"], line["unit_amount"]) == ("sgd", 8000)
    assert "Standard" not in line["product_data"]["name"]
    assert created["success_url"] == payment.return_url
    assert payment.checkout_url.startswith("https://checkout.stripe.test/")


@pytest.mark.parametrize("session, expected", [
    (SimpleNamespace(payment_status="paid", status="complete",
                     payment_intent=SimpleNamespace(payment_method=SimpleNamespace(type="paynow"))),
     ("succeeded", "paynow")),
    (SimpleNamespace(payment_status="unpaid", status="expired", payment_intent=None), ("expired", "")),
    (SimpleNamespace(payment_status="unpaid", status="open", payment_intent=None), ("pending", "")),
])
def test_stripe_outcomes_are_read_from_stripe(session, expected):
    client, _ = _fake_stripe(session)
    assert StripeProcessor("sk_test_abc", client=client).check("cs_test_1") == expected


# -- surviving a restart -----------------------------------------------------


def test_money_survives_a_restart(world, tmp_path):
    world.configure_billing(late_cancel_fee="50%", gst_registered=True, gst_number="M9X")
    charge = owed(world)
    pending = world.start_checkout(charge.id, "http://app/phone")
    path = tmp_path / "s.json"
    persistence.save(world, path)

    restored = persistence.load(path)
    restored.processor.complete(pending.reference, "succeeded", "paynow")
    restored.check_payment(pending.id)

    assert restored.billing.late_cancel_fee == "50%" and restored.billing.gst_registered
    assert restored.charges[charge.id].status == "paid", "a checkout in progress survives too"


# -- over HTTP ------------------------------------------------------------


@pytest.fixture
def client():
    from fastapi.testclient import TestClient
    from mock_ui import app as app_module
    c = TestClient(app_module.app)
    c.post("/api/reset?scenario=busy")
    return c


def due_charge(client, who="dan"):
    state = client.get("/api/state").json()
    return next(c for c in state["charges"] if c["client_id"] == who and c["status"] == "due")


def test_paying_end_to_end_through_the_simulated_page(client):
    charge = due_charge(client)
    start = client.post(f"/api/charges/{charge['id']}/checkout", json={"return_to": "/phone?as=dan"}).json()

    page = client.get(start["url"])
    assert page.status_code == 200 and "S$80.00" in page.text and "PayLah" in page.text

    back = client.post(start["url"], data={"outcome": "succeeded", "method": "paynow"},
                       follow_redirects=False)
    assert back.status_code == 303 and f"paid={start['payment_id']}" in back.headers["location"]

    checked = client.post(f"/api/payments/{start['payment_id']}/check").json()
    assert checked == {"status": "succeeded", "method": "paynow", "charge_status": "paid"}


def test_the_return_address_cannot_point_elsewhere(client):
    charge = due_charge(client)
    for bad in ("https://evil.example/", "//evil.example/x"):
        response = client.post(f"/api/charges/{charge['id']}/checkout", json={"return_to": bad})
        assert response.status_code == 400


def test_the_busy_example_has_money_in_every_state(client):
    statuses = {c["status"] for c in client.get("/api/state").json()["charges"]}

    assert {"paid", "due", "proposed"} <= statuses


def test_fee_decisions_over_http(client):
    fee = next(c for c in client.get("/api/state").json()["charges"] if c["status"] == "proposed")

    assert client.post(f"/api/charges/{fee['id']}/confirm").status_code == 200
    assert client.post(f"/api/charges/{fee['id']}/confirm").status_code == 409
    assert client.post("/api/charges/999999/waive").status_code == 404


def test_billing_settings_over_http(client):
    ok = client.post("/api/billing", json={"late_cancel_window_hours": 48, "late_cancel_fee": "50%"})
    bad = client.post("/api/billing", json={"gst_registered": True, "gst_number": ""})

    assert ok.status_code == 200 and ok.json()["late_cancel_window_hours"] == 48
    assert bad.status_code == 400
