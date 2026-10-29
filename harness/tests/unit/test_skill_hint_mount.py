"""`skill_hint_capability`: the chat agent's skill hint, built from its settings, or none."""

from __future__ import annotations

from typing import TYPE_CHECKING

from core.ai.skill_hint import SkillHint
from core.settings import GatewaySettings
from pydantic_ai.models.test import TestModel
from pydantic_ai.tools import RunContext
from pydantic_ai.usage import RunUsage
from pydantic_ai_harness.system_reminders import SystemReminders

from harness.domain.page_context import PageContext, compose_prompt
from harness.features.authoring.skill_hint import skill_hint_capability
from harness.features.sessions.project_config import ProjectConfig
from harness.session import Runtime, answerable, not_answerable
from harness.settings import HarnessSettings, SkillHintSettings
from tests.support.runtime import a_runtime
from tests.support.state import a_state

if TYPE_CHECKING:
    from pathlib import Path

    import pytest


def _ctx(prompt: str) -> RunContext[Runtime]:
    return RunContext(deps=a_runtime(), model=TestModel(), usage=RunUsage(), prompt=prompt)


def test_off_or_without_a_gateway_key_there_is_no_hint(tmp_path: Path) -> None:
    state = a_state(tmp_path)

    assert skill_hint_capability(SkillHintSettings(enabled=False), GatewaySettings(api_key="k"), state=state, project=ProjectConfig(), spend=None) is None
    assert skill_hint_capability(SkillHintSettings(), GatewaySettings(), state=state, project=ProjectConfig(), spend=None) is None


def test_without_a_gateway_key_the_hint_warns_naming_the_key(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level("WARNING"):
        skill_hint_capability(SkillHintSettings(), GatewaySettings(), state=a_state(tmp_path), project=ProjectConfig(), spend=None)

    assert "infrastructure.gateway.api_key" in caplog.text


def test_the_hint_asks_jev_latest_by_default_and_in_the_committed_file() -> None:
    assert SkillHintSettings().model == "jev-latest"
    assert HarnessSettings().skill_hint.model == "jev-latest"


def test_on_with_a_key_the_hint_is_a_dynamic_reminder_carrying_its_settings(tmp_path: Path) -> None:
    built = skill_hint_capability(
        SkillHintSettings(threshold=0.7, deadline_seconds=1.5), GatewaySettings(api_key="k"), state=a_state(tmp_path), project=ProjectConfig(), spend=None
    )

    assert isinstance(built, SystemReminders)
    (hint,) = built.dynamic_reminders
    assert isinstance(hint, SkillHint)
    assert (hint.threshold, hint.deadline_seconds) == (0.7, 1.5)


def _hint(tmp_path: Path) -> SkillHint[Runtime]:
    built = skill_hint_capability(SkillHintSettings(), GatewaySettings(api_key="k"), state=a_state(tmp_path), project=ProjectConfig(), spend=None)
    assert isinstance(built, SystemReminders)
    (hint,) = built.dynamic_reminders
    assert isinstance(hint, SkillHint)
    return hint


def test_on_an_attended_turn_the_hint_judges_the_message_without_its_page_context(tmp_path: Path) -> None:
    hint = _hint(tmp_path)
    fenced = compose_prompt("draw me a workflow", PageContext(url="/workflows", page="Workflows"))

    with answerable():
        assert hint.text(_ctx(fenced)) == "draw me a workflow"


def test_on_an_unattended_turn_the_hint_has_no_text(tmp_path: Path) -> None:
    hint = _hint(tmp_path)

    with not_answerable():
        assert hint.text(_ctx("draw me a workflow")) is None
