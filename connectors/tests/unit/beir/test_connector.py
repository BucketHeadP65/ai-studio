"""The BEIR connector: a benchmark dataset's documents as source items, and its relevance judgments as a neutral file."""

from __future__ import annotations

import json
import zipfile

from typing import TYPE_CHECKING

import pytest

from core.settings import DocumentIntelligenceSettings
from engram.config import KnowledgeBaseSettings, SourceResources
from engram.sources.contracts import SourceType

from engram_connectors.beir.contracts import BeirConnector, BeirSettingsError
from engram_connectors.conversion.contracts import beir_converter_factory

if TYPE_CHECKING:
    from pathlib import Path

_CORPUS = [
    {"_id": "d1", "title": "Insulin and glucose", "text": "Insulin lowers blood glucose."},
    {"_id": "d2", "title": "Shared title", "text": "Exercise and glucose."},
    {"_id": "d3", "title": "", "text": "No title here."},
    {"_id": "d4", "title": "Shared title", "text": "A second document under the same title."},
]
_QUERIES = [{"_id": "q1", "text": "What lowers blood glucose?"}, {"_id": "q2", "text": "Does exercise matter?"}]
_QRELS = "query-id\tcorpus-id\tscore\nq1\td1\t2\nq1\td2\t0\nq2\td2\t1\n"


def _tiny_zip(tmp_path: Path) -> bytes:
    path = tmp_path / "tiny.zip"
    with zipfile.ZipFile(path, "w") as bundle:
        bundle.writestr("tiny/corpus.jsonl", "".join(json.dumps(line) + "\n" for line in _CORPUS))
        bundle.writestr("tiny/queries.jsonl", "".join(json.dumps(line) + "\n" for line in _QUERIES))
        bundle.writestr("tiny/qrels/test.tsv", _QRELS)
    return path.read_bytes()


class _CountingFetcher:
    """Answers every download with one archive and counts the calls."""

    def __init__(self, archive: bytes) -> None:
        self.archive = archive
        self.urls: list[str] = []

    async def fetch_bytes(self, url: str, *, max_bytes: int) -> bytes:
        del max_bytes
        self.urls.append(url)
        return self.archive


def _connector(tmp_path: Path, fetcher: _CountingFetcher) -> BeirConnector:
    return BeirConnector(dataset="tiny", split="test", cache_dir=tmp_path / "_meta" / "beir-cache", fetcher=fetcher)


async def test_each_document_becomes_one_item_keyed_by_its_id(tmp_path: Path) -> None:
    items = await _connector(tmp_path, _CountingFetcher(_tiny_zip(tmp_path))).list_items_with_pdf()

    assert sorted(item.provider_key for item in items) == ["d1", "d2", "d3", "d4"]
    assert all(item.source_id == item.provider_key for item in items)
    assert all(item.item_type == SourceType.ARTICLE.value for item in items)
    assert {item.provider_key: item.title for item in items}["d3"] == "d3", "an empty title falls back to the id"


async def test_an_item_s_file_holds_its_title_and_text(tmp_path: Path) -> None:
    items = await _connector(tmp_path, _CountingFetcher(_tiny_zip(tmp_path))).list_items_with_pdf()

    first = next(item for item in items if item.provider_key == "d1")
    assert first.pdf_path.read_text(encoding="utf-8") == "# Insulin and glucose\n\nInsulin lowers blood glucose.\n"


async def test_a_second_listing_downloads_nothing_and_rewrites_no_file(tmp_path: Path) -> None:
    fetcher = _CountingFetcher(_tiny_zip(tmp_path))
    connector = _connector(tmp_path, fetcher)
    first = await connector.list_items_with_pdf()
    stamps = {item.provider_key: item.pdf_path.stat().st_mtime_ns for item in first}

    second = await connector.list_items_with_pdf()

    assert len(fetcher.urls) == 1
    assert {item.provider_key: item.pdf_path.stat().st_mtime_ns for item in second} == stamps
    assert [item.date_modified for item in second] == [item.date_modified for item in first]


