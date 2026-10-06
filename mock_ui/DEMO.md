# Showing this to somebody

A click-path that tells a story, so the demonstration is not improvised. About
fifteen minutes at a walk.

## Before they arrive

```bash
mock_ui/run.sh
```

Open **http://127.0.0.1:8000/?as=provider**. In the header, pick **Small
example** and press **Start over**.

Open a second tab on **?as=carol** and leave it on another window or desktop.
Both tabs talk to the same server, so an action in one shows up in the other
within a couple of seconds — that is the whole reason this is a server and not
a static page, and it is worth saying out loud when you first switch.

Check the calendar shows next week with bookings in it. If it is empty, the
scenario did not load.

> **Do not press Start over during the demo.** It throws the session away.

---

## 1. The week (1 min)

Provider, Schedule tab. Four clients, mornings only — 09:00 to 13:00, so four
appointments fill a day.

- Light blue is when the provider works.
- Green blocks are booked appointments.
- **Hover one.** The card shows who, what, the price, what they originally
  asked for, and their history. The calendar dims to *that client's* own
  availability, which is the question you actually have when looking at a
  booking: could this person be somewhere else?

Point at **Carol**: she works, and can only ever manage Tuesday mornings.

## 2. What is waiting (1 min)

**Booking queue** panel, right-hand side. Two requests.

Carol wants Tuesday 09:00 — and both Tuesday morning slots are taken.

Note the tick boxes: a run covers what you choose. By default that is
everything wishing for a time inside the planning window, which is what "do
next week on Monday" means. Requests wanting a date beyond the window are
dimmed and left out, so somebody who asked for next month does not get dragged
into this week.

## 3. Run it (2 min)

**Run on 2 selected.**

A proposal appears — on a calendar, not as a list, because a proposal is only
legible against the week it would land in.

- **Bob** gets a new booking. Straightforward.
- **Dan** is asked to move. There is an **arrow** from where he is to where he
  would go, the old slot in orange, the new one in brown.
- **Carol** lands in the slot Dan is vacating. Hover it: **"Only if Dan moves
  first."** The engine reports that dependency rather than anyone guessing it,
  and it is the one thing about a plan that must not be misread.

Nothing is booked. Nothing is even asked yet.

## 4. The trade-off is a dial (3 min) — *the moment worth taking slowly*

Look at **Wednesday** first. A long session ends at 10:30 and the provider's
admin hour starts at 12:00, so there is a ninety-minute stretch in between.
**Bob asked for 11:00.** Give him that and half an hour is stranded — too short
to sell to anybody.

In the header, set **alpha** and press **Try**, twice:

- **alpha 0.5** — Bob gets Wednesday 11:00, the slot he asked for. The week
  carries 30 minutes of waste.
- **alpha 0.9** — Bob is moved to **Monday 12:00**. Nothing is wasted, and he
  is seen two days before he wanted.

Both proposals sit **side by side**, each with its own calendar and its own
numbers: *waste* (minutes of unsellable gap) and *off-wish* (how far people
ended up from the time they wanted). Watch those two numbers trade against
each other.

This is the point to land: the software does not decide which matters more.
**They do**, and they can see what the choice costs before making it.

Ask them which they would pick. Their answer is worth more than the rest of
the demo put together — it is the one number in this system we cannot derive
and have been guessing at.

> If they ask where it flips: between 0.75 and 0.8 on this example. Be ready
> for a harder question — *"how much is half an hour of dead time worth
> against a day of someone's inconvenience?"* Today the model prices them
> equal-ish (one hour of waste ≈ one day of delay), which is a placeholder,
> not a finding. Their answer is what replaces it.

## 5. Ask the clients (2 min)

Approve one of the proposals. Nothing is written — approving means *this is
worth asking about*.

Switch to the **Carol** tab. She has been offered a slot, and can accept or
decline.

Have her **accept**. Then look again:

- Her calendar still shows the proposed slot, now outlined in green: *agreed,
  waiting for the clinic to confirm*.
- Nothing is in the diary yet.

Two gates, and this is why: a half-applied rearrangement is often worse than
none. Three people move so a fourth can be booked, the fourth says no, and
three moves happened for nothing.

Also worth showing: a client asked to **move** has three answers, not two.
*Accept*, *not that time* (blocks that slot only — we will look for another),
and *keep my slot* (we stop asking). Only the client can tell the last two
apart, and the difference is worth a great deal to the scheduler.

## 6. Settle it (2 min)

Back on the provider tab. The answers are grouped: agreed, waiting, said no.
Three things you can do:

- **Apply the agreed** — write down what is agreed, keep waiting for the rest.
- **Apply agreed, drop the waiting** — stop waiting; those requests go back in
  the queue rather than being lost.
- **Reject all and re-plan** — none of it, and the scheduler runs again.

Apply the agreed. *Now* it is in the calendar.

## 7. You are ill on Wednesday (3 min) — *the other moment worth taking slowly*

**"I cannot work"** panel. Wednesday, all day. Leave the action on **just flag
what is in it**.

A warning appears listing the bookings now sitting in time the provider does
not have. **Nothing has been rescheduled.** Nobody has been contacted.

Say why: a mistyped date must never put half the week's clients through a
rescheduling nobody asked for. Changing your hours is not the same as deciding
what to do about it.

Then decide, explicitly:

- **Find them a new slot** — each client's hour is *kept* until there is
  somewhere to move it to, and it goes through the queue like any request, so
  they are asked rather than told.
- or **Cancel them.**

Choose the first, then run the scheduler again to see the replacements found.
Those bookings come back marked as moved by the clinic, not chosen — so
nothing downstream ever mistakes a slot somebody was pushed into for one they
like.

---

## If you have longer

- **Realistic practice** scenario: ten clients, a full-ish book, and a history
  behind it — attendance, a no-show, a cancellation, somebody moved before. The
  hover cards and client summaries have something to say on this one.
- **Place by hand**: the *place* link on a queued request books it yourself,
  locked, so the next run plans around it instead of proposing it.
- **A client moving themselves**: on a client tab, *move* on one of their own
  bookings — and the choice of whether to give the hour up now or keep it until
  there is somewhere to go.
- **Weekly pattern vs. single date**: the side grid is what every week looks
  like; dragging on the dated calendar overrides one date only. Shift-drag
  marks time away.

## Questions you will be asked, and the honest answers

**"How does the client know they have been asked?"** They do not, yet. There
are no notifications — they see it when they open the page. That is the next
thing to build, and it needs their input on how: text, email, or a phone call
the provider records themselves.

**"What if they never reply?"** The slot is held indefinitely. Sensible expiry
defaults are agreed but not built, because they need real elapsed time.

**"Is this secure?"** Not yet. There is no login; the role is chosen from a
menu. That is deliberate for a mock and is the first thing to change before
anyone else touches it.

**"Can I use it on my iPad?"** Not usefully yet. Every interaction is
mouse-driven — painting availability, dragging a date exception, and the whole
hover layer have no touch equivalent.

Answering these plainly is better than being caught by them. They are also the
questions whose answers should come from the customer rather than from us.
