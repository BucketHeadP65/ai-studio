"""The MultiHop-RAG connector: the benchmark's articles as source items, and its questions as a judgments file."""

from __future__ import annotations

import hashlib
import json

from datetime import date
from typing import TYPE_CHECKING

import pytest

from core.settings import DocumentIntelligenceSettings
from engram.config import KnowledgeBaseSettings, SourceResources
from engram.sources.contracts import SourceType

from engram_connectors.conversion.contracts import multihop_rag_converter_factory
from engram_connectors.multihop_rag.contracts import DatasetHashes, MultihopRagConnector, MultihopRagDatasetError

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

_REVISION_URL = "https://huggingface.co/datasets/yixuantt/MultiHopRAG/resolve/71ac0d0bd1f951d2d6b70311f7d2ae404e1ffa82"
_ALPHA = {
    "title": "Alpha launches a phone",
    "author": "Ada Writer",
    "source": "Example Daily",
    "published_at": "2023-10-02T09:30:00+00:00",
    "category": "technology",
    "url": "https://example.test/alpha",
    "body": "Alpha launched a phone on Monday.",
}
_BETA = {
    "title": "Beta wins the cup",
    "author": None,
    "source": "Example Sports",
    "published_at": "2023-11-05T18:00:00+00:00",
    "category": "sports",
    "url": "https://example.test/beta",
    "body": "Beta won the cup.\n",
}
_GAMMA = {
    "title": "Gamma reports earnings",
    "author": "",
    "source": "Example Business",
    "published_at": "2023-12-24T23:34:39+00:00",
    "category": "business",
    "url": "https://example.test/gamma",
    "body": "Gamma reported earnings.",
}


def _evidence(article: Mapping[str, str | None]) -> dict[str, str | None]:
    return {key: value for key, value in article.items() if key != "body"} | {"fact": "A fact."}


_QUESTIONS = [
    {
        "query": "Did Alpha launch before Beta won?",
        "answer": "Yes",
        "question_type": "temporal_query",
        "evidence_list": [_evidence(_ALPHA), _evidence(_BETA)],
    },
    {
        "query": "Which reported more, Alpha or Gamma?",
        "answer": "Gamma",
        "question_type": "comparison_query",
        "evidence_list": [_evidence(_ALPHA), _evidence(_GAMMA), _evidence(_GAMMA)],
    },
    {"query": "Who won the cup?", "answer": "Beta", "question_type": "inference_query", "evidence_list": [_evidence(_BETA)]},
    {"query": "Who won the 1950 cup?", "answer": "Insufficient information.", "question_type": "null_query", "evidence_list": []},
]
_ARTICLES_BYTES = json.dumps([_ALPHA, _BETA, _GAMMA]).encode()
_QUESTIONS_BYTES = json.dumps(_QUESTIONS).encode()


def _source_id(url: str) -> str:
    return "mhr-" + hashlib.sha256(url.encode()).hexdigest()[:12]


def _prefix(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:16]


class _FakeHub:
    """Answers each dataset url with the bytes of the file it names, and records every download."""

    def __init__(self, files: dict[str, bytes]) -> None:
        self.files = files
        self.urls: list[str] = []

    async def fetch_bytes(self, url: str, *, max_bytes: int) -> bytes:
        self.urls.append(url)
        data = self.files[url.rsplit("/", 1)[-1]]
        assert len(data) <= max_bytes
        return data


def _hub() -> _FakeHub:
    return _FakeHub({"corpus.json": _ARTICLES_BYTES, "MultiHopRAG.json": _QUESTIONS_BYTES})


def _connector(tmp_path: Path, hub: _FakeHub) -> MultihopRagConnector:
    hashes = DatasetHashes(articles=_prefix(_ARTICLES_BYTES), questions=_prefix(_QUESTIONS_BYTES))
    return MultihopRagConnector(cache_dir=tmp_path / "_meta" / "multihop-rag-cache", fetcher=hub, hashes=hashes)


