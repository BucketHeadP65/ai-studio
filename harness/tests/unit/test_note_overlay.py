"""NoteOverlay: a `read_file` return gains the owner-notes fence, for an annotated markdown file only."""

from __future__ import annotations

import re

from typing import TYPE_CHECKING, cast

import pytest

from core.ai.filesystem import UnnumberedFileSystem
from core.ai.workflows.contracts import StepRegistry
from engram.kb.contracts import NoteAnchor, NoteSheet, add_reply, set_status
from pydantic_ai import Agent
from pydantic_ai.capabilities import LocalWorkspace
from pydantic_ai.exceptions import ModelRetry
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.models.test import TestModel
from pydantic_ai_harness.tool_output_limits import Band, LocalFileStore, Spill, ToolOutputLimits

from harness.features.projects.domain import add_note, replace_note
from harness.features.projects.infrastructure.note_sidecars import load_sheet, save_sheet
from harness.features.projects.note_overlay import NoteOverlay, NoteReplyToolset
from harness.session import NoSessionLog, Runtime
from harness.workspace import open_workspace

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path
    from typing import Any

    from pydantic_ai.capabilities import AbstractCapability
    from pydantic_ai.messages import ModelMessage
    from pydantic_ai.models.function import AgentInfo

from tests.support.state import a_throwaway_state


def _reads_then_echoes_the_tool_return(path: str) -> FunctionModel:
    """A model that calls `read_file(path)` once, then answers with that call's return verbatim."""

    def function(messages: Sequence[ModelMessage], info: AgentInfo) -> ModelResponse:
        del info
        for message in reversed(messages):
            for part in message.parts:
                if isinstance(part, ToolReturnPart):
                    return ModelResponse(parts=[TextPart(str(part.content))])
        return ModelResponse(parts=[ToolCallPart("read_file", {"path": path})])

    return FunctionModel(function)


def _agent(model: FunctionModel, project_root: Path, *extra: AbstractCapability[Runtime]) -> Agent[Runtime, str]:
    """The workspace, the files plugin and NoteOverlay over the SAME root -- the mount's own geometry, as in the app."""
    return Agent(
        model,
        deps_type=Runtime,
        output_type=str,
        capabilities=[LocalWorkspace(project_root), UnnumberedFileSystem[Runtime](root_dir=project_root), NoteOverlay(root=project_root), *extra],
    )


def _runtime(project_root: Path) -> Runtime:
    workspace = open_workspace(project_root)
    return Runtime(registry=StepRegistry(), library=cast("Any", None), agent=cast("Any", None), workspace=workspace, session_log=NoSessionLog(), state=a_throwaway_state())


def _checkout_runtime() -> Runtime:
    """The runtime a checkout-rooted session carries: no workspace at all."""
    return Runtime(registry=StepRegistry(), library=cast("Any", None), agent=cast("Any", None), session_log=NoSessionLog(), state=a_throwaway_state())


async def test_reading_an_annotated_markdown_file_carries_the_notes_fence(tmp_path: Path) -> None:
    target = tmp_path / "note.md"
    target.write_text("one\ntwo\nthree\n", encoding="utf-8")
    sheet = NoteSheet(notes=(add_note(NoteSheet(), anchor=NoteAnchor(exact="two"), text="watch this")[1],))
    save_sheet(tmp_path, target, sheet)

    result = await _agent(_reads_then_echoes_the_tool_return("note.md"), tmp_path).run("read note.md", deps=_runtime(tmp_path))

    assert "--- owner notes (annotations, not part of the file) ---" in result.output
    assert '[n1] line 2, status open, on "two": watch this' in result.output


async def test_a_markdown_file_with_no_sidecar_is_untouched(tmp_path: Path) -> None:
    target = tmp_path / "plain.md"
    target.write_text("one\ntwo\nthree\n", encoding="utf-8")

    result = await _agent(_reads_then_echoes_the_tool_return("plain.md"), tmp_path).run("read plain.md", deps=_runtime(tmp_path))

    assert "owner notes" not in result.output


