"""build_environment's knowledge base parameter is a slice, not the whole MCPToolset.

The proof harness wraps the knowledge base to record traffic and enforce the withheld set; the
wrapper can only exist if the seam names the three members build_environment actually uses.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic_ai.models.test import TestModel

from harness.composition import build_environment
from harness.session import NoSessionLog
from harness.settings import WebSearchSettings
from tests.support.environment import fake_engram
from tests.support.state import records_under
from tests.support.tiers import tiers_under

if TYPE_CHECKING:
    from pathlib import Path

    from harness.domain.ports import KnowledgeBaseSurface


def test_the_support_fake_satisfies_the_seam() -> None:
    surface: KnowledgeBaseSurface = fake_engram()  # assignment IS the assertion; pyright checks it
    assert surface is not None


async def test_build_world_accepts_the_seam(tmp_path: Path) -> None:
    registry = await build_environment(model=TestModel(), engram=fake_engram(), sandbox=None, external_agents=dict, session_log=NoSessionLog(), custom=tmp_path / "custom", records=records_under(tmp_path), web_search=WebSearchSettings(), tiers=tiers_under(tmp_path))
    assert registry.grants
