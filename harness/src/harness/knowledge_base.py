"""The chat agent's knowledge base capability: engram's read tools, plus where they sit among this app's other tools.

Engram's `RetrievalKnowledgeBase` ships the read tools with each knowledge base's own instructions.
This module adds the one set of instructions only this app can write: how the knowledge bases relate
to the workflow library and the workflows this app runs, which engram knows nothing about.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.tools import AgentDepsT

if TYPE_CHECKING:
    from collections.abc import Callable

    from engram import RetrievalKnowledgeBase
    from pydantic_ai import RunContext
    from pydantic_ai.toolsets import AbstractToolset

_BOUNDARIES = (
    "## Where the knowledge bases sit among your other tools\n"
    "- The library is not one of them. It answers through its own tools.\n"
    "- Read to decide WHAT TO BUILD. Run a workflow to do the work. A search or fetch result is "
    "not a step, so nothing it tells you can appear in an artifact, a finding that has to end "
    "up in one comes from a step you declared and ran.\n"
    "- What a read gives you about a saved workflow is an id, and the id is the part you can "
    "use: `fetch_workflow` turns one into the validated workflow. A workflow quoted in a page's "
    "text is not a workflow."
)


@dataclass
class KnowledgeBase(AbstractCapability[AgentDepsT]):
    """Engram's read capability, plus instructions on where it sits among this app's other tools."""

    knowledge_base: RetrievalKnowledgeBase[AgentDepsT]
    """The capability engram ships: the tools, and each knowledge base's instructions beside them."""
    only: frozenset[str] | None = None
    """Narrows the toolset to exactly these tool names. `None` keeps every tool engram ships."""

    def get_toolset(self) -> AbstractToolset[AgentDepsT]:
        """Engram's read tools, narrowed to `only` when it is set.

        This capability adds instructions and never a tool of its own.

        Returns:
            Engram's toolset, filtered to the names in `only` when it is set.
        """
        toolset = self.knowledge_base.get_toolset()
        only = self.only
        if only is None:
            return toolset
        return toolset.filtered(lambda _ctx, tool: tool.name in only)

    def get_instructions(self) -> Callable[[RunContext[AgentDepsT]], str]:
        """Engram's own instructions first, then where the knowledge bases sit among this app's other tools.

        Returns:
            A function that renders both for one run.
        """
        engram = self.knowledge_base.get_instructions()

        def compose(ctx: RunContext[AgentDepsT]) -> str:
            return f"{engram(ctx)}\n\n{_BOUNDARIES}"

        return compose