async def test_a_non_markdown_file_is_untouched_even_with_a_sidecar(tmp_path: Path) -> None:
    target = tmp_path / "data.txt"
    target.write_text("one\ntwo\nthree\n", encoding="utf-8")
    sheet = NoteSheet(notes=(add_note(NoteSheet(), anchor=NoteAnchor(exact="two"), text="watch this")[1],))
    save_sheet(tmp_path, target, sheet)

    result = await _agent(_reads_then_echoes_the_tool_return("data.txt"), tmp_path).run("read data.txt", deps=_runtime(tmp_path))

    assert "owner notes" not in result.output


async def test_a_read_with_no_workspace_still_carries_the_notes_fence(tmp_path: Path) -> None:
    """The checkout's own reads are annotated: the mount's root decides, not the session's deps."""
    target = tmp_path / "note.md"
    target.write_text("one\ntwo\nthree\n", encoding="utf-8")
    sheet = NoteSheet(notes=(add_note(NoteSheet(), anchor=NoteAnchor(exact="two"), text="watch this")[1],))
    save_sheet(tmp_path, target, sheet)

    result = await _agent(_reads_then_echoes_the_tool_return("note.md"), tmp_path).run("read note.md", deps=_checkout_runtime())

    assert '[n1] line 2, status open, on "two": watch this' in result.output


async def test_a_workspace_elsewhere_does_not_move_where_notes_are_looked_for(tmp_path: Path) -> None:
    """A note under the MOUNTED root renders even when the session's workspace is another folder."""
    mounted = tmp_path / "mounted"
    mounted.mkdir()
    target = mounted / "note.md"
    target.write_text("one\ntwo\nthree\n", encoding="utf-8")
    sheet = NoteSheet(notes=(add_note(NoteSheet(), anchor=NoteAnchor(exact="two"), text="watch this")[1],))
    save_sheet(mounted, target, sheet)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()

    result = await _agent(_reads_then_echoes_the_tool_return("note.md"), mounted).run("read note.md", deps=_runtime(elsewhere))

    assert '[n1] line 2, status open, on "two": watch this' in result.output


async def test_a_non_utf8_annotated_file_still_carries_the_notes_fence(tmp_path: Path) -> None:
    """Undecodable bytes are replaced, matching the files plugin's own read, never raised at the agent."""
    target = tmp_path / "note.md"
    target.write_bytes(b"caf\xe9 opening\nsecond line\n")
    sheet = NoteSheet(
        notes=(add_note(NoteSheet(), anchor=NoteAnchor(exact="second line"), text="watch this")[1],)
    )
    save_sheet(tmp_path, target, sheet)

    result = await _agent(_reads_then_echoes_the_tool_return("note.md"), tmp_path).run("read note.md", deps=_runtime(tmp_path))

    assert '[n1] line 2, status open, on "second line": watch this' in result.output


async def test_all_archived_notes_leave_the_read_untouched(tmp_path: Path) -> None:
    target = tmp_path / "note.md"
    target.write_text("one\ntwo\nthree\n", encoding="utf-8")
    _sheet, note = add_note(NoteSheet(), anchor=NoteAnchor(exact="two"), text="watch this")
    archived_sheet, _updated = replace_note(NoteSheet(notes=(note,)), note.id, text=None, archived=True)
    save_sheet(tmp_path, target, archived_sheet)

    result = await _agent(_reads_then_echoes_the_tool_return("note.md"), tmp_path).run("read note.md", deps=_runtime(tmp_path))

    assert "owner notes" not in result.output


async def test_all_resolved_threads_leave_the_read_untouched(tmp_path: Path) -> None:
    target = tmp_path / "note.md"
    target.write_text("one\ntwo\nthree\n", encoding="utf-8")
    _sheet, note = add_note(NoteSheet(), anchor=NoteAnchor(exact="two"), text="watch this")
    resolved_sheet, _updated = set_status(NoteSheet(notes=(note,)), note.id, "resolved")
    save_sheet(tmp_path, target, resolved_sheet)

    result = await _agent(_reads_then_echoes_the_tool_return("note.md"), tmp_path).run("read note.md", deps=_runtime(tmp_path))

    assert "owner notes" not in result.output


