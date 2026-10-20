from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from core.settings import DocumentIntelligenceSettings
from engram.config import KnowledgeBaseSettings, SourceResources
from engram.sources.contracts import (
    Resolved,
    SourceQueue,
    SourceQueueEntry,
    SourceQueueStatus,
    SourceQueueUnreadableError,
    SourceType,
    claimants_for,
)

from engram_connectors.conversion.contracts import ArxivHtmlConverter, HybridConverter
from engram_connectors.source_queue.contracts import ArxivResolver, PaperResolver, PdfResolver, PostResolver, Resolver, SourceQueueConnector
from engram_connectors.testing.fakes.source_queue import FakeResolver

_NOW = datetime(2026, 8, 25, 9, 0, tzinfo=UTC)
ARXIV = "https://arxiv.org/abs/2401.01234"
ARTICLE = "https://blog.example.org/agents"


_PROVIDERS = claimants_for(("intake",))


def _provider(tmp_path: Path, **resolvers: Resolver) -> tuple[SourceQueueConnector, SourceQueue]:
    ledger = SourceQueue(tmp_path / "source-queue.yaml", connectors=_PROVIDERS)
    table = {SourceType(kind): resolver for kind, resolver in resolvers.items()}
    return SourceQueueConnector(ledger, table, tmp_path / "cache", clock=lambda: _NOW), ledger


async def test_a_requested_row_resolves_renders_a_cache_file_and_becomes_an_item(tmp_path: Path) -> None:
    resolved = Resolved(title="Agents that plan", markdown="# Agents\n\nbody", url=ARTICLE, converter="article/trafilatura")
    provider, ledger = _provider(tmp_path, article=FakeResolver(results={"url": resolved}))
    row = ledger.add(ARTICLE)

    items = await provider.list_items_with_pdf()

    assert len(items) == 1
    item = items[0]
    assert item.provider_key == row.key
    assert item.source_id == row.key
    assert item.item_type == "article"
    assert item.title == "Agents that plan"
    assert item.url == ARTICLE
    assert item.pdf_path.read_text(encoding="utf-8") == "# Agents\n\nbody"
    assert ledger.get(row.key) is not None
    assert ledger.get(row.key).status is SourceQueueStatus.RESOLVED  # type: ignore[union-attr]


async def test_a_resolved_row_is_served_from_its_cache_without_resolving_again(tmp_path: Path) -> None:
    resolved = Resolved(title="A paper", markdown="text", url=ARXIV, published=date(2024, 1, 3), converter="arxiv-html/pandoc")
    resolver = FakeResolver(results={"arxiv": resolved})
    provider, ledger = _provider(tmp_path, paper=resolver)
    ledger.add(ARXIV)
    first = await provider.list_items_with_pdf()
    second = await provider.list_items_with_pdf()

    assert resolver.calls == ["arxiv-2401.01234"]
    assert first[0].pdf_path == second[0].pdf_path
    assert second[0].year == 2024


async def test_a_failing_resolve_marks_the_row_failed_with_the_reason_and_yields_no_item(tmp_path: Path) -> None:
    provider, ledger = _provider(tmp_path, paper=FakeResolver(errors={"arxiv": "no HTML rendering for this id"}))
    row = ledger.add(ARXIV)

    items = await provider.list_items_with_pdf()

    assert items == []
    failed = ledger.get(row.key)
    assert failed is not None
    assert failed.status is SourceQueueStatus.FAILED
    assert failed.reason == "no HTML rendering for this id"


class _MissingFileResolver:
    """A resolver whose converter reaches for a PDF that was never downloaded."""

    async def resolve(self, entry: SourceQueueEntry) -> Resolved:
        msg = f"[Errno 2] No such file or directory: '{entry.key}.pdf'"
        raise FileNotFoundError(msg)


async def test_a_resolver_that_cannot_read_its_file_fails_only_that_row(tmp_path: Path) -> None:
    """One row's missing file is that row's failure, not the whole provider's: the rows after it still serve."""
    resolved = Resolved(title="Agents that plan", markdown="body", url=ARTICLE, converter="article/trafilatura")
    provider, ledger = _provider(tmp_path, paper=_MissingFileResolver(), article=FakeResolver(results={"url": resolved}))
    bad = ledger.add(ARXIV)
    ledger.add(ARTICLE)

    items = await provider.list_items_with_pdf()

    assert [item.title for item in items] == ["Agents that plan"]
    failed = ledger.get(bad.key)
    assert failed is not None
    assert failed.status is SourceQueueStatus.FAILED
    assert failed.reason == "[Errno 2] No such file or directory: 'arxiv-2401.01234.pdf'"


