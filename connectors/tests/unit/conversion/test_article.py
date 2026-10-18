from pathlib import Path

import httpx
import pytest

from core.net import FetchError
from engram.sources.contracts import ConversionError

from engram_connectors.conversion.contracts import ArticleConverter
from engram_connectors.conversion.infrastructure.article import extract_article

HTML = """<html><head><title>Agents that plan</title></head><body>
<nav>Home About</nav>
<article><h1>Agents that plan</h1><p>Planning agents keep a task list the model owns.</p>
<p>The list is re-injected every turn so it survives compaction.</p></article>
<footer>Copyright</footer></body></html>"""


class FakePageFetcher:
    def __init__(self, pages: dict[str, str], *, fails: dict[str, httpx.HTTPError | FetchError] | None = None) -> None:
        self.pages = pages
        self.fails = fails or {}
        self.calls: list[str] = []

    async def fetch(self, url: str) -> str:
        self.calls.append(url)
        if (failure := self.fails.get(url)) is not None:
            raise failure
        return self.pages[url]


def test_extract_keeps_the_article_and_drops_the_chrome() -> None:
    text = extract_article(HTML, url="https://blog.example.org/agents")
    assert text is not None
    assert "Planning agents keep a task list" in text
    assert "Copyright" not in text
    assert "Home About" not in text


async def test_converter_fetches_the_url_and_labels_the_result(tmp_path: Path) -> None:
    fetcher = FakePageFetcher({"https://blog.example.org/agents": HTML})
    result = await ArticleConverter(fetcher).to_markdown(tmp_path / "unused.md", source_url="https://blog.example.org/agents")
    assert result.converter == "article/trafilatura"
    assert "task list" in result.markdown
    assert fetcher.calls == ["https://blog.example.org/agents"]


async def test_the_result_carries_the_page_title(tmp_path: Path) -> None:
    result = await ArticleConverter(FakePageFetcher({"https://blog.example.org/agents": HTML})).to_markdown(
        tmp_path / "unused.md", source_url="https://blog.example.org/agents"
    )
    assert result.title == "Agents that plan"


async def test_the_page_title_prefers_og_title_over_the_title_tag(tmp_path: Path) -> None:
    html = (
        "<html><head><title>spacy 3.7.2 requires typer, not found</title>"
        '<meta property="og:title" content="spaCy - Industrial-strength NLP"></head>'
        "<body><article><p>Some content that is long enough to extract.</p></article></body></html>"
    )
    fetcher = FakePageFetcher({"https://blog.example.org/spacy": html})
    result = await ArticleConverter(fetcher).to_markdown(tmp_path / "unused.md", source_url="https://blog.example.org/spacy")
    assert result.title == "spaCy - Industrial-strength NLP"


async def test_the_page_title_falls_back_to_the_title_tag_when_there_is_no_og_title(tmp_path: Path) -> None:
    html = "<html><head><title>  Agents  that\nplan  </title></head><body><article><p>Some content that is long enough.</p></article></body></html>"
    fetcher = FakePageFetcher({"https://blog.example.org/agents-2": html})
    result = await ArticleConverter(fetcher).to_markdown(tmp_path / "unused.md", source_url="https://blog.example.org/agents-2")
    assert result.title == "Agents that plan"


async def test_the_page_title_is_none_when_the_page_carries_neither(tmp_path: Path) -> None:
    html = "<html><body><article><p>Some content that is long enough to extract as an article.</p></article></body></html>"
    fetcher = FakePageFetcher({"https://blog.example.org/bare": html})
    result = await ArticleConverter(fetcher).to_markdown(tmp_path / "unused.md", source_url="https://blog.example.org/bare")
    assert result.title is None


async def test_converter_refuses_without_a_url(tmp_path: Path) -> None:
    with pytest.raises(ConversionError):
        await ArticleConverter(FakePageFetcher({})).to_markdown(tmp_path / "unused.md")


async def test_converter_refuses_a_page_with_nothing_to_extract(tmp_path: Path) -> None:
    fetcher = FakePageFetcher({"https://blog.example.org/empty": "<html><body></body></html>"})
    with pytest.raises(ConversionError):
        await ArticleConverter(fetcher).to_markdown(tmp_path / "unused.md", source_url="https://blog.example.org/empty")


async def test_a_failed_fetch_becomes_a_conversion_error_naming_the_url(tmp_path: Path) -> None:
    url = "https://blog.example.org/gone"
    fetcher = FakePageFetcher({}, fails={url: httpx.ConnectTimeout("timed out")})

    with pytest.raises(ConversionError, match=f"fetching {url} failed: timed out"):
        await ArticleConverter(fetcher).to_markdown(tmp_path / "unused.md", source_url=url)


async def test_an_ssrf_refusal_becomes_a_conversion_error_naming_the_url(tmp_path: Path) -> None:
    """`PageFetcher.fetch` refuses a URL that resolves to a private or link-local address by raising `FetchError`."""
    url = "http://169.254.169.254/latest/meta-data/"
    fetcher = FakePageFetcher({}, fails={url: FetchError(f"{url} resolves to a disallowed address")})

    with pytest.raises(ConversionError, match=f"fetching {url} failed:"):
        await ArticleConverter(fetcher).to_markdown(tmp_path / "unused.md", source_url=url)
