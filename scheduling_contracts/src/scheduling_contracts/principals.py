"""Who is asking, and on whose behalf.

The rule the whole design rests on: **a command never carries an identity.**
Identity arrives here, established at the session edge, and is attached by the
gate. A `client_id` in a request body is a claim; a `Principal` is a fact.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .values import ClientId, Role


@dataclass(frozen=True)
class Principal:
    """An authenticated actor.

    `client_id` is set exactly when `role is Role.CLIENT`; `__post_init__`
    refuses any other combination, because a client principal with no id would
    silently pass every ownership check it was compared against.
    """
    role: Role
    client_id: Optional[ClientId] = None

    def __post_init__(self) -> None:
        if self.role is Role.CLIENT and not self.client_id:
            raise ValueError("a client principal must carry a client_id")
        if self.role is not Role.CLIENT and self.client_id is not None:
            raise ValueError(f"{self.role.value} principals have no client_id")

    def owns(self, owner: Optional[ClientId]) -> bool:
        return owner is not None and owner == self.client_id


@dataclass(frozen=True)
class Session:
    """A principal plus how they are reaching us.

    `via_assistant` is the whole of the delegation model at this layer. An
    assistant is not a role — it is an ordinary principal acting through a
    restricted channel, so it can never hold a capability its human does not.
    Attenuation happens in the policy, not here.
    """
    principal: Principal
    via_assistant: bool = False
    # Correlates the audit trail with a conversation, so "why did this happen?"
    # is answerable after the fact.
    conversation_id: Optional[str] = None

    @property
    def delegated(self) -> bool:
        return self.via_assistant
