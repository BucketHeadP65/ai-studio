import re

from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from engram.sources.contracts import ConversionError, ConversionResult, SourceQueueEntry, SourceQueueStatus, SourceType
from engram.testing.fakes.conversion import FakeDocumentConverter
from pydantic_ai import Agent, NativeOutput
from pydantic_ai.models.test import TestModel

from engram_connectors.source_queue.contracts import (
    ArticleResolver,
    ArxivResolver,
    PaperResolver,
    PdfResolver,
    PostResolver,
    ResolveError,
    VideoResolver,
)
from engram_connectors.source_queue.infrastructure.resolvers import PostAnswer, _post_agent
from engram_connectors.testing.factories.youtube import VideoTranscriptFactory
from engram_connectors.testing.fakes.youtube import FakeTranscriptFetcher
from engram_connectors.xai import XaiResource
from engram_connectors.youtube.contracts import FetchedVideo, TranscriptUnavailableError

_NOW = datetime(2026, 8, 25, tzinfo=UTC)


def _entry(url: str, kind: SourceType, key: str) -> SourceQueueEntry:
    return SourceQueueEntry(key=key, url=url, source_type=kind, status=SourceQueueStatus.REQUESTED, found_at=_NOW)


async def test_video_resolver_renders_the_segmented_transcript() -> None:
    doc = VideoTranscriptFactory.build(published=date(2026, 8, 1))
    fetcher = FakeTranscriptFetcher(videos={doc.video_id: FetchedVideo(doc=doc, url=f"https://www.youtube.com/watch?v={doc.video_id}")})
    resolved = await VideoResolver(fetcher).resolve(_entry(f"https://youtu.be/{doc.video_id}", SourceType.VIDEO, f"yt-{doc.video_id}"))
    assert resolved.title == doc.title
    assert resolved.channel == doc.channel
    assert resolved.published == date(2026, 8, 1)
    assert resolved.converter == "youtube-transcript"
    assert doc.segments[0].text in resolved.markdown


async def test_video_resolver_turns_a_missing_transcript_into_a_resolve_error() -> None:
    fetcher = FakeTranscriptFetcher(videos={}, errors={"abc123XYZ_-": TranscriptUnavailableError("no captions")})
    with pytest.raises(ResolveError, match="no captions"):
        await VideoResolver(fetcher).resolve(_entry("https://youtu.be/abc123XYZ_-", SourceType.VIDEO, "yt-abc123XYZ_-"))


async def test_arxiv_resolver_uses_the_html_converter_with_the_abs_url(tmp_path: Path) -> None:
    converter = FakeDocumentConverter(converter="arxiv-html/pandoc")
    resolver = ArxivResolver(converter, cache_dir=tmp_path)
    resolved = await resolver.resolve(_entry("https://arxiv.org/pdf/2401.01234v2", SourceType.PAPER, "arxiv-2401.01234"))
    assert resolved.converter == "arxiv-html/pandoc"
    assert resolved.url == "https://arxiv.org/abs/2401.01234"
    assert converter.call_count == 1


@dataclass
class _RecordingPdfFetch:
    """A `fetch_pdf` stand-in: answers scripted bytes (or nothing) and records the ids asked for."""

    answer: bytes | None
    asked: list[str] = field(default_factory=list)

    async def __call__(self, paper_id: str) -> bytes | None:
        self.asked.append(paper_id)
        return self.answer


async def test_arxiv_resolver_fetches_the_missing_pdf_into_the_cache(tmp_path: Path) -> None:
    fetch = _RecordingPdfFetch(answer=b"%PDF-1.7 fetched")
    resolver = ArxivResolver(FakeDocumentConverter(), cache_dir=tmp_path, fetch_pdf=fetch)

    await resolver.resolve(_entry("https://arxiv.org/abs/1606.05250", SourceType.PAPER, "arxiv-1606.05250"))

    assert fetch.asked == ["1606.05250"]
    assert (tmp_path / "arxiv-1606.05250.pdf").read_bytes() == b"%PDF-1.7 fetched"


async def test_arxiv_resolver_skips_the_fetch_when_the_pdf_is_on_disk(tmp_path: Path) -> None:
    (tmp_path / "arxiv-1606.05250.pdf").write_bytes(b"%PDF-1.7 already here")
    fetch = _RecordingPdfFetch(answer=b"%PDF-1.7 fetched")
    resolver = ArxivResolver(FakeDocumentConverter(), cache_dir=tmp_path, fetch_pdf=fetch)

    await resolver.resolve(_entry("https://arxiv.org/abs/1606.05250", SourceType.PAPER, "arxiv-1606.05250"))

    assert fetch.asked == []
    assert (tmp_path / "arxiv-1606.05250.pdf").read_bytes() == b"%PDF-1.7 already here"


