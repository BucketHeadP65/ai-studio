"""`RuntimeVoiced`, a wrapper that renders a capability's literal instructions after the agent's identity.

pydantic-ai renders literal instruction strings ahead of every function-shaped block, the identity included.
`RuntimeVoiced` turns the literal into a function returning the same words, so they render after the identity.
Wrap only a capability whose instructions are a literal string. When they are not, the wrapper raises
`TypeError` as the agent is built.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from pydantic_ai.capabilities import WrapperCapability
from pydantic_ai.tools import AgentDepsT

if TYPE_CHECKING:
    from collections.abc import Callable

    from pydantic_ai.tools import RunContext


@dataclass
class RuntimeVoiced(WrapperCapability[AgentDepsT]):
    """A capability whose instruction block renders with the run-time voices, never before them."""

    def get_instructions(self) -> Callable[[RunContext[AgentDepsT]], str] | None:
        """The wrapped capability's instructions as a function of the run context.

        Returns:
            A function returning the wrapped literal, or None when the wrapped capability has no instructions.

        Raises:
            TypeError: When the wrapped capability's instructions are not a literal string.
        """
        inner = self.wrapped.get_instructions()
        if inner is None:
            return None
        if not isinstance(inner, str):
            msg = f"{type(self.wrapped).__name__} no longer speaks in a literal. Drop its RuntimeVoiced wrapper."
            raise TypeError(msg)

        def compose(_ctx: RunContext[AgentDepsT]) -> str:
            return inner

        return compose