async def test_the_first_listing_downloads_both_files_once_from_the_pinned_revision(tmp_path: Path) -> None:
    hub = _hub()

    items = await _connector(tmp_path, hub).list_items_with_pdf()

    assert hub.urls == [f"{_REVISION_URL}/corpus.json", f"{_REVISION_URL}/MultiHopRAG.json"]
    assert [item.title for item in items] == ["Alpha launches a phone", "Beta wins the cup", "Gamma reports earnings"]


async def test_a_file_whose_hash_does_not_match_is_refused_naming_it_and_the_next_listing_downloads_again(tmp_path: Path) -> None:
    hub = _FakeHub({"corpus.json": _ARTICLES_BYTES, "MultiHopRAG.json": _QUESTIONS_BYTES + b" "})
    connector = _connector(tmp_path, hub)

    with pytest.raises(MultihopRagDatasetError, match=r"MultiHopRAG\.json"):
        await connector.list_items_with_pdf()

    assert not (tmp_path / "_meta" / "multihop-rag-cache" / "raw").exists()
    assert not (tmp_path / "_meta" / "multihop-rag-cache" / ".raw.partial").exists()

    hub.files["MultiHopRAG.json"] = _QUESTIONS_BYTES
    items = await connector.list_items_with_pdf()

    assert len(items) == 3
    assert len(hub.urls) == 4


async def test_an_articles_file_whose_hash_does_not_match_is_refused_before_the_questions_are_fetched(tmp_path: Path) -> None:
    hub = _FakeHub({"corpus.json": _ARTICLES_BYTES + b" ", "MultiHopRAG.json": _QUESTIONS_BYTES})

    with pytest.raises(MultihopRagDatasetError, match=r"corpus\.json"):
        await _connector(tmp_path, hub).list_items_with_pdf()

    assert hub.urls == [f"{_REVISION_URL}/corpus.json"]
    assert not (tmp_path / "_meta" / "multihop-rag-cache" / "raw").exists()
    assert not (tmp_path / "_meta" / "multihop-rag-cache" / ".raw.partial").exists()


async def test_a_second_listing_downloads_nothing_and_rewrites_no_file(tmp_path: Path) -> None:
    hub = _hub()
    connector = _connector(tmp_path, hub)
    first = await connector.list_items_with_pdf()
    judgments = tmp_path / "_meta" / "multihop-rag-cache" / "judgments.jsonl"
    stamps = {item.provider_key: item.pdf_path.stat().st_mtime_ns for item in first}
    judgments_stamp = judgments.stat().st_mtime_ns

    second = await connector.list_items_with_pdf()

    assert len(hub.urls) == 2
    assert {item.provider_key: item.pdf_path.stat().st_mtime_ns for item in second} == stamps
    assert judgments.stat().st_mtime_ns == judgments_stamp
    assert [item.date_modified for item in second] == [item.date_modified for item in first]


async def test_each_item_carries_its_id_title_date_and_metadata(tmp_path: Path) -> None:
    items = await _connector(tmp_path, _hub()).list_items_with_pdf()

    alpha, beta, gamma = items
    assert alpha.source_id == alpha.provider_key == _source_id("https://example.test/alpha")
    assert alpha.item_type == SourceType.ARTICLE.value == "article"
    assert alpha.published == date(2023, 10, 2)
    assert alpha.url == "https://example.test/alpha"
    assert [item.creators for item in items] == [[], [], []], "the author travels in meta only"
    assert alpha.meta == {
        "outlet": "Example Daily",
        "author": "Ada Writer",
        "published_at": "2023-10-02T09:30:00+00:00",
        "published_on": "2023-10-02",
        "category": "technology",
        "url": "https://example.test/alpha",
    }
    assert beta.meta == {
        "outlet": "Example Sports",
        "published_at": "2023-11-05T18:00:00+00:00",
        "published_on": "2023-11-05",
        "category": "sports",
        "url": "https://example.test/beta",
    }, "an article with no author carries no author key"
    assert "author" not in gamma.meta, "a blank author counts as none"
    assert gamma.meta["published_on"] == "2023-12-24", "the date part of the published time, in the offset the dataset states"