async def test_an_open_threads_replies_render_beneath_it(tmp_path: Path) -> None:
    target = tmp_path / "note.md"
    target.write_text("one\ntwo\nthree\n", encoding="utf-8")
    sheet, note = add_note(NoteSheet(), anchor=NoteAnchor(exact="two"), text="watch this")
    sheet, _updated = add_reply(sheet, note.id, author="owner", text="any update?")
    sheet, _updated = add_reply(sheet, note.id, author="agent", text="fixed it")
    save_sheet(tmp_path, target, sheet)

    result = await _agent(_reads_then_echoes_the_tool_return("note.md"), tmp_path).run("read note.md", deps=_runtime(tmp_path))

    assert '[n1] line 2, status open, on "two": watch this' in result.output
    assert "    reply (owner): any update?" in result.output
    assert "    reply (agent): fixed it" in result.output


async def test_get_instructions_carries_the_note_keeping_header(tmp_path: Path) -> None:
    """Returned as a function (`InstructionPart.sorted`: static before dynamic), so drive a real run."""
    model = TestModel(call_tools=[])
    agent = Agent(model, deps_type=Runtime, output_type=str, capabilities=[NoteOverlay(root=tmp_path)])

    await agent.run("hi", deps=_checkout_runtime())

    params = model.last_model_request_parameters
    assert params is not None
    text = "\n".join(part.content for part in params.instruction_parts or [])
    assert "# Note keeping" in text
    assert "reply_note" in text


class TestReplyNoteTool:
    """`NoteReplyToolset.reply_note`, the agent's own plugin onto a note's thread."""

    async def test_appends_an_agent_reply_to_the_thread(self, tmp_path: Path) -> None:
        target = tmp_path / "note.md"
        target.write_text("one\ntwo\nthree\n", encoding="utf-8")
        _sheet, note = add_note(NoteSheet(), anchor=NoteAnchor(exact="two"), text="watch this")
        save_sheet(tmp_path, target, NoteSheet(notes=(note,)))
        toolset = NoteReplyToolset(tmp_path)

        outcome = await toolset.reply_note("note.md", note.id, "fixed it")

        assert "added" in outcome.lower()
        sheet = load_sheet(tmp_path, target)
        [reply] = sheet.notes[0].replies
        assert reply.author == "agent"
        assert reply.text == "fixed it"

    async def test_refuses_an_unknown_note_id_in_words(self, tmp_path: Path) -> None:
        target = tmp_path / "note.md"
        target.write_text("one\ntwo\nthree\n", encoding="utf-8")
        save_sheet(tmp_path, target, NoteSheet())
        toolset = NoteReplyToolset(tmp_path)

        with pytest.raises(ModelRetry, match="no note"):
            await toolset.reply_note("note.md", "no-such-id", "fixed it")

    async def test_refuses_a_path_with_no_annotated_file_in_words(self, tmp_path: Path) -> None:
        toolset = NoteReplyToolset(tmp_path)

        with pytest.raises(ModelRetry, match="no annotated file"):
            await toolset.reply_note("ghost.md", "any-id", "fixed it")

    async def test_refuses_a_path_outside_the_root_in_words(self, tmp_path: Path) -> None:
        toolset = NoteReplyToolset(tmp_path / "mounted")
        (tmp_path / "mounted").mkdir()

        with pytest.raises(ModelRetry, match="resolves outside"):
            await toolset.reply_note("../escaped.md", "any-id", "fixed it")

    async def test_a_reply_from_the_agent_reaches_the_next_read(self, tmp_path: Path) -> None:
        target = tmp_path / "note.md"
        target.write_text("one\ntwo\nthree\n", encoding="utf-8")
        _sheet, note = add_note(NoteSheet(), anchor=NoteAnchor(exact="two"), text="watch this")
        save_sheet(tmp_path, target, NoteSheet(notes=(note,)))

        result = await _agent(_reply_then_read("note.md", note.id, "fixed it"), tmp_path).run(
            "reply then read note.md", deps=_runtime(tmp_path)
        )

        assert "    reply (agent): fixed it" in result.output


