from collections.abc import Sequence
from pathlib import Path

import pytest

from pydantic_ai import Agent
from pydantic_ai.capabilities import LocalWorkspace
from pydantic_ai.exceptions import UserError
from pydantic_ai.messages import ModelMessage, ModelRequest, ModelResponse, TextPart, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai_harness.shell import Shell

from core.ai.shell import PathGuardedShell

DENIED = ("**/*.threads.json", "creds.json", "**/creds.json", "**/.env", "**/.env.*")


async def _answer(cwd: Path, command: str, *, tool: str = "run_command", denied: Sequence[str] = DENIED) -> str:
    """What the model was sent back after calling `tool` with `command` in a guarded shell on a workspace at `cwd`."""
    answers: list[str] = []

    def model(messages: Sequence[ModelMessage], info: AgentInfo) -> ModelResponse:
        del info
        for message in messages:
            if isinstance(message, ModelRequest):
                answers.extend(str(part.content) for part in message.parts if isinstance(part, ToolReturnPart))
        if answers:
            return ModelResponse(parts=[TextPart("done")])
        return ModelResponse(parts=[ToolCallPart(tool_name=tool, args={"command": command})])

    agent = Agent(FunctionModel(model), capabilities=[LocalWorkspace(cwd), PathGuardedShell(shell=Shell(), denied_path_patterns=denied)])
    await agent.run("run it")
    return answers[0]


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    (tmp_path / "config").mkdir()
    (tmp_path / "creds.json").write_text('{"key": "secret"}')
    (tmp_path / "config" / "creds.json").write_text('{"key": "secret"}')
    (tmp_path / ".env").write_text("KEY=secret")
    (tmp_path / "config" / ".env.local").write_text("KEY=secret")
    (tmp_path / "notes.md").write_text("plain notes")
    (tmp_path / "notes.md.threads.json").write_text("[]")
    return tmp_path


async def test_a_command_naming_a_denied_file_is_refused_before_it_runs(workspace: Path) -> None:
    answer = await _answer(workspace, "touch ran && cat creds.json")

    assert answer == "Path 'creds.json' is denied by pattern 'creds.json'."
    assert not (workspace / "ran").exists()


@pytest.mark.parametrize(
    "command",
    [
        "cat config/creds.json",
        "cd config && cat creds.json",
        "cat creds.json|base64",
        "echo $(cat .env)",
        "cat ./config/.env.local",
        "cat .en*",
        "grep KEY --file=.env",
        "cat notes.md.threads.json",
        "cat config/../creds.json",
    ],
)
async def test_every_way_of_naming_a_denied_file_is_refused(workspace: Path, command: str) -> None:
    answer = await _answer(workspace, command)

    assert "is denied by pattern" in answer
    assert "secret" not in answer


async def test_an_absolute_path_to_a_denied_file_is_refused(workspace: Path) -> None:
    answer = await _answer(workspace, f"cat {workspace / 'config' / 'creds.json'}")

    assert "is denied by pattern" in answer


async def test_a_link_to_a_denied_file_is_refused(workspace: Path) -> None:
    (workspace / "alias.txt").symlink_to(workspace / "creds.json")

    answer = await _answer(workspace, "cat alias.txt")

    assert answer == "Path 'alias.txt' is denied by pattern 'creds.json'."


async def test_a_background_command_naming_a_denied_file_is_refused(workspace: Path) -> None:
    answer = await _answer(workspace, "cat creds.json", tool="start_command")

    assert "is denied by pattern" in answer


async def test_a_command_naming_no_denied_path_runs(workspace: Path) -> None:
    answer = await _answer(workspace, "cat notes.md")

    assert "plain notes" in answer


async def test_a_home_directory_of_an_unknown_user_is_read_as_written(workspace: Path) -> None:
    answer = await _answer(workspace, "echo ~no-such-user-here/notes*")

    assert "~no-such-user-here/notes*" in answer


async def test_a_relative_path_is_read_from_the_workspace_directory(workspace: Path) -> None:
    (workspace / "config" / ".env").write_text("KEY=secret")

    answer = await _answer(workspace, "cat config/.env", denied=("config/.env",))

    assert answer == "Path 'config/.env' is denied by pattern 'config/.env'."


async def test_a_glob_is_expanded_in_the_workspace_directory(workspace: Path) -> None:
    (workspace / "config" / "public.json").write_text("{}")

    answer = await _answer(workspace, "cat config/*.json")

    assert answer == "Path 'config/creds.json' is denied by pattern '**/creds.json'."


async def test_a_guarded_shell_run_with_no_workspace_fails_at_run_start() -> None:
    calls: list[int] = []

    def model(messages: Sequence[ModelMessage], info: AgentInfo) -> ModelResponse:
        del messages, info
        calls.append(1)
        return ModelResponse(parts=[TextPart("done")])

    agent = Agent(FunctionModel(model), capabilities=[PathGuardedShell(shell=Shell(), denied_path_patterns=DENIED)])

    with pytest.raises(UserError, match="`Shell` needs a workspace"):
        await agent.run("run it")

    assert not calls
