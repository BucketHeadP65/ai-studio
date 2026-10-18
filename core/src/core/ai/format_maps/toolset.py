"""The format-maps wrapper toolset: map-then-slice on the wrapped tools' own verbs."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from hashlib import sha256
from typing import TYPE_CHECKING, Literal, TypedDict, Unpack

from pydantic_ai.exceptions import ModelRetry
from pydantic_ai.tools import AgentDepsT, RunContext
from pydantic_ai.toolsets import WrapperToolset
from pydantic_ai.toolsets.abstract import ToolsetTool
from pydantic_core import from_json, to_json

from core.ai.format_maps.domain import Outliner, default_outliners
from core.ai.format_maps.infrastructure.file_cache import FileContentCache
from core.observability import get_logger

if TYPE_CHECKING:
    from pydantic import JsonValue
    from pydantic_ai.toolsets.abstract import SchemaValidatorProt
    from pydantic_core import SchemaValidator

    from core.ai.format_maps.internal_contracts import ContentCache

_logger = get_logger("core.ai.format_maps")
"""Telemetry channel: one INFO record per `part=` cache miss, so misses are countable."""

PART_PARAM = "part"
"""Name of the optional parameter a mapped tool gains for selecting one part of its result."""

_PART_DESCRIPTION = (
    "Optional: return only this part of the result. A large result answers with a map of named parts. "
    "Pass a part token from that map. Omit it to get the full result or the map."
)

_HOW = "This result is large, so this is its map. Call this tool again with the same arguments plus part='<part>' to get one part."

_NO_PARTS = f"This result has no parts to select. Call again without `{PART_PARAM}`."

_NOT_CACHED = (
    f"No fetched content is available to take a part of. Call this tool again without `{PART_PARAM}` to fetch it fresh. "
    "The map comes back, and then you can select a part."
)

WhenFunc = Callable[[Mapping[str, object]], bool]
"""A gate over a call's arguments: `False` leaves that call unmapped."""


def command_starts(*prefixes: str, arg: str = "command") -> WhenFunc:
    """A `when` gate that matches a call whose `arg` argument starts with one of `prefixes`.

    Pair it with a named format, as in `MappedTool(format="diff", when=command_starts("git diff", "git show"))`, so the
    mount states the format and the gate only decides which calls it covers. A missing or non-string argument never
    matches.

    Args:
        *prefixes: The starts that match, compared after the argument's leading whitespace.
        arg: The argument holding the command.

    Returns:
        The gate.
    """

    def matches(args: Mapping[str, object]) -> bool:
        command = args.get(arg)
        return isinstance(command, str) and command.lstrip().startswith(prefixes)

    return matches


@dataclass(frozen=True)
class MappedTool:
    """One tool's mapping: its format name, and optionally its threshold, its refetch choice and its gate."""

    format: str
    """The name of the tool's format in the outliner registry."""

    over: int | None = None
    """The size, in characters of the result as text, from which a result answers with its map.

    `None` takes the toolset's default, and `0` always answers with the map.
    """

    refetch_on_miss: bool | None = None
    """Whether a `part=` call whose content is not cached calls the tool again. `None` takes the toolset's default."""

    when: WhenFunc | None = None
    """A gate over a call's arguments that decides which calls map, such as `command_starts(...)`. `None` maps every call."""


@dataclass(kw_only=True)
class _MappedToolsetTool(ToolsetTool[AgentDepsT]):
    """A wrapped tool carrying its original alongside, so `call_tool` can delegate faithfully."""

    original: ToolsetTool[AgentDepsT]


@dataclass(frozen=True)
class _Call[DepsT]:
    """One call of a mapped tool: its run, its name, its arguments without `part`, and the advertised tool."""

    ctx: RunContext[DepsT]
    name: str
    args: dict[str, object]
    tool: _MappedToolsetTool[DepsT]


class _ValidationOptions(TypedDict, total=False):
    """The keywords pydantic-ai passes a tool's validator beside the input and `allow_partial`."""

    context: object


