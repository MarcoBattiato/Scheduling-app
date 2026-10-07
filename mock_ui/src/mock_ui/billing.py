"""Money: what is owed, what was paid, and the rules that decide it.

The app keeps its own ledger; a payment processor is where money moves, not
the record of it. Amounts are integer cents (as `price_minor_units` already
is) in the practice's currency, SGD.

What is built is stage 1–2 of docs/INTEGRATIONS_PLAN.md: a session is charged
once the provider marks it attended, the client pays from their phone (or the
provider records cash, a transfer, or PayNow straight to the practice), and
late-cancellation and no-show fees are *proposed* — never charged until the
provider confirms each one. Paying at booking, packages and monthly invoices
are later stages; their settings are not offered yet.

Fees are fair by construction, because the facts they need are already
recorded: who cancelled (the client, or the clinic), and whether a slot was
given up as part of a move. A fee is only ever proposed for a client
cancelling, outright, inside the window. The clinic cancelling, the clinic
moving somebody, or a client moving their own booking never proposes one.
"""
from __future__ import annotations

import os
import secrets
from dataclasses import asdict, dataclass, field
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Dict, Optional, Tuple

# Billing modes. Only those built are accepted; the rest are documented in the
# plan and arrive with their stage.
MODES = {
    "after_session": "Pay after each session",
    "off": "Not billed through the app",
}
CHARGE_KINDS = ("session", "late_cancel", "no_show")

# proposed — a fee the rules suggest; nothing is owed until the provider confirms
# due      — owed
# paid     — settled
# waived   — the provider let it go
CHARGE_STATUSES = ("proposed", "due", "paid", "waived")

PAY_METHODS = {
    "paynow": "PayNow (any bank app, or DBS PayLah!)",
    "card": "Card",
    "grabpay": "GrabPay",
    "cash": "Cash",
    "bank_transfer": "Bank transfer",
    "paynow_direct": "PayNow to the practice",
}
RECORDED_METHODS = ("cash", "bank_transfer", "paynow_direct")


@dataclass
class BillingSettings:
    default_mode: str = "after_session"
    late_cancel_window_hours: int = 24
    # "100%", "50%", or a fixed amount like "40.00". "0" turns it off.
    late_cancel_fee: str = "100%"
    no_show_fee: str = "100%"
    # Unregistered is the default: prices are final and nothing about GST is
    # shown. Registered: prices still *include* GST — Singapore requires prices
    # shown to consumers to be GST-inclusive — and the included part is shown
    # on receipts beside the registration number.
    gst_registered: bool = False
    gst_number: str = ""
    gst_rate_percent: float = 9.0
    currency: str = "SGD"


@dataclass
class Charge:
    id: int
    client_id: str
    kind: str                       # CHARGE_KINDS
    amount: int                     # cents, what the client pays (GST included)
    gst: int                        # cents of `amount` that are GST; 0 if unregistered
    description: str                # what the client sees — no clinical detail
    created: datetime
    appointment_id: Optional[int] = None
    status: str = "due"
    # Why a fee was proposed, for the provider deciding whether to confirm it.
    reason: str = ""
    paid_by: Optional[int] = None   # the Payment that settled it


@dataclass
class Payment:
    id: int
    charge_id: int
    amount: int
    processor: str                  # "simulated" | "stripe" | "manual"
    reference: str                  # the processor's id, or a note for manual ones
    status: str = "pending"         # pending | succeeded | failed | expired
    method: str = ""                # PAY_METHODS key, once known
    created: datetime = field(default_factory=datetime.now)
    checkout_url: str = ""
    return_url: str = ""            # where the client comes back to after paying


# -- fees ------------------------------------------------------------------


def fee_amount(rule: str, price: int) -> int:
    """`rule` is "50%" or "40.00" (dollars); returns cents, never negative."""
    rule = (rule or "0").strip()
    try:
        if rule.endswith("%"):
            cents = Decimal(price) * Decimal(rule[:-1]) / 100
        else:
            cents = Decimal(rule) * 100
    except Exception:
        raise ValueError(f"a fee is a percentage like 50% or an amount like 40.00, not {rule!r}")
    if cents < 0:
        raise ValueError("a fee cannot be negative")
    return int(cents.quantize(Decimal(1), rounding=ROUND_HALF_UP))


def gst_included(amount: int, settings: BillingSettings) -> int:
    """The GST inside a GST-inclusive amount; 0 when not registered."""
    if not settings.gst_registered:
        return 0
    rate = Decimal(str(settings.gst_rate_percent))
    return int((Decimal(amount) * rate / (100 + rate)).quantize(Decimal(1), rounding=ROUND_HALF_UP))