async def test_arxiv_resolver_replaces_a_junk_heading_with_the_paper_id_title(tmp_path: Path) -> None:
    class BrokenRender:
        async def to_markdown(self, pdf_path: Path, *, source_url: str | None = None) -> ConversionResult:
            junk = "# ![](_assets/2309.10691/figs-mint-leaf-logo.jpg) MINT: Evaluating LLMs\n\nbody"
            return ConversionResult(markdown=junk, page_count=1, converter="arxiv-html/pandoc", assets={})

    resolved = await ArxivResolver(BrokenRender(), cache_dir=tmp_path).resolve(
        _entry("https://arxiv.org/abs/2309.10691", SourceType.PAPER, "arxiv-2309.10691")
    )

    assert resolved.title == "arXiv 2309.10691"


async def test_article_resolver_replaces_a_junk_title_with_the_first_heading_then_the_url(tmp_path: Path) -> None:
    class JunkTitle:
        async def to_markdown(self, pdf_path: Path, *, source_url: str | None = None) -> ConversionResult:
            return ConversionResult(
                markdown=f"junk opening line\n\n{_BODY}", page_count=1, converter="article/trafilatura", assets={}, title="[Image blocked: No description] rest"
            )

    resolved = await ArticleResolver(JunkTitle(), cache_dir=tmp_path).resolve(
        _entry("https://blog.example.org/agents", SourceType.ARTICLE, "url-1234")
    )

    assert resolved.title == "https://blog.example.org/agents"


async def test_arxiv_resolver_converts_on_even_when_the_fetch_answers_nothing(tmp_path: Path) -> None:
    converter = FakeDocumentConverter(converter="arxiv-html/pandoc")
    resolver = ArxivResolver(converter, cache_dir=tmp_path, fetch_pdf=_RecordingPdfFetch(answer=None))

    resolved = await resolver.resolve(_entry("https://arxiv.org/abs/1606.05250", SourceType.PAPER, "arxiv-1606.05250"))

    assert resolved.converter == "arxiv-html/pandoc"
    assert not (tmp_path / "arxiv-1606.05250.pdf").exists()
    assert converter.call_count == 1


_BODY = " ".join(["body"] * 60)


async def test_article_resolver_names_the_page_from_its_first_heading(tmp_path: Path) -> None:
    class FakeArticle:
        async def to_markdown(self, pdf_path: Path, *, source_url: str | None = None) -> ConversionResult:
            return ConversionResult(markdown=f"# Agents that plan\n\n{_BODY}", page_count=1, converter="article/trafilatura", assets={})

    resolved = await ArticleResolver(FakeArticle(), cache_dir=tmp_path).resolve(
        _entry("https://blog.example.org/agents", SourceType.ARTICLE, "url-0123456789abcdef")
    )
    assert resolved.title == "Agents that plan"
    assert resolved.markdown.startswith("# Agents that plan")


async def test_article_resolver_prefers_the_converters_own_title_over_the_heading(tmp_path: Path) -> None:
    class FakeArticle:
        async def to_markdown(self, pdf_path: Path, *, source_url: str | None = None) -> ConversionResult:
            return ConversionResult(
                markdown=f"# spacy 3.7.2 requires typer, not found\n\n{_BODY}",
                page_count=1,
                converter="article/trafilatura",
                assets={},
                title="spaCy - Industrial-strength NLP",
            )

    resolved = await ArticleResolver(FakeArticle(), cache_dir=tmp_path).resolve(
        _entry("https://blog.example.org/spacy", SourceType.ARTICLE, "url-0123456789abcdef")
    )
    assert resolved.title == "spaCy - Industrial-strength NLP"


async def test_article_resolver_refuses_a_bot_check_page_instead_of_mirroring_it(tmp_path: Path) -> None:
    """A site that answers its bot check instead of the page fails the row with the reason; the check page never becomes a mirror."""

    class BotCheck:
        async def to_markdown(self, pdf_path: Path, *, source_url: str | None = None) -> ConversionResult:
            wall = "Please wait while we verify your browser before letting you continue to the site. "
            return ConversionResult(markdown=f"# Verifying your browser - OpenReview\n\n{wall * 8}", page_count=1, converter="article/trafilatura", assets={})

    with pytest.raises(ResolveError, match="answered a bot-check page, not the article: 'Verifying your browser - OpenReview'"):
        await ArticleResolver(BotCheck(), cache_dir=tmp_path).resolve(_entry("https://openreview.net/forum?id=x", SourceType.ARTICLE, "url-0123456789abcdef"))