async def test_a_kind_with_no_resolver_is_skipped_with_a_reason(tmp_path: Path) -> None:
    provider, ledger = _provider(tmp_path)
    row = ledger.add("https://x.com/someone/status/1234567890")

    assert await provider.list_items_with_pdf() == []
    skipped = ledger.get(row.key)
    assert skipped is not None
    assert skipped.status is SourceQueueStatus.SKIPPED
    assert skipped.reason == "no resolver for post"


async def test_get_answers_one_key_and_none_for_a_failed_or_unknown_one(tmp_path: Path) -> None:
    resolved = Resolved(title="Agents that plan", markdown="body", url=ARTICLE, converter="article/trafilatura")
    provider, ledger = _provider(tmp_path, article=FakeResolver(results={"url": resolved}))
    row = ledger.add(ARTICLE)
    assert (await provider.get(row.key)) is not None
    ledger.mark(row.key, SourceQueueStatus.FAILED, reason="gone")
    assert await provider.get(row.key) is None
    assert await provider.get("missing") is None


async def test_a_malformed_ledger_raises_one_readable_error_naming_the_problem(tmp_path: Path) -> None:
    provider, _ = _provider(tmp_path)
    (tmp_path / "source-queue.yaml").write_text(
        "- key: yt-abc\n  url: https://x\n  kind: not-a-kind\n  status: requested\n", encoding="utf-8"
    )

    with pytest.raises(SourceQueueUnreadableError, match="could not be read"):
        await provider.list_items_with_pdf()


async def test_a_truncated_cache_file_is_a_miss_and_the_row_resolves_again(tmp_path: Path) -> None:
    resolved = Resolved(title="A paper", markdown="fresh text", url=ARXIV, converter="arxiv-html/pandoc")
    resolver = FakeResolver(results={"arxiv": resolved})
    provider, ledger = _provider(tmp_path, paper=resolver)
    row = ledger.add(ARXIV)
    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / f"{row.key}.md").write_text("stale text", encoding="utf-8")
    (cache / f"{row.key}.meta.json").write_text('{"title": "A paper", "url": ', encoding="utf-8")

    items = await provider.list_items_with_pdf()

    assert resolver.calls == ["arxiv-2401.01234"]
    assert items[0].pdf_path.read_text(encoding="utf-8") == "fresh text"


async def test_a_cache_file_missing_a_required_field_is_a_miss_and_the_row_resolves_again(tmp_path: Path) -> None:
    resolved = Resolved(title="A paper", markdown="fresh text", url=ARXIV, converter="arxiv-html/pandoc")
    resolver = FakeResolver(results={"arxiv": resolved})
    provider, ledger = _provider(tmp_path, paper=resolver)
    row = ledger.add(ARXIV)
    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / f"{row.key}.md").write_text("stale text", encoding="utf-8")
    (cache / f"{row.key}.meta.json").write_text('{"title": "A paper"}', encoding="utf-8")

    items = await provider.list_items_with_pdf()

    assert resolver.calls == ["arxiv-2401.01234"]
    assert items[0].pdf_path.read_text(encoding="utf-8") == "fresh text"


async def test_a_requested_row_with_a_cache_file_is_resolved_without_a_resolver(tmp_path: Path) -> None:
    provider, ledger = _provider(tmp_path)
    row = ledger.add("https://x.com/someone/status/1234567890")
    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / f"{row.key}.md").write_text("# a post\n\nbody", encoding="utf-8")
    meta = Resolved(title="a post", markdown="", url=row.url, converter="x-post")
    (cache / f"{row.key}.meta.json").write_text(meta.model_dump_json(exclude={"markdown"}), encoding="utf-8")

    items = await provider.list_items_with_pdf()

    assert len(items) == 1
    assert items[0].item_type == "post"
    assert ledger.get(row.key).status is SourceQueueStatus.RESOLVED  # type: ignore[union-attr]


