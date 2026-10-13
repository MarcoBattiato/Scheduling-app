# Calendars, payments and the AI assistant — plan

Three integrations with the outside world: putting appointments into the
calendars people already use, taking money for them, and letting people run
the app by talking to an AI assistant. All are written
against the interface-first approach in [INTERFACE_PLAN.md](INTERFACE_PLAN.md):
each is a **port** the domain asks for, with adapters behind it, so the
scheduler never learns that Google or Stripe exist.

Status: calendar steps 1–2 **built** (event rules and the subscription feed,
in `mock_ui/calendar_sync.py`); payments steps 1–2 **built** in test mode
(`mock_ui/billing.py`: ledger, pay-after-session, fees awaiting the provider,
a simulated processor and Stripe Checkout in test mode). Everything else is a
proposal, as is Part 3 beyond its first three steps (the guarded core, the
clinician chat and the assistant, built). Decisions still needed are collected at the
end.

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
- **Reconciliation, not hooks.** A booking changes in a dozen places (the
  solver, a settlement, a client's own move, the provider's time off…), and a
  missed hook is a calendar that silently drifts. Instead, `events_for(person)`
  says what a calendar *should* hold, and each push channel keeps what it last
  sent and sends `changes(before, after)` — created, updated, cancelled. A
  failed send is simply still different next time, so retrying is free, and
  each event's stable UID and sequence number mean a retry cannot duplicate.
  A Google outage therefore never blocks or loses a booking.
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

1. ✅ The event rules: what each person's calendar holds, identity by chain
   root, and `changes` for push channels.
2. ✅ **A** — the feed, with a per-person secret link that survives restarts
   and can be replaced if leaked.
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

**Ways to pay**, all offered side by side on the payment page:

| Method | Notes |
|---|---|
| Cards (Visa, Mastercard, Amex), Apple Pay, Google Pay | The only methods that can be **saved and charged later**, which fees and after-session charges need |
| **PayNow** | A QR code scanned in any participating app — the bank apps and **DBS PayLah!** among them — so PayLah is covered through PayNow rather than as a separate method. Lower fee than cards; no chargebacks; QR valid for an hour |
| **GrabPay** | Redirect to Grab to approve; no recurring payments |
| **Recorded by the provider** | Cash, bank transfer, or PayNow straight to the practice's own number — no processor and no fee. The provider marks it paid; the ledger records it like any other payment. |

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

1. ✅ The ledger and the rules, against Stripe **test mode** or a built-in
   simulated processor: per-session charges and pay-after-session, provider
   recording cash / transfer / PayNow-to-the-practice.
2. ✅ Fees (late cancellation, no-show), using the provenance already
   recorded — proposed only, each confirmed or waived by the provider.
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

## Part 3 — The AI assistant

Both the provider and clients can do everything either through the screens or
by talking to an assistant. The screens are never second-class: anything the
assistant can do, the GUI can do, so nobody has to use the AI.

### Where it lives — decided: inside the app

An **Assistant** tab in the phone app. The server sends the conversation to
Claude together with a set of *tools*; each tool is one command from
`scheduling_contracts`, and every call goes through the same permission gate
as the screens. No other app or account is involved. (Exposing the same
commands to the Claude apps over MCP stays possible later, behind the same
gate, once sign-in exists.)

### How it is guarded

The contracts already model this: an assistant is **the same person acting
through a restricted channel** (`Session.via_assistant`), and the permission
table says, per command, whether an assistant may do it freely (reads),
only as a draft the human confirms (every write), or never (money, settings
that steer the scheduler). Delegation is proved to only ever *narrow* what
the person holds.

- **The tool list is cut to the person.** A client's assistant is not given
  provider tools at all — and every call is checked again regardless.
- **Every change ends in a confirmation card** in the chat: "Move your Tue
  10:00 to Wed 11:00? [Confirm] [Cancel]". The card is bound to that exact
  change, so the model cannot propose one thing and execute another.
- **No money**, not even as a draft.
- **Text written by others cannot escalate.** A note or a name containing
  instructions can at most get the assistant to *propose* something the
  person was entitled to do anyway — which they would still have to confirm.
