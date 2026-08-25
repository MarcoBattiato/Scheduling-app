# scheduling_contracts

The command surface, the permission model, and the ports every other package is
written against. Declarations only — nothing here executes anything.

**It must never acquire a dependency.** This is the fixed point the others
conform to, so an import here is inherited by all of them.
`tests/test_isolation.py` enforces a whitelist of five standard-library modules,
deliberately stricter than "is it on PyPI", because the mistake being guarded
against is a well-meaning `from calendar_store import ...`.

**A port is declared by the consumer that needs it, not by the package that
satisfies it.** The domain says "I need a window of availability"; an adapter
maps `calendar_store` onto that, and the store never learns the domain exists.
Get that arrow backwards and the coupling has been renamed rather than removed.

## The rule the whole thing rests on

A command carries *what* should happen and never *who is asking*. There is no
actor field anywhere in `commands.py`, and `test_no_command_carries_an_actor_field`
keeps it that way. Where `client_id` appears it names the **subject**, and the
gate still checks it against the authenticated principal.

The same reasoning removed `by_provider` from `appointment.cancel`: who cancelled
is evidence that anchoring will read, so it is derived from the session rather
than chosen by the caller.

## Metadata is data, not `if` statements

Each command registers its tier and what it points at, so `permissions.py` is a
table rather than a pile of branches. That is what makes the model checkable:

- **Totality** — every role has an explicit entry for every command. No
  fallback, because a fallback is where an accidental grant hides.
- **Attenuation** — an assistant's answer is computed *after* its delegator's,
  so delegation can only narrow. `test_delegation_never_widens_a_permission`
  proves it across every (role, command) pair rather than by inspection.

Adding a command without a permission entry fails a test. That is the intended
friction.

## Ownership is resolved outside the policy

`decide()` is pure and takes an already-resolved `Target`. Finding out who owns
appointment 41 is a repository lookup, and doing it in the gate is what keeps the
policy exhaustively testable. An ownership failure returns
`Deny(disguise_as_missing=True)`: the caller is told there is no such thing,
because "forbidden" on someone else's record confirms it exists.

## Three things that look like mistakes and are not

- **Three `(start, end)` types** — `calendar_store.TimeSegment`,
  `scheduling_engine.TimeSegment`, and `TimeSpan` here. None import each other.
  That is what lets three packages agree on a shape without agreeing on a
  dependency; anything that only reads a span should accept any object with
  `.start`/`.end`.
- **`approval.respond` is denied to the provider.** Everywhere else the provider
  outranks the client. Answering for someone would forge the consent the whole
  negotiation exists to obtain.
- **`optimiser.set_*` is forbidden to assistants rather than drafted.** Its
  effects are practice-wide and diffuse: a mis-set horizon does not look wrong
  in a confirmation dialog the way a cancelled appointment does, so confirmation
  buys nothing.

## Not here on purpose

Payments and documents are named in `docs/CONTROL_SURFACE.md` but absent from
`commands.py`. Their payloads are not designed, and speculative shapes are
expensive in the one place everything else conforms to.

Run tests: `python -m pytest tests/ -q` (needs only pytest; the package itself
installs with nothing).