async def test_the_judgments_file_holds_every_judged_query(tmp_path: Path) -> None:
    await _connector(tmp_path, _CountingFetcher(_tiny_zip(tmp_path))).list_items_with_pdf()

    path = tmp_path / "_meta" / "beir-cache" / "tiny" / "judgments.test.jsonl"
    lines = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert lines == [
        {"query_id": "q1", "question": "What lowers blood glucose?", "judgments": {"d1": 2, "d2": 0}},
        {"query_id": "q2", "question": "Does exercise matter?", "judgments": {"d2": 1}},
    ]


async def test_get_answers_one_document(tmp_path: Path) -> None:
    connector = _connector(tmp_path, _CountingFetcher(_tiny_zip(tmp_path)))

    item = await connector.get("d2")
    missing = await connector.get("nope")

    assert item is not None
    assert item.title == "Shared title"
    assert missing is None


def test_build_reads_the_dataset_from_the_knowledge_base_folder(tmp_path: Path) -> None:
    (tmp_path / "knowledge-base-schema.json").write_text("{}", encoding="utf-8")
    (tmp_path / "_meta").mkdir()
    (tmp_path / "_meta" / "beir.yaml").write_text("dataset: scifact\nsplit: test\n", encoding="utf-8")

    connector = BeirConnector.build(SourceResources(knowledge_base=KnowledgeBaseSettings(path=tmp_path)))

    assert isinstance(connector, BeirConnector)
    assert connector.dataset == "scifact"


def test_a_missing_settings_file_is_refused_by_name(tmp_path: Path) -> None:
    (tmp_path / "knowledge-base-schema.json").write_text("{}", encoding="utf-8")
    (tmp_path / "_meta").mkdir()

    with pytest.raises(BeirSettingsError, match=r"_meta/beir\.yaml"):
        BeirConnector.build(SourceResources(knowledge_base=KnowledgeBaseSettings(path=tmp_path)))


def test_the_connector_claims_no_url() -> None:
    assert BeirConnector.claims("https://example.test/paper.pdf") is None
    assert BeirConnector.source_type_of("anything") is SourceType.ARTICLE


async def test_the_converter_passes_the_cached_file_through(tmp_path: Path) -> None:
    path = tmp_path / "d1.md"
    path.write_text("# T\n\nBody.\n", encoding="utf-8")

    result = await beir_converter_factory(DocumentIntelligenceSettings()).to_markdown(path)

    assert result.markdown == "# T\n\nBody.\n"
    assert result.converter == "beir"


async def test_two_ids_that_differ_only_by_case_get_their_own_files(tmp_path: Path) -> None:
    path = tmp_path / "cased.zip"
    with zipfile.ZipFile(path, "w") as bundle:
        corpus = [{"_id": "Doc", "title": "Upper", "text": "Upper text."}, {"_id": "doc", "title": "Lower", "text": "Lower text."}]
        bundle.writestr("tiny/corpus.jsonl", "".join(json.dumps(line) + "\n" for line in corpus))
        bundle.writestr("tiny/queries.jsonl", "")
        bundle.writestr("tiny/qrels/test.tsv", "query-id\tcorpus-id\tscore\n")

    items = await _connector(tmp_path, _CountingFetcher(path.read_bytes())).list_items_with_pdf()

    by_id = {item.provider_key: item for item in items}
    assert by_id["Doc"].pdf_path.name.lower() != by_id["doc"].pdf_path.name.lower()
    assert "Upper text." in by_id["Doc"].pdf_path.read_text(encoding="utf-8")
    assert "Lower text." in by_id["doc"].pdf_path.read_text(encoding="utf-8")


async def test_the_preview_lists_the_same_items_with_nothing_awaiting_resolution(tmp_path: Path) -> None:
    connector = _connector(tmp_path, _CountingFetcher(_tiny_zip(tmp_path)))

    preview = await connector.preview_items()

    assert list(preview.items) == await connector.list_items_with_pdf()
    assert preview.would_resolve == ()