- **Logged**: every assistant action, with the conversation it came from —
  what it *did*, not what was *said*.

### Privacy — the part that decides whether this can be offered at all

Talking to the assistant sends text to the AI provider, outside Singapore and
outside the practice. Under the PDPA that raises consent and notification,
the transfer-limitation obligation, and protection and retention. And the
data is unusually sensitive: for a psychology practice, *being a client* is
sensitive before any clinical detail. So the design minimises what leaves:

**1. The model never learns who anybody is.**

- **A client's assistant carries no identifier at all.** Nobody can act on
  another client, so the tools always mean "me": *my* bookings, *my* hours.
  The model sees "you have a 60-minute appointment on Tuesday at 10:00" — no
  name, no id. Identity is held by the server's session, never in the
  conversation.
- **The provider's assistant** does need to tell clients apart, so each
  client gets a **pseudonym for that conversation only** ("Client A",
  "Client B"), assigned at random and never reused, so nothing is linkable
  across conversations. The server swaps real names out before sending and
  back in before showing; the provider sees real names, the model never does.
- **No clinical fields** reach the assistant: no service names, no notes, no
  attendance history beyond what scheduling needs (CONTROL_SURFACE.md §7
  already requires this).

What leaves the practice is therefore close to anonymous scheduling data:
durations, times, and availability, attached to nobody.

**2. Opt-in, never required.** Off by default. The practice decides whether
to offer it; each client switches it on themselves after a plain notice of
what is sent, to whom, and where.

**3. The right terms with the AI provider.** Anthropic's commercial API terms
do not use the data for training; a data processing agreement covers the
transfer-limitation obligation; shorter or zero data retention can be
arranged (check current terms). Alternatively, Claude through AWS or Google
Cloud may keep processing in an Asia-Pacific region — availability per region
to be checked. Required before going live, not for the demo.

**4. Everything else stays local.** Voice through the phone's own dictation,
so audio never goes to another company. Conversations kept no longer than
needed; erasing a client (crypto-shredding) erases their conversations too.

**5. A model on the practice's own computer.** Nothing leaves at all, at the
cost of markedly worse reliability. **Built** as an alternative to Claude
(`ASSISTANT_PROVIDER=local`, through Ollama): free, and what the prototype
uses until a Claude API key is in place. For a hosted deployment it needs a
server with a GPU, which costs far more than the API.

### What clients type: distress, and the clinician

One risk no amount of name-stripping removes: a client writes their own
messages, and a therapy client may write something clinical or alarming
("I'm not coping, I need to come in sooner"). Two answers:

