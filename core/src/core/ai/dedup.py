"""A capability that never hands the agent the same tool result twice in one run.

Use it as `DedupToolResults(tools=["search", "fetch"])`. Each run keeps one set of everything the
selected tools returned. Items already shown are dropped from a list result. A single result equal to
one already shown answers with a short note instead of the body. A listing whose every item was
already shown answers with the note too. State never crosses runs.

A repeat is suppressed only while the tool return that first showed it is still in the run's message
history. When that return is gone, the content passes through whole and is recorded under the new call.

The `identity` strategy decides what "the same" means:

* `None` (the default): the content itself. Two results are the same when their canonical bytes are
  the same, so an item that reappears with any difference stays.
* a field name (`"id"`, `"ref"`): the item's own identifier. A reappearance is a repeat even when
  other fields changed. Items without the field fall back to content identity.
* a callable `(item) -> str | None`: a key of your own. `None` falls back to content identity.

`marker` decides what a repeated list item becomes: dropped when it is `None` (the default), or the
stand-in the callable builds from it. `note` is what a repeated single result answers with. A `{key}`
placeholder in it receives the matched identity. Word it in the agent's language: what happened, and
that the earlier copy is the one to reuse.

`tools` is a pydantic-ai `ToolSelector` (`'all'`, tool names, a metadata match, or a predicate). Leave
out tools whose results a code-mode sandbox consumes as values. A program that indexes into an emptied
list or reads fields off a note string fails.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.messages import ToolReturnPart
from pydantic_ai.tools import AgentDepsT, ToolSelector, matches_tool_selector
from pydantic_ai.toolsets import WrapperToolset
from pydantic_core import to_json

if TYPE_CHECKING:
    from collections.abc import Callable

    from pydantic_ai.tools import RunContext
    from pydantic_ai.toolsets import AbstractToolset, ToolsetTool

DEFAULT_NOTE = "Already shown this run. Reuse it from the earlier result above."
"""What a repeated result answers with when the capability is given no `note`."""

Identity = str | None
"""A matched identity: the extracted key when a strategy produced one, else `None` (content match)."""


def _fingerprint(item: object) -> bytes | None:
    """The item's canonical bytes, or `None` when it cannot be serialized."""
    try:
        return to_json(item, serialize_unknown=True)
    except ValueError:
        return None


def _visible_returns(ctx: RunContext[AgentDepsT]) -> frozenset[str]:
    """The tool-call ids whose returns are still present in the run's messages."""
    return frozenset(
        part.tool_call_id for message in ctx.messages for part in message.parts if isinstance(part, ToolReturnPart)
    )


@dataclass
class DedupToolResults(AbstractCapability[AgentDepsT]):
    """Filter already-shown results out of the selected tools, per run."""

    tools: ToolSelector[AgentDepsT] = "all"
    """Which tools to deduplicate: `'all'`, tool names, a metadata match, or a predicate."""
    identity: str | Callable[[object], str | None] | None = None
    """When two results are the same: by content (`None`, the default), by the item's own field
    (`"id"`, `"ref"`), or by a key function. Field and function strategies fall back to content
    for items they cannot key."""
    marker: Callable[[object], object] | None = None
    """What a repeated list item becomes: dropped (`None`, the default), or the stand-in this
    builds from it. The stand-in should have the same shape as a real item."""
    note: str = DEFAULT_NOTE
    """What a repeated single result, or a listing that repeats entirely, answers with.

    `{key}` receives the matched identity, or `"every result"` for a whole listing."""

    @classmethod
    def get_serialization_name(cls) -> str | None:
        """The stable name this capability serializes under.

        Returns:
            `"DedupToolResults"`.
        """
        return "DedupToolResults"

    def get_wrapper_toolset(self, toolset: AbstractToolset[AgentDepsT]) -> AbstractToolset[AgentDepsT]:
        """A fresh dedup wrapper over this run's toolset, with its own empty state.

        Args:
            toolset: The run's toolset to wrap.

        Returns:
            The wrapping toolset.
        """
        return _Deduped(wrapped=toolset, selector=self.tools, identity=self.identity, marker=self.marker, note=self.note)


@dataclass
class _Deduped(WrapperToolset[AgentDepsT]):
    """The run-scoped wrapper: one `shown` set, results differenced on the way out."""

    selector: ToolSelector[AgentDepsT] = "all"
    identity: str | Callable[[object], str | None] | None = None
    marker: Callable[[object], object] | None = None
    note: str = DEFAULT_NOTE
    shown: dict[object, str | None] = field(default_factory=dict)
    """Each shown member mapped to the `tool_call_id` whose return carried it."""

    async def call_tool(self, name: str, tool_args: dict[str, object], ctx: RunContext[AgentDepsT], tool: ToolsetTool[AgentDepsT]) -> object:
        """Delegate, then difference the result against everything this run has been shown."""
        result = await super().call_tool(name, tool_args, ctx, tool)
        if not await matches_tool_selector(self.selector, ctx, tool.tool_def):
            return result
        visible = _visible_returns(ctx)
        if isinstance(result, list | tuple):
            fresh: list[object] = []
            for item in result:
                new, _ = self._fresh(item, ctx, visible)
                if new:
                    fresh.append(item)
                elif self.marker is not None:
                    fresh.append(self.marker(item))
            if not fresh and result:
                # Every item was already shown: answer with the note, not an empty list.
                return self.note.replace("{key}", "every result")
            return tuple(fresh) if isinstance(result, tuple) else fresh
        new, key = self._fresh(result, ctx, visible)
        if new:
            return result
        if self.marker is not None:
            return self.marker(result)
        return self.note.replace("{key}", key or "the result")

    def _fresh(self, item: object, ctx: RunContext[AgentDepsT], visible: frozenset[str]) -> tuple[bool, Identity]:
        """Whether `item` is new to the history the model can still see, recording it, and its identity."""
        key = self._key(item)
        member: object | None = f"key:{key}" if key is not None else _fingerprint(item)
        if member is None:
            return True, None
        carrier = self.shown.get(member, "")
        if carrier != "" and carrier in visible:
            return False, key
        self.shown[member] = ctx.tool_call_id
        return True, key

    def _key(self, item: object) -> str | None:
        """The identity strategy's key for `item`, or `None` to judge by content."""
        if callable(self.identity):
            extracted = self.identity(item)
            return extracted if isinstance(extracted, str) and extracted else None
        if isinstance(self.identity, str):
            value = item.get(self.identity) if isinstance(item, dict) else getattr(item, self.identity, None)
            return value if isinstance(value, str) and value else None
        return None
