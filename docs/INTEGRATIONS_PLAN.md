# Calendars and payments — plan

Two integrations with the outside world: putting appointments into the
calendars people already use, and taking money for them. Both are written
against the interface-first approach in [INTERFACE_PLAN.md](INTERFACE_PLAN.md):
each is a **port** the domain asks for, with adapters behind it, so the
scheduler never learns that Google or Stripe exist.

Status: **proposal**. Nothing here is built. Decisions still needed are
collected at the end.

---

## Part 1 — Calendar sync

### What gets synced

Only **settled** facts leave the app:

| In the app | In people's calendars |
|---|---|
| Booking written to the diary | Event created |
| Booking moved (any reason) | **Same** event updated — not a cancel plus a new one |
| Booking cancelled (by anyone) | Event cancelled / removed |
| Offer out, or agreed but not yet confirmed | **Nothing.** An event that later vanishes is worse than none. |
| Provider's own blocks (admin, breaks) | Provider's calendar only |

A move must look like a move. The store records a reschedule as a *new*
appointment linked to the old one by `supersedes`; the calendar event's
identity therefore follows the **root of that chain**, so a booking moved three
times is still one event with a history, not three cancellations.

### The channels — all of them, because they serve different people

| Channel | Who | Speed | Needs |
|---|---|---|---|
| **A. Subscription feed** (`.ics` link) | Anyone; set up once | Apple: minutes. Google: **hours** — Google refreshes subscribed calendars on its own schedule, typically several times a day at best | A secret per-person URL. For Google to fetch it, the server must be reachable from the internet. |
| **B. Email invitations** (iCalendar `REQUEST` / `CANCEL`) | Clients by default | Immediate | An email-sending service (e.g. Postmark, Amazon SES, Resend). Works in Google, Apple, Outlook and anything else that reads mail. |
| **C. Google Calendar, direct** (Google Calendar API) | The provider; clients who opt in | Immediate | A Google Cloud project with OAuth consent; each person connects their account once. |
| **D. Microsoft 365 / Outlook, direct** (Graph API) | Same, for Outlook users | Immediate | An Azure app registration; same shape as C. |
| **E. Apple iCloud, direct** (CalDAV) | Apple users who want direct writes | Immediate | **An app-specific password from the user.** Apple has no OAuth calendar API, so this is the only direct route. Clunky to set up; offered last. |

Recommended defaults, with everything else available as an option in settings:

- **Provider:** C (or D) — one account, connected once, instant.
- **Clients:** B — no account linking, works with every calendar.
- **A** for anyone who prefers a subscription, with the speed caveat stated.
- **E** only on request.

### Reading the provider's calendar too (optional, later)

Once the provider's Google/Outlook calendar is connected, its *other* events
(dentist, school run) can be read as **busy time** and subtracted from the
provider's availability. The scheduler then stops offering hours the provider
does not actually have. Same rule as everywhere else: this only *flags*
bookings that now conflict; it never reschedules anybody by itself.

### What goes in an event

Client calendars are often shared with a partner, a family, or an employer.

- **Client's event:** "Appointment — [practice name]", time, address or video
  link. **No service name, no clinician speciality, no notes.**
- **Provider's event:** client name and service; still no clinical notes.