**An internal chat with the clinician** — client ↔ provider messaging inside
the app, which never goes through the AI. Useful on its own ("running 10
minutes late"), and it is where the assistant hands over:

- **The assistant is for scheduling only, and says so** when it is first
  opened: it does not give advice, and what you tell it is not read by your
  clinician unless you send it to them.
- **When a message suggests distress** — or the client simply asks for a
  person — the assistant stops handling it as scheduling. It immediately
  shows emergency contacts (in Singapore, 995 or SMS 70995, Samaritans of
  Singapore on 1767 and the National Mindline on 1771; the practice confirms the numbers and wording), and offers: *"Would you
  like me to pass this to your clinician?"*
- **On yes, the conversation is transferred to the clinician chat**, the
  client's own words included, and the provider is notified. The client is
  told plainly when they can expect a reply, because the chat is not watched
  around the clock and is not an emergency service.
- **A "Message my clinician" button is always on screen**, AI or not. Detection
  by a model is not reliable enough to be the only way to reach a person.

Two things to be clear about:

- The distressing message *has already gone to the AI provider* by the time
  it is detected — it was typed into the assistant. The hand-off keeps
  everything after it in the practice.
- **Whether the transfer needs the client's yes** is a clinical decision.
  Asking respects the client's choice; forwarding automatically errs on the
  side of safety. The design supports either, as a practice setting. Forwarding
  without asking would be the single, deliberate exception to "an assistant
  never writes on its own", and the contract tests would record it as such.

Clinician messages are clinical data: stored encrypted per client, like notes,
and covered by erasure.

### Your examples against the rules

| Ask | How it is handled |
|---|---|
| Provider: "review the outstanding bookings" | Reads — allowed as is |
| Provider: compare the engine at different settings | A new **what-if read**: runs the scheduler at any settings and returns the results *without saving anything*. The assistant can compare as many as asked ("at 0.9 Client B moves to Monday and nothing is wasted"); turning one into a real proposal is a confirmed step |
| Provider: change the saved defaults | Forbidden to the assistant today; could become confirm-only. Practice's choice |
| Client: update my availability | Draft → confirmation card |
| Client: move my appointment | Draft → confirmation card |
| Client: "I want to talk to someone" | Hand-off to the clinician chat |

### Voice

Claude takes text, not audio, so speech becomes text first:

1. The phone keyboard's microphone — works today, free, and **audio stays on
   the phone**.
2. A mic button in the app using the browser's speech recognition — free;
   quality varies by browser and accent; some browsers send audio to their
   own vendor, so check before relying on it.
3. A speech-to-text service — best accuracy (Singapore English, mixed
   languages), small cost per minute, and **another company receiving
   audio**, which needs the same privacy treatment as the AI provider.

Replies can be read aloud with the phone's built-in speech, free and local.

### Model and cost

| Model | Price, input / output per million tokens | Use |
|---|---|---|
| Claude Opus 5.5 | US$4 / $20 | Strongest; the provider's analysis questions |
| Claude Sonnet 5.5 | US$2 / $10 | Half the price; likely enough for clients' tasks |
| Claude Haiku 4.5 | US$1 / $5 | Cheapest; simple exchanges only |

Roughly a few US cents per exchange on Opus, half that on Sonnet — a few
dollars a month for a small practice. Needs an Anthropic API key on the
server, kept like the Stripe key: in the environment, never in the app.

### Order

1. ✅ **The guarded core** (`mock_ui/control.py`): commands wired to the
   permission gate, per-person views with no identifiers (clients) or
   per-conversation pseudonyms (provider), server-written confirmation cards
   behind single-use tokens, the audit trail, and `plan.preview` — the
   what-if read. No AI is connected yet. This is also the first real consumer of
   `scheduling_contracts`, and moves INTERFACE_PLAN.md phase C forward.
2. ✅ **The clinician chat**, with "Messages" always in the client's tab bar.
   Through the gate from the start, never in the shared state, forbidden to
   assistants. No notifications yet (they need hosting), so the provider is
   alerted in the app only.
3. ✅ **The assistant** for both roles — a button on every screen rather than
   a tab — typed or dictated through the keyboard, with the opt-in notice,
   confirmation cards, and the hand-off (client's yes required; flagged for
   the provider). Needs `ANTHROPIC_API_KEY` on the server to talk to Claude.
4. **What-if comparisons** for the provider.
5. A mic button; a speech service only if dictation is not good enough.

The demo can run on the "view as" picker like the screens do. Going live
needs sign-in, the AI provider's terms (point 3 above), and the practice's
sign-off on the notice and the distress hand-off.

---

## Decisions still needed

1. A domain name for the practice (for sending email, and the app's address).
2. Whether this practice is GST-registered (asked of the client).
3. When to put the app online — which means doing sign-in (INTERFACE_PLAN.md
   phase C) first.

**AI assistant — for the practice to decide**

4. Whether to offer an AI assistant at all, given the notice and opt-in.
5. Distress hand-off: transfer only with the client's yes, or automatically?
   Should the provider be alerted, and how? The emergency numbers and wording.
6. How quickly the clinician chat is answered, so clients can be told.
7. Whether processing must stay in Singapore or the region — which decides
   between Anthropic directly and a cloud provider.
8. Whether the assistant may ever change saved scheduler settings (with
   confirmation), or only compare them.

**AI assistant — for you**

9. Model: Opus 5.5 for everyone, or Opus for the provider and Sonnet for
   clients.
10. An Anthropic API key, set as `ANTHROPIC_API_KEY` on the machine running
    the server, when building starts.
