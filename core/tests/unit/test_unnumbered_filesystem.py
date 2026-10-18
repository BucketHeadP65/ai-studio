from collections.abc import Sequence
from pathlib import Path

from pydantic_ai import Agent
from pydantic_ai.capabilities import LocalWorkspace
from pydantic_ai.messages import ModelMessage, ModelRequest, ModelResponse, TextPart, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai_harness.tool_output_limits import Band, LocalFileStore, Spill, ToolOutputLimits

from core.ai.filesystem import UnnumberedFileSystem


async def _read(root: Path, path: str) -> str:
    """What `read_file` returned for `path`, read by an agent over `root`."""
    returned: list[str] = []

    def model(messages: Sequence[ModelMessage], info: AgentInfo) -> ModelResponse:
        del info
        for message in messages:
            if isinstance(message, ModelRequest):
                returned.extend(str(part.content) for part in message.parts if isinstance(part, ToolReturnPart))
        if returned:
            return ModelResponse(parts=[TextPart("done")])
        return ModelResponse(parts=[ToolCallPart(tool_name="read_file", args={"path": path})])

    agent = Agent(FunctionModel(model), capabilities=[LocalWorkspace(root), UnnumberedFileSystem(root_dir=root)])
    await agent.run("read it")
    return returned[0]


async def test_read_file_returns_each_line_as_it_is_in_the_file(tmp_path: Path) -> None:
    (tmp_path / "notes.md").write_text("# Title\n\n12\tstarts with a number and a tab\nlast line\n")

    header, _, body = (await _read(tmp_path, "notes.md")).partition("\n")

    assert header.startswith("[notes.md")
    assert body == "# Title\n\n12\tstarts with a number and a tab\nlast line\n"


async def test_a_read_that_answers_with_a_message_passes_through(tmp_path: Path) -> None:
    (tmp_path / "empty.md").write_text("")

    returned = await _read(tmp_path, "empty.md")

    assert "(empty file)" in returned


async def test_a_file_with_carriage_return_line_ends_reads_as_it_is(tmp_path: Path) -> None:
    (tmp_path / "old.txt").write_bytes(b"alpha\rbeta\rgamma\r\n")

    _, _, body = (await _read(tmp_path, "old.txt")).partition("\n")

    assert body == "alpha\rbeta\rgamma\r\n"


async def test_a_file_with_a_form_feed_reads_as_it_is(tmp_path: Path) -> None:
    (tmp_path / "pages.py").write_bytes(b"def a():\n    pass\n\x0c\ndef b():\n    pass\n")

    _, _, body = (await _read(tmp_path, "pages.py")).partition("\n")

    assert body == "def a():\n    pass\n\x0c\ndef b():\n    pass\n"


async def test_read_file_is_not_described_as_numbered(tmp_path: Path) -> None:
    descriptions: list[str] = []

    def model(messages: Sequence[ModelMessage], info: AgentInfo) -> ModelResponse:
        del messages
        descriptions.extend(tool.description or "" for tool in info.function_tools if tool.name == "read_file")
        return ModelResponse(parts=[TextPart("done")])

    await Agent(FunctionModel(model), capabilities=[LocalWorkspace(tmp_path), UnnumberedFileSystem(root_dir=tmp_path)]).run("hi")

    assert descriptions
    assert "line number" not in descriptions[0]


async def test_a_large_file_is_read_whole_without_line_numbers(tmp_path: Path) -> None:
    text = "".join(f"line {number} " + "x" * 40 + "\n" for number in range(1, 1200))
    assert len(text) > 60_000
    (tmp_path / "big.txt").write_text(text)

    header, _, body = (await _read(tmp_path, "big.txt")).partition("\n")

    assert header.startswith("[big.txt")
    assert body == text


async def test_a_spilled_read_is_unnumbered_in_the_preview_and_in_the_store(tmp_path: Path) -> None:
    text = "".join(f"line {number} " + "x" * 40 + "\n" for number in range(1, 400))
    (tmp_path / "big.txt").write_text(text)
    returned: list[str] = []

    def model(messages: Sequence[ModelMessage], info: AgentInfo) -> ModelResponse:
        del info
        for message in messages:
            if isinstance(message, ModelRequest):
                returned.extend(str(part.content) for part in message.parts if isinstance(part, ToolReturnPart))
        if returned:
            return ModelResponse(parts=[TextPart("done")])
        return ModelResponse(parts=[ToolCallPart(tool_name="read_file", args={"path": "big.txt"})])

    store = tmp_path / "spill"
    limits = ToolOutputLimits(bands=[Band(over=2_000, action=Spill(preview_chars=500))], store=LocalFileStore(store))
    await Agent(FunctionModel(model), capabilities=[LocalWorkspace(tmp_path), UnnumberedFileSystem(root_dir=tmp_path), limits]).run(
        "read it"
    )

    spilled = [path.read_text() for path in store.rglob("*") if path.is_file()]
    assert len(spilled) == 1
    assert "line 1 xxxx" in returned[0]
    assert "     1\tline 1" not in returned[0]
    assert spilled[0].partition("\n")[2] == text
