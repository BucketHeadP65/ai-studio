"""The Claude Code loader: one session file becomes one step of pydantic-ai messages."""

from __future__ import annotations

import json

from typing import TYPE_CHECKING

import pytest

from pydantic_ai.messages import ModelResponse, ThinkingPart, ToolCallPart, ToolReturnPart

from core.ai.transcripts.infrastructure.claude_code import ClaudeCodeSessionLoader

if TYPE_CHECKING:
    from pathlib import Path

SESSION = "0a1b2c3d-session"
STAMP = "2026-09-04T12:00:00Z"


def record(kind: str, uuid: str, message: dict[str, object] | None = None, **extra: object) -> dict[str, object]:
    line: dict[str, object] = {"type": kind, "uuid": uuid, "timestamp": STAMP, **extra}
    if message is not None:
        line["message"] = message
    return line


def assistant(message_id: str, content: list[dict[str, object]], usage: dict[str, int]) -> dict[str, object]:
    return {"id": message_id, "role": "assistant", "model": "claude-x", "content": content, "usage": usage}


def a_session(root: Path, *, tool_result_is_error: bool = False) -> None:
    lines = [
        record("user", "u1", {"role": "user", "content": "Read the file"}),
        record(
            "assistant",
            "a1",
            assistant(
                "msg_1",
                [{"type": "thinking", "thinking": "I should read it.", "signature": "sig"}],
                {"input_tokens": 10, "output_tokens": 2},
            ),
        ),
        record(
            "assistant",
            "a2",
            assistant(
                "msg_1",
                [{"type": "tool_use", "id": "toolu_1", "name": "Read", "input": {"path": "a.py"}}],
                {"input_tokens": 10, "output_tokens": 5},
            ),
        ),
        record(
            "user",
            "u2",
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "toolu_1",
                        "content": [{"type": "text", "text": "print(1)"}],
                        "is_error": tool_result_is_error,
                    }
                ],
            },
        ),
        record(
            "assistant",
            "s1",
            assistant("msg_side", [{"type": "text", "text": "a subagent talking"}], {"input_tokens": 1, "output_tokens": 1}),
            isSidechain=True,
        ),
        record("file-history-snapshot", "f1"),
        record(
            "assistant",
            "a3",
            assistant(
                "msg_2", [{"type": "text", "text": "It prints 1."}], {"input_tokens": 20, "output_tokens": 4, "cache_read_input_tokens": 7}
            ),
        ),
    ]
    (root / f"{SESSION}.jsonl").write_text("\n".join(json.dumps(line) for line in lines) + "\n", encoding="utf-8")


async def test_the_whole_file_is_one_step_with_ordinals_in_file_order(tmp_path: Path) -> None:
    a_session(tmp_path)

    transcript = await ClaudeCodeSessionLoader(tmp_path)(SESSION)

    assert transcript.source == f"claude-code:{SESSION}"
    assert len(transcript.steps) == 1
    assert transcript.steps[0].ordinals == (0, 1, 2, 3)


async def test_assistant_records_sharing_a_message_id_are_one_response(tmp_path: Path) -> None:
    a_session(tmp_path)

    transcript = await ClaudeCodeSessionLoader(tmp_path)(SESSION)

    response = transcript.messages[1]
    assert isinstance(response, ModelResponse)
    assert [type(part) for part in response.parts] == [ThinkingPart, ToolCallPart]
    assert response.model_name == "claude-x"


async def test_a_tool_result_answers_its_call_under_the_calls_name(tmp_path: Path) -> None:
    a_session(tmp_path)

    (found,) = (await ClaudeCodeSessionLoader(tmp_path)(SESSION)).calls()

    assert isinstance(found.answer, ToolReturnPart)
    assert found.answer.tool_name == "Read"
    assert found.answer.content == "print(1)"
    assert found.answer.outcome == "success"


async def test_an_error_result_is_a_failed_outcome(tmp_path: Path) -> None:
    a_session(tmp_path, tool_result_is_error=True)

    (found,) = (await ClaudeCodeSessionLoader(tmp_path)(SESSION)).calls()

    assert isinstance(found.answer, ToolReturnPart)
    assert found.answer.outcome == "failed"


async def test_usage_carries_the_cache_reads(tmp_path: Path) -> None:
    a_session(tmp_path)

    last = (await ClaudeCodeSessionLoader(tmp_path)(SESSION)).messages[3]

    assert isinstance(last, ModelResponse)
    assert last.usage.cache_read_tokens == 7


async def test_sidechain_and_bookkeeping_records_are_dropped(tmp_path: Path) -> None:
    a_session(tmp_path)

    transcript = await ClaudeCodeSessionLoader(tmp_path)(SESSION)

    assert len(transcript.messages) == 4


async def test_a_missing_session_is_refused(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        await ClaudeCodeSessionLoader(tmp_path)("no-such-session")