async def test_a_seen_row_is_never_served_or_resolved(tmp_path: Path) -> None:
    resolved = Resolved(title="A paper", markdown="text", url=ARXIV, converter="arxiv-html/pandoc")
    resolver = FakeResolver(results={"arxiv": resolved})
    provider, ledger = _provider(tmp_path, paper=resolver)
    ledger.add_seen(ARXIV, found_via="url-abc", reason="beyond hop depth 1")

    assert await provider.list_items_with_pdf() == []
    assert await provider.get("arxiv-2401.01234") is None
    assert resolver.calls == []


async def test_a_removed_row_is_never_served_or_resolved(tmp_path: Path) -> None:
    resolved = Resolved(title="A paper", markdown="text", url=ARXIV, converter="arxiv-html/pandoc")
    resolver = FakeResolver(results={"arxiv": resolved})
    provider, ledger = _provider(tmp_path, paper=resolver)
    ledger.remove(ARXIV, reason="removed 2026-08-31: off-domain", found_at=_NOW)

    assert await provider.list_items_with_pdf() == []
    assert await provider.get("arxiv-2401.01234") is None
    assert resolver.calls == []


async def test_a_crawl_found_row_fetching_an_already_mirrored_title_is_skipped_as_a_duplicate(tmp_path: Path) -> None:
    """Two URLs, one work (a moved repository): the second fetch answers the same title and never lands a second mirror."""
    resolved = Resolved(
        title="GitHub - mll-lab-nu/RAGEN",
        markdown="# RAGEN\n\nbody",
        url="https://github.com/RAGEN-AI/RAGEN",
        converter="article/trafilatura",
    )
    provider, ledger = _provider(tmp_path, article=FakeResolver(results={"url": resolved}))
    first = ledger.add("https://github.com/RAGEN-AI/RAGEN")
    second, _added = ledger.add_once("https://github.com/ZihanWang314/RAGEN", found_via="arxiv-2504.20073", found_at=_NOW)

    items = await provider.list_items_with_pdf()

    assert [item.provider_key for item in items] == [first.key], "one mirror lands, not two"
    dup = ledger.get(second.key)
    assert dup is not None
    assert dup.status is SourceQueueStatus.SKIPPED
    assert dup.reason is not None
    assert f"duplicate of {first.key}" in dup.reason


async def test_an_owner_added_row_is_never_second_guessed_by_the_duplicate_check(tmp_path: Path) -> None:
    resolved = Resolved(
        title="GitHub - mll-lab-nu/RAGEN",
        markdown="# RAGEN\n\nbody",
        url="https://github.com/RAGEN-AI/RAGEN",
        converter="article/trafilatura",
    )
    provider, ledger = _provider(tmp_path, article=FakeResolver(results={"url": resolved}))
    ledger.add("https://github.com/RAGEN-AI/RAGEN")
    ledger.add("https://github.com/ZihanWang314/RAGEN")

    items = await provider.list_items_with_pdf()

    assert len(items) == 2, "what the owner queued by hand fetches regardless"


class TestClaims:
    """`SourceQueueConnector.claims` — the ledger's own URL recognition, the catch-all when nothing more specific claims a URL first."""

    def test_a_youtube_url_claims_video(self) -> None:
        assert SourceQueueConnector.claims("https://www.youtube.com/watch?v=abc123XYZ_-") is SourceType.VIDEO

    def test_an_arxiv_url_claims_paper(self) -> None:
        assert SourceQueueConnector.claims(ARXIV) is SourceType.PAPER

    def test_a_bare_pdf_url_claims_paper(self) -> None:
        assert SourceQueueConnector.claims("https://example.org/whitepaper.pdf") is SourceType.PAPER

    def test_an_x_status_url_claims_post(self) -> None:
        assert SourceQueueConnector.claims("https://x.com/someone/status/12345") is SourceType.POST

    def test_anything_else_claims_article(self) -> None:
        assert SourceQueueConnector.claims(ARTICLE) is SourceType.ARTICLE


