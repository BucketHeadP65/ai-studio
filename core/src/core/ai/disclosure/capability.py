"""The pydantic-ai capability that teaches leveled reads and caps what envelopes may spend in one run.

`ProgressiveDisclosure` ships no content of its own. It adds three things to any agent that holds a provider's read tools.
Its instructions teach the model to read a level's `summary` and `children` before going deeper.
Its budget meters every conformant result with `approx_tokens`. The result whose cost pushes the run past
`max_envelope_tokens` is itself replaced by a budget stub in `search_only` mode, so an envelope larger than the whole
budget is stubbed on its first call.
A result is conformant when it is a bare `DisclosureEnvelope` or a pydantic-ai `ToolReturn` whose `return_value` is one.
Anything else passes through untouched and uncounted, whichever tool produced it.
Every call through the capability is one span, and a conformant result adds the envelope's `ref`, `kind`, `mode` and
whether the budget was reached.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from pydantic_ai import ToolReturn
from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.tools import AgentDepsT
from pydantic_ai.toolsets import WrapperToolset

from core import observability

from .domain import DisclosureEnvelope, approx_tokens

if TYPE_CHECKING:
    from collections.abc import Callable

    from pydantic_ai import RunContext
    from pydantic_ai.toolsets import AbstractToolset, ToolsetTool

_PROTOCOL_TEXT = (
    "## Leveled reads\n"
    "Some read results are leveled: the item's own `summary` and `content` plus `children`, a "
    "one-line summary of each thing directly inside it, with the ref to read next. Read the "
    "summaries first and open only the children whose summary is not enough. Every level costs "
    "tokens."
)
"""The whole prompt contribution, identical for every `ProgressiveDisclosure` instance.

It never announces the budget. The read that spends the budget returns a stub that names it."""


@dataclass
class ProgressiveDisclosure(AbstractCapability[AgentDepsT]):
    """Teaches leveled reads and caps the tokens conformant `DisclosureEnvelope` results may spend in one run."""

    max_envelope_tokens: int = 20_000
    """The per-run cap on cumulative `approx_tokens` spent on `DisclosureEnvelope` results.
    Never announced in the prompt: the read that spends it returns the budget stub, and that
    stub is where the number first reaches the model."""

    def get_instructions(self) -> Callable[[RunContext[AgentDepsT]], str]:
        """Give the agent the leveled-reads protocol text.

        The text renders with the run-time instructions, never in the literal prefix ahead of the agent's identity.

        Returns:
            A function of the run context that returns the protocol text.
        """

        def compose(_ctx: RunContext[AgentDepsT]) -> str:
            return _PROTOCOL_TEXT

        return compose

    def get_wrapper_toolset(self, toolset: AbstractToolset[AgentDepsT]) -> AbstractToolset[AgentDepsT] | None:
        """Wrap the agent's assembled toolset in a budget wrapper for one run.

        Each call builds a fresh record of spent tokens, so no run spends another run's budget.
        The toolset copies pydantic-ai makes within a run share that one record.

        Args:
            toolset: The agent's assembled toolset.

        Returns:
            The toolset under this run's budget.
        """
        return _EnvelopeBudget(wrapped=toolset, max_envelope_tokens=self.max_envelope_tokens, spent=[])


@dataclass
class _EnvelopeBudget(WrapperToolset[AgentDepsT]):
    """Meters `DisclosureEnvelope` results for one run and stubs the one that spends the budget."""

    max_envelope_tokens: int
    spent: list[int]

    async def call_tool(
        self, name: str, tool_args: dict[str, object], ctx: RunContext[AgentDepsT], tool: ToolsetTool[AgentDepsT]
    ) -> object:
        """Call the tool under a span, record a conformant result's tokens, and stub it when they cross the budget."""
        with observability.span("disclosure tool call", tool=name) as active:
            result = await super().call_tool(name, tool_args, ctx, tool)
            envelope = _unwrap_envelope(result)
            if envelope is None:
                return result
            self.spent.append(approx_tokens(envelope))
            over_budget = sum(self.spent) > self.max_envelope_tokens
            active.set_attribute("disclosure.ref", envelope.ref)
            active.set_attribute("disclosure.kind", envelope.kind)
            active.set_attribute("disclosure.mode", envelope.mode)
            active.set_attribute("disclosure.budget_reached", over_budget)
            if not over_budget:
                return result
            stub = _budget_stub(name, tool_args, self.max_envelope_tokens)
            return ToolReturn(return_value=stub) if isinstance(result, ToolReturn) else stub


def _unwrap_envelope(result: object) -> DisclosureEnvelope | None:
    """The `DisclosureEnvelope` a conformant result carries, bare or `ToolReturn`-wrapped, else `None`."""
    if isinstance(result, DisclosureEnvelope):
        return result
    if isinstance(result, ToolReturn) and isinstance(result.return_value, DisclosureEnvelope):
        return result.return_value
    return None


def _budget_stub(name: str, tool_args: dict[str, object], max_envelope_tokens: int) -> DisclosureEnvelope:
    """The stub a call returns once metering it spends the run's disclosure budget."""
    ref = tool_args.get("ref")
    return DisclosureEnvelope(
        ref=ref if isinstance(ref, str) else name,
        kind="budget",
        title="Reading budget reached",
        summary=f"Reading budget of {max_envelope_tokens} tokens spent. Narrow with search, then read only what you need.",
        mode="search_only",
    )
