"""A Claude Code session as a transcript source: `claude-code:<session id>` opens one session file.

The file `<session id>.jsonl` in a Claude Code project folder holds one record per line. The `user` and `assistant`
records carry an Anthropic-API message whose content is a list of blocks, and those become pydantic-ai messages in one
`Step`. Everything else in the file is dropped, as is a sidechain record, which belongs to a subagent's own conversation.
Nothing is inferred: a denied permission arrives as an error result whose text says so, and stays a failed outcome.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, Field
from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelRequestPart,
    ModelResponse,
    ModelResponsePart,
    TextPart,
    ThinkingPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.usage import RequestUsage

from core.ai.transcripts.models import Step, Transcript

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path


class _Usage(BaseModel):
    model_config = ConfigDict(extra="ignore")

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0


class _Block(BaseModel):
    """One content block of an Anthropic-API message, whichever type it is."""

    model_config = ConfigDict(extra="ignore")

    type: str
    text: str | None = None
    thinking: str | None = None
    signature: str | None = None
    id: str | None = None
    name: str | None = None
    input: dict[str, object] | None = None
    tool_use_id: str | None = None
    content: str | list[_Block] | None = None
    is_error: bool = False


class _Message(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str | None = None
    role: str
    model: str | None = None
    content: str | list[_Block]
    usage: _Usage | None = None


class _Record(BaseModel):
    """One line of a session file."""

    model_config = ConfigDict(extra="ignore")

    type: str
    uuid: str | None = None
    timestamp: datetime | None = None
    is_sidechain: bool = Field(default=False, alias="isSidechain")
    message: _Message | None = None


@dataclass(frozen=True)
class ClaudeCodeSessionLoader:
    """Opens a Claude Code session file as one step of pydantic-ai messages.

    A tool result becomes a `ToolReturnPart` named after the call it answers, and `is_error` becomes the outcome `failed`.
    Consecutive assistant records sharing a message id are one response.
    """

    root: Path
    """The Claude Code project folder that holds the session files."""

    async def __call__(self, locator: str) -> Transcript:
        """Open the session file `<locator>.jsonl` under `root` as one step.

        Args:
            locator: The session id.

        Returns:
            The session, its source `claude-code:<session id>`.

        Raises:
            FileNotFoundError: when there is no such session file.
            ValueError: when a line does not read as a record, or the file holds no message.
        """
        path = self.root / f"{locator}.jsonl"
        messages = tuple(_messages(path))
        return Transcript(source=f"claude-code:{locator}", steps=(Step(messages=messages, ordinals=tuple(range(len(messages)))),))


def _messages(path: Path) -> Iterator[ModelMessage]:
    tool_names: dict[str, str] = {}
    open_response: ModelResponse | None = None
    open_response_id: str | None = None
    for record in _records(path):
        message = record.message
        if message is None:
            continue
        if record.type == "assistant":
            response = _response(message, record.timestamp, tool_names)
            if open_response is not None and message.id is not None and message.id == open_response_id:
                open_response = _joined(open_response, response)
                continue
            if open_response is not None:
                yield open_response
            open_response, open_response_id = response, message.id
            continue
        if open_response is not None:
            yield open_response
            open_response = open_response_id = None
        yield _request(message, record.uuid, tool_names)
    if open_response is not None:
        yield open_response


def _records(path: Path) -> Iterator[_Record]:
    """The user and assistant records of the main conversation, in file order."""
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        record = _Record.model_validate_json(line)
        if not record.is_sidechain and record.type in {"user", "assistant"}:
            yield record


def _joined(earlier: ModelResponse, later: ModelResponse) -> ModelResponse:
    """Two records of one response as one: the parts in order, the usage of the later record, the stamps of the earlier."""
    return ModelResponse(
        parts=[*earlier.parts, *later.parts], usage=later.usage, model_name=earlier.model_name, timestamp=earlier.timestamp
    )


def _response(message: _Message, timestamp: datetime | None, tool_names: dict[str, str]) -> ModelResponse:
    parts: list[ModelResponsePart] = []
    for block in _blocks(message.content):
        if block.type == "text" and block.text is not None:
            parts.append(TextPart(content=block.text))
        elif block.type == "thinking" and block.thinking is not None:
            parts.append(ThinkingPart(content=block.thinking, signature=block.signature))
        elif block.type == "tool_use" and block.id is not None and block.name is not None:
            tool_names[block.id] = block.name
            parts.append(ToolCallPart(tool_name=block.name, args=block.input, tool_call_id=block.id))
    usage = RequestUsage()
    if message.usage is not None:
        usage = RequestUsage(
            input_tokens=message.usage.input_tokens,
            output_tokens=message.usage.output_tokens,
            cache_read_tokens=message.usage.cache_read_input_tokens,
            cache_write_tokens=message.usage.cache_creation_input_tokens,
        )
    if timestamp is None:
        return ModelResponse(parts=parts, usage=usage, model_name=message.model)
    return ModelResponse(parts=parts, usage=usage, model_name=message.model, timestamp=timestamp)


def _request(message: _Message, run_id: str | None, tool_names: dict[str, str]) -> ModelRequest:
    parts: list[ModelRequestPart] = []
    for block in _blocks(message.content):
        if block.type == "text" and block.text is not None:
            parts.append(UserPromptPart(content=block.text))
        elif block.type == "tool_result" and block.tool_use_id is not None:
            parts.append(
                ToolReturnPart(
                    tool_name=tool_names.get(block.tool_use_id, "unknown"),
                    content=_text_of(block.content),
                    tool_call_id=block.tool_use_id,
                    outcome="failed" if block.is_error else "success",
                )
            )
    return ModelRequest(parts=parts, run_id=run_id)


def _blocks(content: str | list[_Block]) -> list[_Block]:
    return [_Block(type="text", text=content)] if isinstance(content, str) else content


def _text_of(content: str | list[_Block] | None) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    return "\n".join(block.text for block in content if block.type == "text" and block.text is not None)