class TestKeyFor:
    """`SourceQueueConnector.key_for` — `yt-`/`arxiv-`/`x-` prefixed keys where a scheme applies, else the generic hash key."""

    def test_a_youtube_url_keys_by_its_video_id(self) -> None:
        assert SourceQueueConnector.key_for("https://www.youtube.com/watch?v=abc123XYZ_-") == "yt-abc123XYZ_-"

    def test_an_arxiv_url_keys_by_its_paper_id_without_the_version(self) -> None:
        assert SourceQueueConnector.key_for("https://arxiv.org/pdf/2401.01234v2") == "arxiv-2401.01234"

    def test_an_x_status_url_keys_by_its_status_id(self) -> None:
        assert SourceQueueConnector.key_for("https://x.com/someone/status/1234567890") == "x-1234567890"

    def test_a_twitter_com_status_url_keys_by_its_status_id_too(self) -> None:
        assert SourceQueueConnector.key_for("https://twitter.com/someone/status/1234567890") == "x-1234567890"

    def test_a_mobile_twitter_com_status_url_keys_by_its_status_id_too(self) -> None:
        assert SourceQueueConnector.key_for("https://mobile.twitter.com/someone/status/1234567890") == "x-1234567890"

    def test_a_non_status_x_url_falls_back_to_the_generic_hash_key(self) -> None:
        key = SourceQueueConnector.key_for("https://x.com/someone")
        assert key.startswith("url-")
        assert len(key) == len("url-") + 16

    def test_anything_else_falls_back_to_the_generic_hash_key(self) -> None:
        key = SourceQueueConnector.key_for(ARTICLE)
        assert key.startswith("url-")
        assert len(key) == len("url-") + 16


class TestMediumOf:
    """`SourceQueueConnector.source_type_of` — this ledger's own vocabulary IS the medium, verbatim."""

    @pytest.mark.parametrize("item_type", ["video", "paper", "post", "article"])
    def test_every_kind_value_folds_to_itself(self, item_type: str) -> None:
        assert SourceQueueConnector.source_type_of(item_type) is SourceType(item_type)

    def test_an_unknown_item_type_raises(self) -> None:
        with pytest.raises(ValueError, match="talk"):
            SourceQueueConnector.source_type_of("talk")


def _corpus(tmp_path: Path) -> KnowledgeBaseSettings:
    (tmp_path / "knowledge-base-schema.json").write_text("{}", encoding="utf-8")
    (tmp_path / "_meta").mkdir()
    return KnowledgeBaseSettings(path=tmp_path)


class TestBuild:
    """`SourceQueueConnector.build` — the Document Intelligence endpoint and the `xai` settings block are optional features."""

    def test_with_neither_resource_the_table_carries_no_post_and_the_paper_resolver_is_html_only(self, tmp_path: Path) -> None:
        provider = SourceQueueConnector.build(SourceResources(knowledge_base=_corpus(tmp_path)))

        assert isinstance(provider, SourceQueueConnector)
        resolvers = provider._resolvers
        assert set(resolvers) == {SourceType.VIDEO, SourceType.PAPER, SourceType.ARTICLE}
        paper = resolvers[SourceType.PAPER]
        assert isinstance(paper, PaperResolver)
        assert isinstance(paper._arxiv, ArxivResolver)
        assert isinstance(paper._arxiv._converter, ArxivHtmlConverter)
        assert isinstance(paper._pdf, PdfResolver)
        assert isinstance(paper._pdf._converter, ArxivHtmlConverter)

    def test_with_both_resources_the_table_carries_post_and_the_paper_resolver_falls_back_to_azure(self, tmp_path: Path) -> None:
        resources = SourceResources(
            knowledge_base=_corpus(tmp_path),
            document_intelligence=DocumentIntelligenceSettings(endpoint="https://di.example.org"),
            settings={"xai": {"api_key": "secret"}},
        )

        provider = SourceQueueConnector.build(resources)

        assert isinstance(provider, SourceQueueConnector)
        resolvers = provider._resolvers
        assert set(resolvers) == {SourceType.VIDEO, SourceType.PAPER, SourceType.ARTICLE, SourceType.POST}
        assert isinstance(resolvers[SourceType.POST], PostResolver)
        paper = resolvers[SourceType.PAPER]
        assert isinstance(paper, PaperResolver)
        assert isinstance(paper._arxiv, ArxivResolver)
        assert isinstance(paper._arxiv._converter, HybridConverter)
        assert isinstance(paper._pdf, PdfResolver)
        assert isinstance(paper._pdf._converter, HybridConverter)
        assert paper._arxiv._converter is paper._pdf._converter


async def test_get_on_a_malformed_ledger_raises_the_same_readable_error(tmp_path: Path) -> None:
    provider, _ = _provider(tmp_path)
    (tmp_path / "source-queue.yaml").write_text(
        "- key: yt-abc\n  url: https://x\n  kind: not-a-kind\n  status: requested\n", encoding="utf-8"
    )

    with pytest.raises(SourceQueueUnreadableError, match="could not be read"):
        await provider.get("yt-abc")
