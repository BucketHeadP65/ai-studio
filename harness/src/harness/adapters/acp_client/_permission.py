"""How a permission request from an external agent is decided, and how the answer is spelled on the wire."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from acp import schema

if TYPE_CHECKING:
    from collections.abc import Sequence

# The option kinds each verdict prefers, in order.
_ALLOW_KINDS: tuple[schema.PermissionOptionKind, ...] = ("allow_once", "allow_always")
_REJECT_KINDS: tuple[schema.PermissionOptionKind, ...] = ("reject_once", "reject_always")


class PermissionPolicy(Protocol):
    """Decides one permission request from the external agent."""

    async def decide(self, request: schema.RequestPermissionRequest) -> bool:
        """`True` lets the call through, `False` refuses it."""
        ...


@dataclass(frozen=True)
class AllowOnce:
    """Allow every request once each, without asking, for an agent already trusted with its working directory."""

    async def decide(self, request: schema.RequestPermissionRequest) -> bool:
        """Allow."""
        del request
        return True


def outcome_for(options: Sequence[schema.PermissionOption], *, allowed: bool) -> schema.AllowedOutcome | schema.DeniedOutcome:
    """The wire answer for `allowed` from the agent's options, or a cancelled turn for a refusal with no reject option."""
    for kind in _ALLOW_KINDS if allowed else _REJECT_KINDS:
        for option in options:
            if option.kind == kind:
                return schema.AllowedOutcome(outcome="selected", option_id=option.option_id)
    if allowed and options:
        return schema.AllowedOutcome(outcome="selected", option_id=options[0].option_id)
    return schema.DeniedOutcome(outcome="cancelled")