async def test_article_resolver_refuses_a_challenge_page_under_the_sites_own_title(tmp_path: Path) -> None:
    """A challenge page that keeps the site's title is still a bot check: its body asks the reader to pass a check."""

    class ChallengeUnderSiteTitle:
        async def to_markdown(self, pdf_path: Path, *, source_url: str | None = None) -> ConversionResult:
            body = (
                "Verifying your browser. Complete the check below to continue to OpenReview. Please complete the verification above. "
                "Have an OpenReview account? Sign in to skip this check. This process is automatic and your browser will redirect "
                "to the requested content shortly once the check is complete."
            )
            return ConversionResult(markdown=f"# OpenReview\n\n{body}", page_count=1, converter="article/trafilatura", assets={})

    with pytest.raises(ResolveError, match="answered a bot-check page, not the article: 'OpenReview'"):
        await ArticleResolver(ChallengeUnderSiteTitle(), cache_dir=tmp_path).resolve(
            _entry("https://openreview.net/forum?id=x", SourceType.ARTICLE, "url-0123456789abcdef")
        )


async def test_article_resolver_keeps_a_long_article_that_mentions_a_browser_check(tmp_path: Path) -> None:
    class LongArticle:
        async def to_markdown(self, pdf_path: Path, *, source_url: str | None = None) -> ConversionResult:
            prose = "Agents that browse the web meet pages that say complete the check below to continue, and they must stop there. " * 20
            return ConversionResult(markdown=f"# How agents meet bot checks\n\n{prose}", page_count=1, converter="article/trafilatura", assets={})

    resolved = await ArticleResolver(LongArticle(), cache_dir=tmp_path).resolve(
        _entry("https://blog.example.org/checks", SourceType.ARTICLE, "url-0123456789abcdef")
    )
    assert resolved.title == "How agents meet bot checks"


async def test_article_resolver_refuses_a_near_empty_page(tmp_path: Path) -> None:
    class Thin:
        async def to_markdown(self, pdf_path: Path, *, source_url: str | None = None) -> ConversionResult:
            return ConversionResult(markdown="# A page\n\nfive words only here now", page_count=1, converter="article/trafilatura", assets={})

    with pytest.raises(ResolveError, match="answered 7 words, too few to be the article"):
        await ArticleResolver(Thin(), cache_dir=tmp_path).resolve(_entry("https://blog.example.org/thin", SourceType.ARTICLE, "url-0123456789abcdef"))


async def test_a_conversion_error_is_a_resolve_error(tmp_path: Path) -> None:
    converter = FakeDocumentConverter(converter="arxiv-html/pandoc", error_on={tmp_path / "arxiv-2401.01234.pdf"}, exc=ConversionError("boom"))
    with pytest.raises(ResolveError):
        await ArxivResolver(converter, cache_dir=tmp_path).resolve(_entry("https://arxiv.org/abs/2401.01234", SourceType.PAPER, "arxiv-2401.01234"))


@dataclass
class _RecordingBytesFetch:
    """A `fetch` stand-in for `PdfResolver`: answers scripted bytes and records the URLs asked for."""

    answer: bytes
    asked: list[str] = field(default_factory=list)

    async def __call__(self, url: str) -> bytes:
        self.asked.append(url)
        return self.answer


_PDF_URL = "https://example.org/papers/whitepaper.pdf"


async def test_paper_resolver_sends_a_url_keyed_entry_to_the_pdf_route_which_fetches_its_own_url(tmp_path: Path) -> None:
    fetch = _RecordingBytesFetch(answer=b"%PDF-1.7 body")
    pdf_route = PdfResolver(FakeDocumentConverter(converter="azure-di"), cache_dir=tmp_path, fetch=fetch)
    paper = PaperResolver(ArxivResolver(FakeDocumentConverter(), cache_dir=tmp_path), pdf_route)

    resolved = await paper.resolve(_entry(_PDF_URL, SourceType.PAPER, "url-abc123"))

    assert fetch.asked == [_PDF_URL]
    assert resolved.url == _PDF_URL
    assert resolved.converter == "azure-di"
    assert (tmp_path / "url-abc123.pdf").read_bytes() == b"%PDF-1.7 body"