def money(cents: int, currency: str = "SGD") -> str:
    return f"{'S$' if currency == 'SGD' else currency + ' '}{cents / 100:,.2f}"


def validate(settings: BillingSettings) -> None:
    if settings.default_mode not in MODES:
        raise ValueError(f"billing mode must be one of {sorted(MODES)}")
    if not 0 <= settings.late_cancel_window_hours <= 24 * 14:
        raise ValueError("the late-cancellation window is 0 to 336 hours")
    fee_amount(settings.late_cancel_fee, 10000)
    fee_amount(settings.no_show_fee, 10000)
    if not 0 <= settings.gst_rate_percent <= 30:
        raise ValueError("a GST rate is a percentage between 0 and 30")
    if settings.gst_registered and not settings.gst_number.strip():
        raise ValueError("a GST-registered practice needs its GST registration number")


# -- processors ------------------------------------------------------------


class SimulatedProcessor:
    """Stands in for Stripe so payments can be tried with no account at all.

    The "hosted page" is served by this app (`/pay/simulated/{reference}`):
    it shows the amount, lets you choose PayNow, card or GrabPay, and pay or
    fail. Everything after that — returning to the app, the app asking the
    processor what happened — runs exactly as it will with Stripe.
    """
    name = "simulated"
    label = "Simulated payments (no real money, no account needed)"

    def __init__(self) -> None:
        self.outcomes: Dict[str, Tuple[str, str]] = {}    # reference -> (status, method)

    def start_checkout(self, charge: Charge, description: str, return_url: str,
                       currency: str) -> Tuple[str, str]:
        reference = "sim_" + secrets.token_urlsafe(12)
        self.outcomes[reference] = ("pending", "")
        return reference, f"/pay/simulated/{reference}"

    def complete(self, reference: str, status: str, method: str) -> None:
        if reference not in self.outcomes:
            raise KeyError(reference)
        self.outcomes[reference] = (status, method)

    def check(self, reference: str) -> Tuple[str, str]:
        return self.outcomes.get(reference, ("expired", ""))


class StripeProcessor:
    """Stripe Checkout, **test mode only**.

    The key comes from the environment (`STRIPE_SECRET_KEY`), never from a
    request or the session file, and a live key is refused outright: real
    money waits for sign-in (CONTROL_SURFACE.md §8). Checkout is Stripe's own
    hosted page, so card details never reach this app; which methods it
    offers (cards, PayNow, GrabPay) is set in the Stripe dashboard.

    Whether a payment succeeded is asked of Stripe when the client comes back,
    not taken from the redirect — a return address can be typed by anyone.
    """
    name = "stripe"
    label = "Stripe (test mode)"

    def __init__(self, secret_key: str, client=None) -> None:
        if not secret_key.startswith(("sk_test_", "rk_test_")):
            raise ValueError("only a Stripe *test* key is accepted until sign-in exists")
        if client is None:
            import stripe as client                      # optional dependency
        self.stripe = client
        self.stripe.api_key = secret_key

    def start_checkout(self, charge: Charge, description: str, return_url: str,
                       currency: str) -> Tuple[str, str]:
        joiner = "&" if "?" in return_url else "?"
        session = self.stripe.checkout.Session.create(
            mode="payment",
            line_items=[{
                "quantity": 1,
                "price_data": {
                    "currency": currency.lower(),
                    "unit_amount": charge.amount,
                    # What Stripe, and the client's statement, see. Kept free
                    # of anything clinical, like the calendar entries.
                    "product_data": {"name": description},
                },
            }],
            client_reference_id=str(charge.id),
            metadata={"charge_id": str(charge.id)},
            success_url=return_url,
            cancel_url=return_url + f"{joiner}cancelled=1",
        )
        return session.id, session.url

    def check(self, reference: str) -> Tuple[str, str]:
        session = self.stripe.checkout.Session.retrieve(
            reference, expand=["payment_intent.payment_method"])
        if session.payment_status == "paid":
            method = ""
            intent = getattr(session, "payment_intent", None)
            if intent is not None and getattr(intent, "payment_method", None) is not None:
                method = intent.payment_method.type          # card | paynow | grabpay
            return "succeeded", method
        if session.status == "expired":
            return "expired", ""
        return "pending", ""


def processor_from_environment():
    """Stripe when a test key is configured, otherwise the simulation."""
    key = os.environ.get("STRIPE_SECRET_KEY", "").strip()
    if not key:
        return SimulatedProcessor(), None
    try:
        return StripeProcessor(key), None
    except Exception as exc:          # a live key, or the library missing
        return SimulatedProcessor(), f"Stripe not used ({exc}); payments are simulated"


def settings_dict(settings: BillingSettings) -> dict:
    return asdict(settings)