class _PartAwareValidator:
    """Validates a call's arguments with the wrapped tool's own validator, then puts `part` back beside them."""

    def __init__(self, inner: SchemaValidator | SchemaValidatorProt) -> None:
        self._inner = inner

    def validate_json(
        self,
        input: str | bytes | bytearray,  # noqa: A002 (the `SchemaValidatorProt` parameter name)
        *,
        allow_partial: bool | Literal["off", "on", "trailing-strings"] = False,
        **options: Unpack[_ValidationOptions],
    ) -> object:
        parsed: JsonValue = from_json(input, allow_partial=allow_partial not in (False, "off"))
        return self.validate_python(parsed, allow_partial=allow_partial, **options)

    def validate_python(
        self,
        input: JsonValue,  # noqa: A002 (the `SchemaValidatorProt` parameter name)
        *,
        allow_partial: bool | Literal["off", "on", "trailing-strings"] = False,
        **options: Unpack[_ValidationOptions],
    ) -> object:
        if not isinstance(input, dict):
            return self._inner.validate_python(input, allow_partial=allow_partial, **options)
        args = dict(input)
        part = args.pop(PART_PARAM, None)
        if part is not None and not isinstance(part, str):
            msg = f"`{PART_PARAM}` must be a string part token from the result map."
            raise ModelRetry(msg)
        validated = self._inner.validate_python(args, allow_partial=allow_partial, **options)
        if part is None or not isinstance(validated, dict):
            return validated
        return {**validated, PART_PARAM: part}


@dataclass
class FormatMapsToolset(WrapperToolset[AgentDepsT]):
    """Wraps a toolset so its configured tools answer a large result with a map and slice one part on demand.

    Each configured tool gains an optional `part` argument. A call without `part` passes a small, non-text or unmapped
    result through whole, and a large result that maps answers with the map while its content goes to the `cache`. A
    call with `part` slices the cached content and never calls the wrapped tool, unless nothing is cached and
    `refetch_on_miss` allows a new call. Nothing cached otherwise answers with a retry telling the model to call again
    without `part`, and an unknown part answers with a retry naming the available parts.
    """

    tools: Mapping[str, MappedTool] = field(default_factory=dict[str, MappedTool])
    """The configured tools by name."""

    outliners: Mapping[str, Outliner] = field(default_factory=default_outliners)
    """The outliners by format name."""

    default_over: int = 8_000
    """The size, in characters, from which the result of a tool without its own `over` answers with a map."""

    cache: ContentCache = field(default_factory=FileContentCache)
    """Where mapped content waits for `part=` calls."""

    refetch_on_miss: bool = False
    """Whether a tool without its own choice is called again when a `part=` call finds nothing cached."""

    async def get_tools(self, ctx: RunContext[AgentDepsT]) -> dict[str, ToolsetTool[AgentDepsT]]:
        """The wrapped tools, with the schema of each configured tool extended by `part`.

        Args:
            ctx: The run's context.

        Returns:
            Every wrapped tool by name.
        """
        tools = await super().get_tools(ctx)
        out: dict[str, ToolsetTool[AgentDepsT]] = {}
        for name, tool in tools.items():
            if name not in self.tools:
                out[name] = tool
                continue
            out[name] = _MappedToolsetTool(
                toolset=tool.toolset,
                tool_def=replace(tool.tool_def, parameters_json_schema=_with_part(tool.tool_def.parameters_json_schema)),
                max_retries=tool.max_retries,
                args_validator=_PartAwareValidator(tool.args_validator),
                args_validator_func=_without_part(tool.args_validator_func),
                original=tool,
            )
        return out

    async def call_tool(
        self, name: str, tool_args: dict[str, object], ctx: RunContext[AgentDepsT], tool: ToolsetTool[AgentDepsT]
    ) -> object:
        """Answer a call with the wrapped tool's result, the map of that result, or one named part.

        Args:
            name: The called tool's name.
            tool_args: The validated arguments, with `part` when the model passed it.
            ctx: The run's context.
            tool: The tool as `get_tools` advertised it.

        Returns:
            The wrapped tool's result, the map of a large result, or the text of one part.

        Raises:
            ModelRetry: When `part` names nothing in the content, or nothing is cached and the tool may not be called again.
        """
        config = self.tools.get(name)
        if config is None or not isinstance(tool, _MappedToolsetTool):
            return await super().call_tool(name, tool_args, ctx, tool)
        part = tool_args.get(PART_PARAM)
        call = _Call(ctx=ctx, name=name, args={key: value for key, value in tool_args.items() if key != PART_PARAM}, tool=tool)
        gated_off = config.when is not None and not config.when(call.args)
        outliner = None if gated_off else self.outliners.get(config.format)
        if isinstance(part, str):
            return await self._serve_part(call, config, outliner, part)
        result = await self.wrapped.call_tool(name, call.args, ctx, tool.original)
        return await self._mapped(call, config, outliner, result)

    async def _mapped(self, call: _Call[AgentDepsT], config: MappedTool, outliner: Outliner | None, result: object) -> object:
        """`result` itself, or its map envelope when it is text large enough to map."""
        text = None if outliner is None else _as_text(result)
        over = config.over if config.over is not None else self.default_over
        if outliner is None or text is None or len(text) < over:
            return result
        mapping = outliner.map(text)
        if mapping is None:
            return result
        await self.cache.put(_cache_key(call), text)
        return {"format": config.format, "size_chars": len(text), "how": _HOW, **mapping}

    async def _serve_part(self, call: _Call[AgentDepsT], config: MappedTool, outliner: Outliner | None, part: str) -> str:
        """One named part, sliced from the cached content, never a fresh invocation by default."""
        if outliner is None:
            raise ModelRetry(_NO_PARTS)
        key = _cache_key(call)
        text = await self.cache.get(key)
        if text is None:
            refetch = config.refetch_on_miss if config.refetch_on_miss is not None else self.refetch_on_miss
            _logger.info("part miss: tool {tool!r}, run {run_id}, refetch={refetch}", tool=call.name, run_id=call.ctx.run_id, refetch=refetch)
            if not refetch:
                raise ModelRetry(_NOT_CACHED)
            result = await self.wrapped.call_tool(call.name, call.args, call.ctx, call.tool.original)
            text = _as_text(result)
            if text is None:
                raise ModelRetry(_NO_PARTS)
            await self.cache.put(key, text)
        return self._slice(outliner, text, part)

    def _slice(self, outliner: Outliner, text: str, part: str) -> str:
        """One named part of `text`, or a `ModelRetry` teaching the recovery."""
        mapping = outliner.map(text)
        if mapping is None:
            raise ModelRetry(_NO_PARTS)
        section = outliner.slice(text, part)
        if section is None:
            available = ", ".join(repr(stub["part"]) for stub in mapping["parts"][:20])
            msg = f"No part {part!r} in this result. Available parts: {available}."
            raise ModelRetry(msg)
        return section


