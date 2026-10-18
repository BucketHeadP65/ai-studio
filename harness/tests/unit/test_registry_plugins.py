"""`registered_knowledge_bases`: every knowledge base the studio has registered, read over the studio's API.

The registry belongs to the studio, so what this file pins is what the plugins here make of the
studio's answers: which rows become targets, which are skipped and why, and what happens when the
studio does not answer at all.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import httpx
import pytest

from engram.kb.domain.schema import RetrievalSpec
from engram.testing.specs import research_spec

from harness.adapters import knowledge_base as knowledge_base_adapter
from harness.adapters.knowledge_base import StudioPatience
from tests.support.fakes.studio_registry import FakeStudioApi, registry_row, unreachable_studio

if TYPE_CHECKING:
    from pathlib import Path

    from engram.kb.contracts import KnowledgeBaseSchema


def _spec(slug: str, *, collection: str | None = None, private: bool = False) -> KnowledgeBaseSchema:
    """A research spec under `slug`, its collection defaulting to the agreeing case."""
    retrieval = RetrievalSpec(collection=collection if collection is not None else slug, private=private)
    return research_spec().model_copy(update={"slug": slug, "retrieval": retrieval})


async def test_every_registered_knowledge_base_with_a_schema_is_a_target(tmp_path: Path) -> None:
    studio = FakeStudioApi(
        rows=[registry_row("research", tmp_path / "Knowledge"), registry_row("harness-research", tmp_path / "harness-research")],
        specs={"research": _spec("research"), "harness-research": _spec("harness-research")},
    )

    targets = await knowledge_base_adapter.registered_knowledge_bases(studio)

    assert [target.name for target in targets] == ["research", "harness-research"]
    assert [target.schema.slug for target in targets] == ["research", "harness-research"]
    assert [target.collection for target in targets] == ["research", "harness-research"]
    assert targets[1].path == tmp_path / "harness-research"


async def test_each_target_s_schema_is_asked_for_by_name(tmp_path: Path) -> None:
    """The listing says which knowledge bases exist, the spec route says what each one is."""
    studio = FakeStudioApi(rows=[registry_row("research", tmp_path / "Knowledge")], specs={"research": _spec("research")})

    await knowledge_base_adapter.registered_knowledge_bases(studio)

    assert studio.requested == ["/api/knowledge-bases", "/api/knowledge-bases/research/schema"]


async def test_a_private_row_is_skipped_and_logged_at_debug(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    studio = FakeStudioApi(
        rows=[registry_row("private-vault", tmp_path / "vault", private=True)], specs={"private-vault": _spec("private-vault")}
    )

    with caplog.at_level("DEBUG"):
        targets = await knowledge_base_adapter.registered_knowledge_bases(studio)

    assert targets == (), "a private knowledge base never becomes a library target"
    assert "private-vault" in caplog.text
    assert studio.requested == ["/api/knowledge-bases"], "a private row is decided from the listing, its schema is never asked for"


async def test_a_collection_no_registry_row_names_is_not_a_target() -> None:
    """An indexed collection with no folder has nothing to author into, so it is no target."""
    studio = FakeStudioApi(rows=[registry_row("orphaned", None, registered=False)], specs={"orphaned": _spec("orphaned")})

    assert await knowledge_base_adapter.registered_knowledge_bases(studio) == ()


async def test_a_broken_row_is_skipped_and_logged(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    studio = FakeStudioApi(rows=[registry_row("moved-away", tmp_path / "gone", broken=True)], specs={"moved-away": _spec("moved-away")})

    with caplog.at_level("WARNING"):
        targets = await knowledge_base_adapter.registered_knowledge_bases(studio)

    assert targets == ()
    assert "moved-away" in caplog.text


async def test_a_row_whose_schema_the_studio_cannot_serve_is_skipped_not_raised(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """One unreadable schema costs one row, never the whole registry."""
    studio = FakeStudioApi(
        rows=[registry_row("good", tmp_path / "good"), registry_row("schema-less", tmp_path / "schema-less")], specs={"good": _spec("good")}
    )

    with caplog.at_level("WARNING"):
        targets = await knowledge_base_adapter.registered_knowledge_bases(studio)

    assert [target.name for target in targets] == ["good"]
    assert "schema-less" in caplog.text


async def test_a_row_named_after_the_repo_docs_knowledge_base_is_skipped_and_logged(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    studio = FakeStudioApi(rows=[registry_row("repo-docs", tmp_path / "repo-docs")], specs={"repo-docs": _spec("repo-docs")})

    with caplog.at_level("WARNING"):
        targets = await knowledge_base_adapter.registered_knowledge_bases(studio)

    assert targets == (), "this checkout already mounts repo-docs as its own plugin"
    assert "repo-docs" in caplog.text


async def test_a_row_whose_schema_slug_matches_the_repo_docs_knowledge_base_is_skipped(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    studio = FakeStudioApi(rows=[registry_row("mirror", tmp_path / "mirror")], specs={"mirror": _spec("repo-docs")})

    with caplog.at_level("WARNING"):
        targets = await knowledge_base_adapter.registered_knowledge_bases(studio)

    assert targets == (), "the registry name differs, but the schema's own slug still collides"
    assert "mirror" in caplog.text


async def test_three_names_that_agree_are_not_logged(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    studio = FakeStudioApi(
        rows=[registry_row("harness-research", tmp_path / "harness-research")], specs={"harness-research": _spec("harness-research")}
    )

    with caplog.at_level("WARNING"):
        targets = await knowledge_base_adapter.registered_knowledge_bases(studio)

    assert [target.name for target in targets] == ["harness-research"]
    assert caplog.text == ""


async def test_three_names_that_disagree_are_logged_and_the_row_still_stands(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    studio = FakeStudioApi(rows=[registry_row("rag", tmp_path / "Knowledge")], specs={"rag": _spec("knowledge", collection="research")})

    with caplog.at_level("WARNING"):
        targets = await knowledge_base_adapter.registered_knowledge_bases(studio)

    assert [target.name for target in targets] == ["rag"], "a disagreement is reported, never refused"
    for name in ("rag", "knowledge", "research"):
        assert name in caplog.text


class TestAStudioThatDoesNotAnswer:
    """No registry means no knowledge bases, and that must be said rather than shown as an empty one."""

    async def test_an_unreachable_studio_is_a_refusal_naming_it(self) -> None:
        studio = unreachable_studio()

        with pytest.raises(RuntimeError, match=r"studio\.test:8100") as refused:
            await knowledge_base_adapter.registered_knowledge_bases(studio)

        assert "/api/knowledge-bases" in str(refused.value)

    async def test_a_failure_status_on_the_listing_is_the_same_refusal(self) -> None:
        studio = FakeStudioApi(listing_status=500)

        with pytest.raises(RuntimeError, match=r"studio\.test:8100"):
            await knowledge_base_adapter.registered_knowledge_bases(studio)

    async def test_a_studio_that_stops_answering_mid_read_is_a_refusal_not_a_thinner_registry(self, tmp_path: Path) -> None:
        """A transport failure on a schema is the studio going away, not that knowledge base having no schema."""

        class _AnswersThenStops(FakeStudioApi):
            async def get(self, url: str) -> httpx.Response:
                if url != "/api/knowledge-bases":
                    msg = "[Errno 61] Connection refused"
                    raise httpx.ConnectError(msg)
                return await super().get(url)

        studio = _AnswersThenStops(rows=[registry_row("research", tmp_path / "Knowledge")])

        with pytest.raises(RuntimeError, match=r"studio\.test:8100"):
            await knowledge_base_adapter.registered_knowledge_bases(studio)

    async def test_an_empty_registry_is_no_targets_and_no_refusal(self) -> None:
        """A studio that answers with nothing registered is an ordinary deployment, not a fault."""
        assert await knowledge_base_adapter.registered_knowledge_bases(FakeStudioApi()) == ()


class TestPatienceWithAStudioThatIsNotAnsweringYet:
    """A boot that races the studio's own boot waits for it instead of failing."""

    async def test_a_studio_that_starts_answering_before_patience_runs_out_answers_the_registry(self, tmp_path: Path) -> None:
        studio = FakeStudioApi(
            rows=[registry_row("research", tmp_path / "Knowledge")], specs={"research": _spec("research")}, connection_failures=2
        )

        targets = await knowledge_base_adapter.registered_knowledge_bases(studio, patience=StudioPatience(attempts=5, every=0))

        assert [target.name for target in targets] == ["research"]
        assert studio.requested.count("/api/knowledge-bases") == 3, "two failures, then the try that finally answers"

    async def test_a_studio_that_never_answers_is_the_usual_refusal_after_every_attempt_is_spent(self) -> None:
        studio = unreachable_studio()

        with pytest.raises(RuntimeError, match=r"studio\.test:8100"):
            await knowledge_base_adapter.registered_knowledge_bases(studio, patience=StudioPatience(attempts=3, every=0))

        assert studio.requested == ["/api/knowledge-bases"] * 3

    async def test_a_wrong_answer_is_never_retried(self) -> None:
        """A failure status is the studio answering wrongly, not the studio being silent -- one try is enough to know."""
        studio = FakeStudioApi(listing_status=500)

        with pytest.raises(RuntimeError, match=r"studio\.test:8100"):
            await knowledge_base_adapter.registered_knowledge_bases(studio, patience=StudioPatience(attempts=5, every=0))

        assert studio.requested == ["/api/knowledge-bases"]


async def test_the_readable_knowledge_bases_are_the_registry_plus_this_repo_s_docs(tmp_path: Path) -> None:
    """The registry never lists this checkout's docs knowledge base, so the plugin set appends it."""
    studio = FakeStudioApi(rows=[registry_row("research", tmp_path / "Knowledge")], specs={"research": _spec("research")})

    targets = await knowledge_base_adapter._readable_knowledge_bases(studio)

    assert [target.name for target in targets] == ["research", "repo-docs"]
