"""The note overlay: the capability that shows the agent the owner notes on a file it reads, and lets it reply.

It appends the open, unarchived threads to a `read_file` return for a markdown file that carries a note sidecar. A
resolved thread is a settled conversation and is left out, as an archived one is. The root a path is resolved against
is fixed at the mount and must be the root the mounted files plugin reads from, because `read_file`'s `path` is relative
to it. It wraps `ToolOutputLimits`, so it sees `read_file`'s return after that capability has decided its shape: a plain
string gets the fence appended, and a spilled `ToolReturn` gets it appended to `return_value`, leaving `metadata` and
its `read_tool_result` handle untouched. The agent answers through `reply_note`, never by editing a note or its sidecar,
which the file tools cannot see.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from pydantic_ai.capabilities import AbstractCapability, CapabilityOrdering
from pydantic_ai.exceptions import ModelRetry
from pydantic_ai.messages import ToolReturn
from pydantic_ai.toolsets import FunctionToolset
from pydantic_ai_harness.tool_output_limits import ToolOutputLimits

from harness.features.projects import service
from harness.features.projects.domain import annotate_read
from harness.features.projects.exceptions import ProjectError
from harness.session import Runtime

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping
    from pathlib import Path

    from engram.kb.contracts import NoteSheet
    from pydantic_ai.messages import ToolCallPart
    from pydantic_ai.tools import RunContext, ToolDefinition
    from pydantic_ai.toolsets import AgentToolset

_READ_FILE_TOOL = "read_file"

_NOTE_KEEPING_INSTRUCTIONS = (
    "# Note keeping\n"
    "- Notes are the owner's span-anchored margin comments on this project's files, kept in "
    "sidecars the file tools deliberately cannot read or write.\n"
    "- They arrive appended under a `read_file` of the annotated file: open threads only, "
    "each with its replies and status.\n"
    "- Reply with `reply_note(path, note_id, text)`; a reply joins the thread as your own voice. "
    "Never answer a note by editing the note or its sidecar, and never remove the anchored text "
    "just to make a note disappear.\n"
    "- An open note on a span is standing feedback about exactly that span. Treat it as a "
    "request to honor while you work on that file. A resolved thread is a settled conversation "
    "and is not shown to you."
)


@dataclass
class NoteOverlay(AbstractCapability[Runtime]):
    """Appends the owner notes to a `read_file` return, for an annotated markdown file."""

    root: Path
    """The directory a `read_file` path is resolved against, the mounted files plugin's own root."""

    def get_ordering(self) -> CapabilityOrdering:
        """Wrap `ToolOutputLimits`, so this capability's rewrite always runs on its output, whatever the list order.

        Returns:
            The ordering that wraps `ToolOutputLimits`.
        """
        return CapabilityOrdering(wraps=[ToolOutputLimits])

    def get_instructions(self) -> Callable[[RunContext[Runtime]], str]:
        """The note-keeping instructions: what owner notes are, how they arrive, and how to answer them.

        They come as a function, so they render among the dynamic instructions after the agent's own identity. The
        text never varies by request.

        Returns:
            A function that answers the instructions for one request.
        """

        def compose(ctx: RunContext[Runtime]) -> str:
            del ctx
            return _NOTE_KEEPING_INSTRUCTIONS

        return compose

    def get_toolset(self) -> AgentToolset[Runtime]:
        """`reply_note`, resolved against this capability's own mounted root.

        Returns:
            The toolset that holds `reply_note`.
        """
        return NoteReplyToolset(self.root)

    async def after_tool_execute(
        self,
        ctx: RunContext[Runtime],  # noqa: ARG002 (required by AbstractCapability.after_tool_execute's signature)
        *,
        call: ToolCallPart,
        tool_def: ToolDefinition,  # noqa: ARG002 (required by AbstractCapability.after_tool_execute's signature)
        args: Mapping[str, object],
        result: object,
    ) -> object:
        """The tool result untouched, unless `call` read an annotated markdown file.

        Args:
            ctx: The run context.
            call: The tool call that just ran.
            tool_def: The called tool's definition.
            args: The call's validated arguments.
            result: What the tool returned.

        Returns:
            `result` with the owner notes fence appended, or `result` untouched.
        """
        if call.tool_name != _READ_FILE_TOOL:
            return result
        path = args.get("path")
        if not isinstance(path, str) or not path.endswith(".md"):
            return result
        annotated = service.annotated_text(self.root, path)
        if annotated is None:
            return result
        text, sheet = annotated
        return _annotate_result(result, text, sheet)


def _annotate_result(result: object, text: str, sheet: NoteSheet) -> object:
    """`result` with the fence appended where the model reads it: a plain string or a `ToolReturn`'s string `return_value`."""
    if isinstance(result, str):
        return annotate_read(result, text, sheet)
    if isinstance(result, ToolReturn) and isinstance(result.return_value, str):
        return replace(result, return_value=annotate_read(result.return_value, text, sheet))
    return result


class NoteReplyToolset(FunctionToolset[Runtime]):
    """Holds `reply_note`, the one tool the agent has for answering an owner note's thread."""

    def __init__(self, root: Path) -> None:
        """Register `reply_note` over `root`.

        Args:
            root: The root the agent's file tools read from, which every `path` is resolved against.
        """
        super().__init__()
        self._root = root
        self.add_function(self.reply_note, name="reply_note")

    async def reply_note(self, path: str, note_id: str, text: str) -> str:
        """Reply to a note's thread, as the agent.

        Args:
            path: Root-relative path of the annotated markdown file.
            note_id: The note whose thread the reply joins.
            text: The reply body.
        """
        try:
            service.reply_as_agent(self._root, path, note_id, text)
        except ProjectError as error:
            raise ModelRetry(str(error)) from error
        return f"Reply added to note {note_id!r}."
