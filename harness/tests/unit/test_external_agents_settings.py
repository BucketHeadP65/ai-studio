"""The external-agent registry: one JSON file in the state dir, read fresh on every call.

Fresh per read rather than cached, so an edit reaches the next turn with no restart. Every case
reads a file under its own directory, so nothing here touches the deployment's own state.
"""

from __future__ import annotations

import json

from typing import TYPE_CHECKING

import pytest

from pydantic import ValidationError

from harness.settings import CHECKOUT, ExternalAgentEntry, ExternalAgentSettings, read_external_agents
from tests.support.state import a_state

if TYPE_CHECKING:
    from pathlib import Path


def _write(payload: object, home: Path) -> Path:
    path = a_state(home).external_agents
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_no_file_means_no_agents(tmp_path: Path) -> None:
    assert read_external_agents(a_state(tmp_path).external_agents) == {}


def test_one_entry_reads_back_whole(tmp_path: Path) -> None:
    _write(
        {
            "codex": {
                "command": "npx",
                "args": ["-y", "codex-acp"],
                "env": {"OPENAI_API_KEY": "sk-test"},
                "permission": "auto",
                "timeout_seconds": 120.0,
            }
        },
        tmp_path,
    )

    registry = read_external_agents(a_state(tmp_path).external_agents)

    assert set(registry) == {"codex"}
    entry = registry["codex"]
    assert entry.command == "npx"
    assert entry.args == ("-y", "codex-acp")
    assert entry.env == {"OPENAI_API_KEY": "sk-test"}
    assert entry.permission == "auto"
    assert entry.timeout_seconds == 120.0


def test_the_defaults_are_gate_and_ten_minutes(tmp_path: Path) -> None:
    _write({"claude-code": {"command": "claude-code-acp"}}, tmp_path)

    entry = read_external_agents(a_state(tmp_path).external_agents)["claude-code"]

    assert entry.args == ()
    assert entry.env == {}
    assert entry.permission == "gate"
    assert entry.timeout_seconds == 600.0


def test_an_edit_reaches_the_next_call_with_no_restart(tmp_path: Path) -> None:
    _write({"a": {"command": "one"}}, tmp_path)
    assert set(read_external_agents(a_state(tmp_path).external_agents)) == {"a"}

    _write({"a": {"command": "one"}, "b": {"command": "two"}}, tmp_path)

    assert set(read_external_agents(a_state(tmp_path).external_agents)) == {"a", "b"}


def test_an_unknown_permission_is_refused_by_name(tmp_path: Path) -> None:
    _write({"a": {"command": "one", "permission": "sometimes"}}, tmp_path)

    with pytest.raises(ValueError, match="permission"):
        read_external_agents(a_state(tmp_path).external_agents)


def test_an_unknown_key_is_refused_rather_than_ignored(tmp_path: Path) -> None:
    """A typo in a registry entry must be loud: a silently dropped key is a plugin that never opens."""
    _write({"a": {"command": "one", "timeoutSeconds": 30}}, tmp_path)

    with pytest.raises(ValueError, match="timeoutSeconds"):
        read_external_agents(a_state(tmp_path).external_agents)


def test_a_torn_file_is_refused_with_the_path_in_the_message(tmp_path: Path) -> None:
    _write({}, tmp_path).write_text("{not json", encoding="utf-8")

    with pytest.raises(ValueError, match=r"external-agents\.json"):
        read_external_agents(a_state(tmp_path).external_agents)


def test_the_model_validates_a_bare_mapping() -> None:
    settings = ExternalAgentSettings.model_validate({"a": {"command": "one"}})

    assert settings.root == {"a": ExternalAgentEntry(command="one")}


def test_the_example_file_in_the_checkout_parses() -> None:
    """The shipped example is the deployment's starting point, so it must be a valid registry."""
    example = json.loads((CHECKOUT / "external-agents.example.json").read_text(encoding="utf-8"))

    assert set(ExternalAgentSettings.model_validate(example).root)


def test_a_validation_error_names_the_entry() -> None:
    with pytest.raises(ValidationError, match="command"):
        ExternalAgentSettings.model_validate({"a": {}})
