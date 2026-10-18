"""The terminal's cwd-containment gate: `registered_knowledge_base_roots` over the studio's registry.

Which directories a terminal may start in is the registry's answer plus this checkout, so what
this file pins is the gate's reading of that answer, including the two cases where the gate must
stay at its narrowest rather than raise.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from harness.adapters.knowledge_base import registered_knowledge_base_roots
from harness.settings import CHECKOUT
from tests.support.fakes.studio_registry import FakeStudioApi, registry_row, unreachable_studio

if TYPE_CHECKING:
    from pathlib import Path

    import pytest


class TestRegisteredKnowledgeBaseRoots:
    """Every registered knowledge base's folder, plus this checkout, always resolved."""

    async def test_every_registered_folder_plus_the_checkout_is_a_root(self, tmp_path: Path) -> None:
        primary = tmp_path / "primary"
        other = tmp_path / "other"
        primary.mkdir()
        other.mkdir()
        studio = FakeStudioApi(rows=[registry_row("primary", primary), registry_row("other", other)])

        roots = await registered_knowledge_base_roots(studio)

        assert roots == (primary.resolve(), other.resolve(), CHECKOUT.resolve())

    async def test_a_private_knowledge_base_is_still_a_legal_root(self, tmp_path: Path) -> None:
        """Keeping a vault out of the shared library is a retrieval decision. The owner still works in it."""
        vault = tmp_path / "client-vault"
        vault.mkdir()
        studio = FakeStudioApi(rows=[registry_row("client-vault", vault, private=True)])

        assert vault.resolve() in await registered_knowledge_base_roots(studio)

    async def test_a_collection_with_no_folder_is_skipped_not_raised(self, tmp_path: Path) -> None:
        good = tmp_path / "good"
        good.mkdir()
        studio = FakeStudioApi(rows=[registry_row("good", good), registry_row("orphaned", None, registered=False)])

        assert await registered_knowledge_base_roots(studio) == (good.resolve(), CHECKOUT.resolve())

    async def test_an_unreachable_studio_leaves_only_the_checkout(self, caplog: pytest.LogCaptureFixture) -> None:
        """The narrower gate, never the wider one: a security gate degrades, it does not open up."""
        with caplog.at_level("WARNING"):
            roots = await registered_knowledge_base_roots(unreachable_studio())

        assert roots == (CHECKOUT.resolve(),)
        assert "studio.test:8100" in caplog.text

    async def test_a_studio_answering_something_else_leaves_only_the_checkout(self) -> None:
        """A failure status is no answer about the roots, and no answer is the narrowest gate."""
        assert await registered_knowledge_base_roots(FakeStudioApi(listing_status=500)) == (CHECKOUT.resolve(),)