def _reply_then_read(path: str, note_id: str, text: str) -> FunctionModel:
    """Call `reply_note` once, then `read_file`, then answer with that read's return verbatim."""

    def function(messages: Sequence[ModelMessage], info: AgentInfo) -> ModelResponse:
        del info
        returns = [part for message in messages for part in message.parts if isinstance(part, ToolReturnPart)]
        if not returns:
            return ModelResponse(parts=[ToolCallPart("reply_note", {"path": path, "note_id": note_id, "text": text})])
        if len(returns) == 1:
            return ModelResponse(parts=[ToolCallPart("read_file", {"path": path})])
        return ModelResponse(parts=[TextPart(str(returns[-1].content))])

    return FunctionModel(function)


def _script_spill_then_readback(observed: list[str]) -> FunctionModel:
    """Read `big.md` once; once its return spills, read the handle back; record both returns."""

    def function(messages: Sequence[ModelMessage], info: AgentInfo) -> ModelResponse:
        del info
        returns = [part for message in messages for part in message.parts if isinstance(part, ToolReturnPart)]
        if not returns:
            return ModelResponse(parts=[ToolCallPart("read_file", {"path": "big.md"})])
        if len(returns) == 1:
            content = str(returns[0].content)
            observed.append(content)
            handle = re.search(r"stored to handle '([^']+)'", content)
            assert handle is not None, f"expected a spill handle in the tool return, got: {content!r}"
            return ModelResponse(parts=[ToolCallPart("read_tool_result", {"handle": handle.group(1)})])
        observed.append(str(returns[-1].content))
        return ModelResponse(parts=[TextPart("done")])

    return FunctionModel(function)


async def test_a_spilled_annotated_read_still_carries_the_fence_and_the_handle_still_works(tmp_path: Path) -> None:
    target = tmp_path / "big.md"
    body = "ANNOTATED ANCHOR HERE\n" + "\n".join(f"filler line number {i} with enough padding text." for i in range(1, 100))
    target.write_text(body, encoding="utf-8")
    assert len(body) > 2_000, "the fixture must land comfortably over the test's own spill band"
    sheet = NoteSheet(
        notes=(add_note(NoteSheet(), anchor=NoteAnchor(exact="ANNOTATED ANCHOR HERE"), text="watch this")[1],)
    )
    save_sheet(tmp_path, target, sheet)
    limits: ToolOutputLimits[Runtime] = ToolOutputLimits(
        bands=[Band(over=200, action=Spill(preview_chars=80))],
        store=LocalFileStore(base_dir=tmp_path / "spill"),
    )
    observed: list[str] = []
    # `ToolOutputLimits` listed BEFORE `NoteOverlay` -- the naive list order that would defeat NoteOverlay
    # without its own `get_ordering()` declaration (`combined.py` dispatches `after_tool_execute`
    # in reverse list order, so this position would otherwise make NoteOverlay run first, on the raw
    # content, with its fence then discarded by the spill that follows it).
    agent: Agent[Runtime, str] = Agent(
        _script_spill_then_readback(observed),
        deps_type=Runtime,
        output_type=str,
        capabilities=[LocalWorkspace(tmp_path), UnnumberedFileSystem[Runtime](root_dir=tmp_path), limits, NoteOverlay(root=tmp_path)],
    )

    await agent.run("read big.md", deps=_runtime(tmp_path))

    spill_return, readback_return = observed
    assert "Tool output too large" in spill_return, "the fixture must actually have spilled"
    assert "--- owner notes (annotations, not part of the file) ---" in spill_return
    assert '[n1] line 1, status open, on "ANNOTATED ANCHOR HERE": watch this' in spill_return
    assert "owner notes" not in readback_return, "the fence is NoteOverlay's addition, not part of the stored payload"
    assert "filler line number 50" in readback_return, "the spilled handle must still retrieve the real file content"