async def test_the_judgments_file_maps_evidence_titles_to_source_ids(tmp_path: Path) -> None:
    await _connector(tmp_path, _hub()).list_items_with_pdf()

    path = tmp_path / "_meta" / "multihop-rag-cache" / "judgments.jsonl"
    lines = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    alpha, beta, gamma = (_source_id(article["url"] or "") for article in (_ALPHA, _BETA, _GAMMA))
    assert lines == [
        {
            "query_id": "q0001",
            "question": "Did Alpha launch before Beta won?",
            "judgments": {alpha: 1, beta: 1},
            "answer": "Yes",
            "query_type": "temporal_query",
        },
        {
            "query_id": "q0002",
            "question": "Which reported more, Alpha or Gamma?",
            "judgments": {alpha: 1, gamma: 1},
            "answer": "Gamma",
            "query_type": "comparison_query",
        },
        {"query_id": "q0003", "question": "Who won the cup?", "judgments": {beta: 1}, "answer": "Beta", "query_type": "inference_query"},
        {
            "query_id": "q0004",
            "question": "Who won the 1950 cup?",
            "judgments": {},
            "answer": "Insufficient information.",
            "query_type": "null_query",
        },
    ]


async def test_an_evidence_title_missing_from_the_articles_is_refused_naming_it(tmp_path: Path) -> None:
    question = {
        "query": "Where?",
        "answer": "Here",
        "question_type": "inference_query",
        "evidence_list": [_evidence(_ALPHA) | {"title": "Ghost headline"}],
    }
    questions = json.dumps([question]).encode()
    hub = _FakeHub({"corpus.json": _ARTICLES_BYTES, "MultiHopRAG.json": questions})
    hashes = DatasetHashes(articles=_prefix(_ARTICLES_BYTES), questions=_prefix(questions))
    connector = MultihopRagConnector(cache_dir=tmp_path / "cache", fetcher=hub, hashes=hashes)

    with pytest.raises(MultihopRagDatasetError, match="Ghost headline"):
        await connector.list_items_with_pdf()


async def test_each_document_file_holds_the_title_as_a_heading_then_the_body(tmp_path: Path) -> None:
    items = await _connector(tmp_path, _hub()).list_items_with_pdf()

    alpha, beta, _ = items
    assert alpha.pdf_path.read_text(encoding="utf-8") == "# Alpha launches a phone\n\nAlpha launched a phone on Monday.\n"
    assert beta.pdf_path.read_text(encoding="utf-8") == "# Beta wins the cup\n\nBeta won the cup.\n"
    assert alpha.pdf_path.name == f"{alpha.source_id}.md"


async def test_get_answers_one_article(tmp_path: Path) -> None:
    connector = _connector(tmp_path, _hub())

    item = await connector.get(_source_id("https://example.test/beta"))
    missing = await connector.get("mhr-000000000000")

    assert item is not None
    assert item.title == "Beta wins the cup"
    assert missing is None


def test_build_caches_under_the_knowledge_base_meta_folder(tmp_path: Path) -> None:
    (tmp_path / "knowledge-base-schema.json").write_text("{}", encoding="utf-8")
    (tmp_path / "_meta").mkdir()

    connector = MultihopRagConnector.build(SourceResources(knowledge_base=KnowledgeBaseSettings(path=tmp_path)))

    assert isinstance(connector, MultihopRagConnector)
    assert connector.cache_dir == tmp_path / "_meta" / "multihop-rag-cache"


def test_the_connector_claims_no_url() -> None:
    assert MultihopRagConnector.claims("https://example.test/alpha") is None
    assert MultihopRagConnector.source_type_of("anything") is SourceType.ARTICLE


async def test_the_converter_passes_the_cached_file_through(tmp_path: Path) -> None:
    path = tmp_path / "mhr-0123456789ab.md"
    path.write_text("# T\n\nBody.\n", encoding="utf-8")

    result = await multihop_rag_converter_factory(DocumentIntelligenceSettings()).to_markdown(path)

    assert result.markdown == "# T\n\nBody.\n"
    assert result.converter == "multihop_rag"


async def test_the_preview_lists_the_same_items_with_nothing_awaiting_resolution(tmp_path: Path) -> None:
    connector = _connector(tmp_path, _hub())

    preview = await connector.preview_items()

    assert list(preview.items) == await connector.list_items_with_pdf()
    assert preview.would_resolve == ()
