"""`FormatMaps`: map-then-slice reading for known-format tool returns, on the tools' own verbs."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.tools import AgentDepsT

from core.ai.format_maps.domain import Outliner, default_outliners
from core.ai.format_maps.infrastructure.file_cache import FileContentCache
from core.ai.format_maps.toolset import FormatMapsToolset, MappedTool

if TYPE_CHECKING:
    from collections.abc import Mapping

    from pydantic_ai.toolsets import AbstractToolset

    from core.ai.format_maps.internal_contracts import ContentCache


@dataclass
class FormatMaps(AbstractCapability[AgentDepsT]):
    """Gives the large text returns of configured tools a map, and those tools a `part` argument that slices one part.

    Formats are declared per tool, never sniffed from content, and a format name the registry does not know fails at
    construction. A mapped return's content goes to the `cache` under a run-scoped key, so every `part=` call slices the
    same snapshot and never calls the tool again. Nothing cached answers with a retry telling the model to call again
    without `part`, unless `refetch_on_miss` allows a new call. Pair it with `ToolOutputLimits`, which reduces whatever
    this capability passes through, maps included.

    Example:
        ```python
        from pydantic_ai import Agent
        from core.ai.format_maps.contracts import FormatMaps

        agent = Agent("openai:gpt-4o", capabilities=[FormatMaps(tools={"read_docs": "markdown"})])
        ```
    """

    tools: Mapping[str, str | MappedTool] = field(default_factory=dict[str, "str | MappedTool"])
    """Tool names to map, each with its format name or full `MappedTool` config."""

    over: int = 8_000
    """Default size threshold (characters) above which a mapped tool's return answers with its map.

    `0` means always answer with the map. A `MappedTool.over` overrides this per tool.
    """

    outliners: Mapping[str, Outliner] | None = None
    """Overrides and extensions for the stock outliner registry (`markdown`, `csv`, `listing`, `diff`, `json`)."""

    cache: ContentCache | None = None
    """Where mapped content waits for `part=` calls. Defaults to a `FileContentCache`."""

    refetch_on_miss: bool = False
    """Whether a `part=` call calls the tool again when its content is not cached.

    Off, nothing cached answers with a retry telling the model to call again without `part`. A
    `MappedTool.refetch_on_miss` overrides it per tool. Turn it on only for deterministic reads, because a new call makes
    a new snapshot that part tokens such as `item 3` then resolve against.
    """

    def __post_init__(self) -> None:
        registry = {**default_outliners(), **(self.outliners or {})}
        for name, config in self.tools.items():
            format_name = config.format if isinstance(config, MappedTool) else config
            if format_name not in registry:
                known = ", ".join(sorted(registry))
                msg = f"{name}: unknown format {format_name!r}. Known formats: {known}."
                raise ValueError(msg)

    def get_wrapper_toolset(self, toolset: AbstractToolset[AgentDepsT]) -> AbstractToolset[AgentDepsT] | None:
        """Wrap the agent's assembled toolset in the format-maps toolset.

        Args:
            toolset: The agent's assembled toolset.

        Returns:
            The wrapping toolset.
        """
        registry = {**default_outliners(), **(self.outliners or {})}
        normalized = {name: config if isinstance(config, MappedTool) else MappedTool(format=config) for name, config in self.tools.items()}
        return FormatMapsToolset(
            wrapped=toolset,
            tools=normalized,
            outliners=registry,
            default_over=self.over,
            cache=self.cache if self.cache is not None else FileContentCache(),
            refetch_on_miss=self.refetch_on_miss,
        )
