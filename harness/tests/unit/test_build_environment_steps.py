"""`build_environment()` loads active authored steps into the session's registry.

Pending-gate parity at the environment-build seam: a step written but never activated does not load;
once activated it is registered by the very next `build_environment()` call -- the session registry a
fresh process boots with.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic_ai.models.test import TestModel

from harness.composition import build_environment
from harness.features.selfauthoring.infrastructure.steps import FileCustomSteps
from harness.session import NoSessionLog
from harness.settings import WebSearchSettings
from tests.support.environment import fake_engram
from tests.support.selfauthoring import FakeActivationLog
from tests.support.state import records_under
from tests.support.tiers import tiers_under

if TYPE_CHECKING:
    from pathlib import Path

_DOUBLE_SOURCE = """
async def double(x: int) -> int:
    '''Doubles x.'''
    return x * 2
"""


def _store(tmp_path: Path) -> FileCustomSteps:
    return FileCustomSteps(tmp_path / "custom" / "steps", activations=FakeActivationLog())


async def test_a_pending_step_does_not_load_into_the_session_registry(tmp_path: Path) -> None:
    _store(tmp_path).write("double", _DOUBLE_SOURCE)

    registry = await build_environment(TestModel(), engram=fake_engram(), external_agents=dict, session_log=NoSessionLog(), custom=tmp_path / "custom", records=records_under(tmp_path), web_search=WebSearchSettings(), tiers=tiers_under(tmp_path))

    assert "double" not in registry


async def test_an_activated_step_loads_into_the_session_registry(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.write("double", _DOUBLE_SOURCE)
    assert store.activate("double") is True

    registry = await build_environment(TestModel(), engram=fake_engram(), external_agents=dict, session_log=NoSessionLog(), custom=tmp_path / "custom", records=records_under(tmp_path), web_search=WebSearchSettings(), tiers=tiers_under(tmp_path))

    assert "double" in registry
    implementation = registry.get("double")
    assert implementation is not None
    assert (await implementation.function(x=10)) == 20
