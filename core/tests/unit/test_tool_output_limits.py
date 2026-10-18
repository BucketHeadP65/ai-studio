from pathlib import Path

from pydantic_ai import RunContext
from pydantic_ai.messages import ToolCallPart, ToolReturn
from pydantic_ai.models.test import TestModel
from pydantic_ai.tools import ToolDefinition
from pydantic_ai.usage import RunUsage
from pydantic_ai_harness.tool_output_limits import Band, LocalFileStore, Spill, Truncate

from core.ai.tool_output_limits import ToolOutputLimits


async def _reduce(limits: ToolOutputLimits[None], result: object) -> object:
    """What `limits` hands back for `result`, returned by a tool named `load`."""
    return await limits.after_tool_execute(
        RunContext[None](deps=None, model=TestModel(), usage=RunUsage(), run_id="run-1"),
        call=ToolCallPart(tool_name="load", args="{}", tool_call_id="call-1"),
        tool_def=ToolDefinition(name="load"),
        args={},
        result=result,
    )


async def test_a_spilled_return_keeps_the_tools_it_revealed(tmp_path: Path) -> None:
    limits = ToolOutputLimits[None](bands=[Band(over=5, action=Spill())], store=LocalFileStore(base_dir=tmp_path))

    reduced = await _reduce(limits, ToolReturn(return_value="a" * 100, tools=["some_tool", "another_tool"]))

    assert isinstance(reduced, ToolReturn)
    assert reduced.tools == ["some_tool", "another_tool"]


async def test_a_truncated_return_keeps_the_tools_it_revealed() -> None:
    limits = ToolOutputLimits[None](bands=[Band(over=10, action=Truncate(max_chars=20))])

    reduced = await _reduce(limits, ToolReturn(return_value="a" * 100, content="note", tools=["x"]))

    assert isinstance(reduced, ToolReturn)
    assert reduced.tools == ["x"]
    assert reduced.content == "note"


async def test_a_return_under_the_limits_comes_back_as_it_was() -> None:
    limits = ToolOutputLimits[None](bands=[Band(over=1_000, action=Truncate(max_chars=1_000))])
    original = ToolReturn(return_value="short", tools=["x"])

    assert await _reduce(limits, original) is original


async def test_a_reduced_return_without_tools_stays_without_them() -> None:
    limits = ToolOutputLimits[None](bands=[Band(over=10, action=Truncate(max_chars=20))])

    reduced = await _reduce(limits, ToolReturn(return_value="a" * 100))

    assert isinstance(reduced, ToolReturn)
    assert not reduced.tools
