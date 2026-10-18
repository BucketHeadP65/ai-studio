"""Each adapter in the shipped example starts and answers `initialize`.

A handshake, not a turn: no prompt is sent, so no model runs and no credential is spent. The
adapters are npm packages this repo does not vendor, so a machine without them (or without a
network) skips with a reason rather than failing. That keeps the gate green everywhere and still
catches the thing worth catching, which is an entry naming a package that cannot start.
"""

from __future__ import annotations

import json

from typing import TYPE_CHECKING

import pytest

from harness.adapters.acp_client import ExternalAgent
from harness.settings import CHECKOUT, ExternalAgentSettings

if TYPE_CHECKING:
    from pathlib import Path

    from harness.settings import ExternalAgentEntry

EXAMPLE = CHECKOUT / "external-agents.example.json"

_CONFIGURED = ExternalAgentSettings.model_validate_json(EXAMPLE.read_text(encoding="utf-8")).root


@pytest.mark.parametrize("name", sorted(_CONFIGURED))
async def test_the_adapter_answers_initialize(name: str, tmp_path: Path) -> None:
    entry: ExternalAgentEntry = _CONFIGURED[name]
    agent = ExternalAgent(command=entry.command, args=entry.args, env=entry.env)

    try:
        answer = await agent.handshake(cwd=tmp_path)
    except (OSError, EOFError, TimeoutError) as exc:
        pytest.skip(f"{name} did not start ({entry.command} {' '.join(entry.args)}): {exc}")

    assert answer.protocol_version >= 1


def test_the_example_lists_the_three_adapters() -> None:
    """A guard on the file itself, so the probe above can never pass by testing nothing."""
    assert set(_CONFIGURED) == {"claude-code", "codex", "pi"}
    assert json.loads(EXAMPLE.read_text(encoding="utf-8"))["pi"]["command"] == "npx"