async def test_paper_resolver_sends_an_arxiv_keyed_entry_to_the_arxiv_route_unchanged(tmp_path: Path) -> None:
    fetch = _RecordingBytesFetch(answer=b"%PDF-1.7 body")
    arxiv_route = ArxivResolver(FakeDocumentConverter(converter="arxiv-html/pandoc"), cache_dir=tmp_path)
    paper = PaperResolver(arxiv_route, PdfResolver(FakeDocumentConverter(), cache_dir=tmp_path, fetch=fetch))

    resolved = await paper.resolve(_entry("https://arxiv.org/pdf/2401.01234v2", SourceType.PAPER, "arxiv-2401.01234"))

    assert fetch.asked == []
    assert resolved.url == "https://arxiv.org/abs/2401.01234"
    assert resolved.converter == "arxiv-html/pandoc"


async def test_pdf_resolver_refuses_a_url_the_ssrf_guard_refuses_and_never_fetches(tmp_path: Path) -> None:
    """No injected fetch here, the real guarded fetch runs, a loopback literal needs no DNS lookup."""
    resolver = PdfResolver(FakeDocumentConverter(), cache_dir=tmp_path)

    with pytest.raises(ResolveError, match="not a public address"):
        await resolver.resolve(_entry("http://127.0.0.1/internal.pdf", SourceType.PAPER, "url-internal"))

    assert not (tmp_path / "url-internal.pdf").exists()


async def test_pdf_resolver_turns_a_conversion_failure_into_a_resolve_error_naming_the_url(tmp_path: Path) -> None:
    fetch = _RecordingBytesFetch(answer=b"%PDF-1.7 body")
    converter = FakeDocumentConverter(error_on={tmp_path / "url-broken.pdf"}, exc=ConversionError("could not read this PDF"))
    resolver = PdfResolver(converter, cache_dir=tmp_path, fetch=fetch)

    with pytest.raises(ResolveError, match=re.escape(_PDF_URL)):
        await resolver.resolve(_entry(_PDF_URL, SourceType.PAPER, "url-broken"))


@dataclass
class _PostAgentFactory:
    """A scriptable `agent_factory`: records the resource it was called with, replays a scripted `PostAnswer` through `TestModel`."""

    answer: dict[str, object]
    seen: list[XaiResource] = field(default_factory=list)

    def __call__(self, resource: XaiResource) -> Agent[None, PostAnswer]:
        self.seen.append(resource)
        return Agent(TestModel(custom_output_args=self.answer), output_type=PostAnswer)


_RESOURCE = XaiResource(api_key="test-key", model="grok-4-fast-non-reasoning")


async def test_post_resolver_renders_the_post_like_the_feed_scanner_does() -> None:
    answer: dict[str, object] = {
        "found": True,
        "handle": "someone",
        "date": "2026-08-24T18:00:00Z",
        "text": "New paper on planning agents: https://arxiv.org/abs/2401.01234",
        "links": ["https://arxiv.org/abs/2401.01234"],
    }
    factory = _PostAgentFactory(answer)
    resolver = PostResolver(_RESOURCE, agent_factory=factory)

    resolved = await resolver.resolve(_entry("https://x.com/someone/status/1234567890", SourceType.POST, "x-1234567890"))

    assert resolved.converter == "x-post"
    assert resolved.channel == "someone"
    assert resolved.published == date(2026, 8, 24)
    assert resolved.markdown.startswith("# @someone, 2026-08-24T18:00:00+00:00")
    assert "New paper on planning agents" in resolved.markdown
    assert "- https://arxiv.org/abs/2401.01234" in resolved.markdown
    assert factory.seen == [_RESOURCE]


async def test_post_resolver_raises_when_the_agent_cannot_find_the_post() -> None:
    resolver = PostResolver(_RESOURCE, agent_factory=_PostAgentFactory({"found": False}))

    with pytest.raises(ResolveError, match="could not retrieve"):
        await resolver.resolve(_entry("https://x.com/someone/status/999", SourceType.POST, "x-999"))


async def test_post_resolver_turns_an_agent_failure_into_a_resolve_error() -> None:
    def factory(resource: XaiResource) -> Agent[None, PostAnswer]:
        del resource
        msg = "no xai api key configured"
        raise RuntimeError(msg)

    resolver = PostResolver(_RESOURCE, agent_factory=factory)

    with pytest.raises(ResolveError, match="no xai api key configured"):
        await resolver.resolve(_entry("https://x.com/someone/status/1", SourceType.POST, "x-1"))


def test_the_post_agent_answers_as_native_structured_output() -> None:
    assert isinstance(_post_agent(XaiResource(api_key="test")).output_type, NativeOutput)