def _with_part(schema: dict[str, JsonValue]) -> dict[str, JsonValue]:
    """`schema` with the optional `part` string property added, everything else untouched."""
    existing = schema.get("properties")
    properties: dict[str, JsonValue] = dict(existing) if isinstance(existing, dict) else {}
    properties[PART_PARAM] = {"type": "string", "description": _PART_DESCRIPTION}
    return {**schema, "properties": properties}


def _without_part(func: Callable[..., object] | None) -> Callable[..., object] | None:
    """A custom args validator shielded from `part`, which the original signature never had."""
    if func is None:
        return None

    def validate(ctx: RunContext[AgentDepsT], **kwargs: object) -> object:
        return func(ctx, **{key: value for key, value in kwargs.items() if key != PART_PARAM})

    return validate


def _cache_key[DepsT](call: _Call[DepsT]) -> str:
    """A key from the run, the tool and its arguments without `part`, so the same call in the same run finds its content."""
    digest = sha256(to_json({key: call.args[key] for key in sorted(call.args)})).hexdigest()[:16]
    return f"{call.ctx.run_id or 'run'}/{call.name}.{digest}"


def _as_text(result: object) -> str | None:
    """The result as text to slice: a string itself, a list, tuple or dict as JSON, anything else `None`."""
    if isinstance(result, str):
        return result
    if isinstance(result, (list, tuple, dict)):
        try:
            return to_json(result).decode("utf-8")
        except ValueError:  # pragma: no cover (the framework rejects unserializable returns before this)
            return None
    return None