That is a PDPA (Singapore's Personal Data Protection Act) minimisation point
as much as a courtesy.

### How it fits the architecture

- A new port, `CalendarPublisher`, beside the planned `NotificationPort`:
  `publish(event_id, change)` where change is created / updated / cancelled.
- An **outbox**: every settled change writes a row in the same step as the
  booking; a worker sends it and retries on failure. A Google outage must
  never block or lose a booking, and a retry must never create a duplicate
  (each event carries a stable UID and a sequence number).
- Per person: which channels are on, their tokens (encrypted), and the
  external event ids, so updates and cancellations reach the right event.

### What can be tried locally

| Channel | Locally? |
|---|---|
| A — feed | Apple Calendar on the same Wi-Fi: yes. Google: no, it needs a public URL. |
| B — email | Yes, with a sending service's test mode, or a local mail catcher. |
| C/D — Google/Outlook | Yes: OAuth allows `localhost` redirects for development. |
| E — iCloud | Yes, with a real app-specific password. |

### Order

1. Outbox and the event-identity rule (chain root → UID). Pure domain work.
2. **A** — the feed. No third party; proves the event mapping end to end.
3. **B** — email invitations. Also delivers the first real **notifications**,
   which the app does not have at all today.
4. **C** — Google for the provider.
5. Busy-time import from Google.
6. **D**, then **E**.

---

## Part 2 — Payments

### Scope, as decided

- **What is charged:** per session, prepaid packages, late-cancellation fees,
  and no-show fees — all of them.
- **When:** at booking, after the session, and monthly invoices — all of them,
  chosen **per client** by the provider, with a practice default.
- **Who pays:** the client. Insurance and third-party payers are out of scope
  for now; the design leaves room for a "payer" distinct from the client.
- **Where:** Singapore, prices in **SGD**.

### Processor: Stripe

Available in Singapore, and covers everything above:

| Need | Stripe feature |
|---|---|
| Card payments | Checkout / Payment Element — hosted, so **card data never touches this app** |
| PayNow (the usual way to pay in Singapore) | Supported — but it **cannot be charged later without the client** |
| Card on file, for fees and after-session charges | Saved payment methods (SetupIntent), charged "off-session" |
| Monthly invoices | Stripe Invoicing, payable by card or PayNow |
| Refunds | Refunds API |
| Testing | Test mode, test cards; local webhooks via the Stripe CLI |

The PayNow point matters: a client who only pays by PayNow can be sent a
payment link for a fee, but cannot be charged automatically. So
"charge late-cancellation fees automatically" needs a card on file, and the
app should say so when a client has none.

### The money model

The app keeps its own **ledger**; Stripe is the processor, not the record.
Amounts in cents (`price_minor_units` already is).

- **Charge** — what is owed: a session, a package, a fee. Linked to the
  appointment (by chain root, so a moved session stays paid once).
- **Payment** — money received against charges; from a Stripe webhook.
- **Refund** — money returned.
- **Credit** — prepaid sessions from a package: bought, used, expired.

### The rules

| Event | What happens |
|---|---|
| **Pay at booking** | Paying is part of accepting the offer: "Accept and pay". If payment fails, the offer stays open, and the slot is held only for a short, provider-set window. |
| **Pay after the session** | Charge raised when the provider marks attendance; card on file charged, or a payment link sent. |
| **Monthly invoice** | At month end, one invoice for the month's attended sessions and fees. |
| **Package** | Bought up front; each attended session uses a credit. Expiry and refundability are provider settings. |
| **Client cancels late** | A fee is **proposed** if inside the provider's window (e.g. 24 h before) — a fixed amount or a percentage of the price — and **charged only once the provider confirms it**, case by case. Never automatic. |
| **No-show** | Fee **proposed** when the provider marks "didn't show"; charged only on the provider's confirmation, as above. |
| **Clinic cancels or moves the client** | **Never a fee.** A paid session is refunded or kept as credit, the client's choice. |
| **Client declines a proposed move** | Never a fee — it was the clinic's question. |

The app already records the facts these rules need: **who** cancelled
(client or provider) and whether a booking was **moved by the clinic**
(`Origin.DISPLACED`). So the fee rules can be fair by construction.

### Singapore specifics — to confirm with an accountant

- **GST** is 9%. Registration is compulsory once taxable turnover exceeds
  S$1 million a year, and voluntary below that. As far as I know, healthcare
  and counselling services are **not** GST-exempt in Singapore. The app
  should support both registered (tax invoices showing the GST registration
  number) and not registered.
- **Invoices and receipts**: sequential numbering, practice details, date,
  description, amount; GST breakdown when registered.
- **PDPA** applies to payment records as to everything else.

None of this is legal or tax advice; it shapes what the app must be able to
do, not what the practice must charge.

### Architecture

- A `PaymentPort` (Stripe adapter behind it), as already sketched in
  CONTROL_SURFACE.md §K.
- Commands, from that table plus what the scope adds: `invoice.issue`,
  `invoice.read`, `payment.take`, `payment.refund`, `payment_method.add`,
  `package.sell`, `fee_policy.set`, `billing_mode.set`.
- **No assistant writes in this group, not even drafts** (already decided
  there).
- Webhooks are the source of truth for "paid": the app never assumes a
  payment succeeded because a page said so.

### Order

**Real money waits for real sign-in.** Today anyone on the network can act as
anyone (CONTROL_SURFACE.md §8), and taking payments on top of that would be
taking them on behalf of whoever typed a name. So:

1. Build the ledger and the rules against Stripe **test mode**, in the mock:
   per-session charges and pay-after-session first, since they touch the
   fewest moving parts.
2. Fees (late cancellation, no-show), using the provenance already recorded.
3. Pay at booking ("Accept and pay").
4. Packages and credits.
5. Monthly invoices.
6. **Live mode only after** phase C of INTERFACE_PLAN.md: sign-in, identity
   from the session, ownership checks.

---

## Settings this implies

Everything a practice might reasonably decide differently is a setting, with
the provider able to override per client where it makes sense:

| Setting | Values |
|---|---|
| Billing mode (practice default, per-client override) | pay at booking · after the session · monthly invoice · package |
| Late-cancellation window | hours before the session, e.g. 24 or 48 |
| Late-cancellation fee | fixed amount, or % of the session price; 0 = no fee |
| No-show fee | fixed amount, or % of the session price; 0 = no fee |
| Fee confirmation | **always on**: every fee waits for the provider to confirm it |
| Pay-at-booking hold | how long an offered slot is held while the client pays |
| Package credits expire | never, or after N days |
| Unused package credits refundable | yes / no |
| GST | **not registered (default)** · registered (GST number, rate) |

## Decided

- **Fees are never charged automatically.** Every late-cancellation or no-show
  fee is proposed and waits for the provider to confirm or waive it.
- **Packages:** expiry and refundability are both settings, not fixed rules.
- **GST:** a setting, and the app must work fully for a business that is
  **not** registered — that is the default. Not registered: prices are
  final, no GST is added or shown, and documents are plain invoices and
  receipts. Registered: GST is added at the configured rate and documents
  become tax invoices carrying the GST registration number. Whether this
  particular practice is registered is to be confirmed with the client.
- **Pay-at-booking hold:** a setting.
- **Email:** Resend's free tier to start (3,000 emails a month, 100 a day),
  sending from the practice's own domain. Brevo (300 a day free) is the
  fallback; Amazon SES is cheapest at volume but the most work to set up.
- **Hosting:** a small always-on server in Singapore — Fly.io is the
  recommendation (low monthly cost, HTTPS and a public address built in),
  with DigitalOcean as the alternative. **Not before sign-in exists** (see
  below): today the app has no login, so putting it on the internet would let
  anyone read every client's name and bookings.

## Decisions still needed

1. A domain name for the practice (for sending email, and the app's address).
2. Whether this practice is GST-registered (asked of the client).
3. When to put the app online — which means doing sign-in (INTERFACE_PLAN.md
   phase C) first.
