r"""Unit tests for the `studio source-queue` CLI subcommand group (`src/studio/knowledge_base/cli_source_queue.py`).

`source-queue add`, `list` and `retry` are filesystem-only (no DB, no container, mirroring
`init scaffold`): they operate directly on `<knowledge_base>/_meta/source-queue.yaml` via `SourceQueue`. Driven
by :class:`typer.testing.CliRunner` against a plain tmp_path knowledge base root, so no wireup container is
ever built. `add` also reads the knowledge base's own `knowledge-base-schema.json`, which is what refuses a knowledge base
with no queue connector to resolve the row.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from engram.kb.contracts import CanonicalSection, KnowledgeBaseSchema, PageTypeSpec, SourcesSpec
from engram.kb.domain.schema import RetrievalSpec
from engram.sources.contracts import SourceQueue, SourceQueueStatus, claimants_for
from typer.testing import CliRunner

from studio.knowledge_base.cli import app

if TYPE_CHECKING:
    from pathlib import Path

runner = CliRunner()

WATCH_URL = "https://www.youtube.com/watch?v=abc123XYZ_-"
OTHER_WATCH_URL = "https://www.youtube.com/watch?v=zzz999ZZZ_-"
ARXIV_URL = "https://arxiv.org/abs/2401.01234"


def _queue(knowledge_base: Path) -> SourceQueue:
    return SourceQueue(knowledge_base / "_meta" / "source-queue.yaml", connectors=claimants_for(("youtube", "intake")))


def _spec(knowledge_base: Path, *connectors: str) -> Path:
    """Write a knowledge base schema at `knowledge_base` declaring `connectors` (none disables the source layer)."""
    spec = KnowledgeBaseSchema(
        slug="inbox",
        page_types=(PageTypeSpec(page_type="note", id_prefix="note.", canonical_sections=(CanonicalSection(slug="overview"),)),),
        sources=SourcesSpec(
            enabled=bool(connectors),
            connectors=tuple(connectors) or ("zotero",),
            key_field="zotero_key",
            digest_link_field="digest_of",
            hash_field="source_hash",
            digest_types={"paper": "note"},
        ),
        retrieval=RetrievalSpec(collection="inbox"),
    )
    (knowledge_base / "knowledge-base-schema.json").write_text(spec.model_dump_json(), encoding="utf-8")
    return knowledge_base


def _queue_knowledge_base(knowledge_base: Path) -> Path:
    """A knowledge base root whose schema declares the queue connector, the only shape `source-queue add` accepts."""
    return _spec(knowledge_base, "intake")


class TestSourceQueueAdd:
    def test_appends_a_row_and_reports_the_key(self, tmp_path: Path):
        _queue_knowledge_base(tmp_path)

        result = runner.invoke(app, ["source-queue", "add", WATCH_URL, "--knowledge-base", str(tmp_path)])

        assert result.exit_code == 0, result.output
        assert "yt-abc123XYZ_-" in result.output
        assert "requested" in result.output.lower()

    def test_writes_the_queue_file_under_meta(self, tmp_path: Path):
        _queue_knowledge_base(tmp_path)

        runner.invoke(app, ["source-queue", "add", WATCH_URL, "--knowledge-base", str(tmp_path)])

        entries = _queue(tmp_path).entries()
        assert [entry.key for entry in entries] == ["yt-abc123XYZ_-"]
        assert entries[0].status is SourceQueueStatus.REQUESTED

    def test_is_idempotent_on_the_row_key(self, tmp_path: Path):
        _queue_knowledge_base(tmp_path)

        runner.invoke(app, ["source-queue", "add", WATCH_URL, "--knowledge-base", str(tmp_path)])
        result = runner.invoke(app, ["source-queue", "add", WATCH_URL, "--knowledge-base", str(tmp_path)])

        assert result.exit_code == 0, result.output
        assert len(_queue(tmp_path).entries()) == 1

    def test_defaults_knowledge_base_to_the_current_directory(self, tmp_path: Path, monkeypatch):
        _queue_knowledge_base(tmp_path)
        monkeypatch.chdir(tmp_path)

        result = runner.invoke(app, ["source-queue", "add", WATCH_URL])

        assert result.exit_code == 0, result.output
        assert (tmp_path / "_meta" / "source-queue.yaml").is_file()

    def test_accepts_any_url_not_only_youtube(self, tmp_path: Path):
        _queue_knowledge_base(tmp_path)

        result = runner.invoke(app, ["source-queue", "add", ARXIV_URL, "--knowledge-base", str(tmp_path)])

        assert result.exit_code == 0, result.output
        assert "arxiv-2401.01234  [requested]" in result.output

    def test_records_found_via(self, tmp_path: Path):
        _queue_knowledge_base(tmp_path)

        runner.invoke(
            app, ["source-queue", "add", ARXIV_URL, "--found-via", "rss:https://blog.example.org/feed.xml", "--knowledge-base", str(tmp_path)]
        )

        entries = _queue(tmp_path).entries()
        assert entries[0].found_via == "rss:https://blog.example.org/feed.xml"

    def test_refuses_a_knowledge_base_whose_connectors_do_not_include_the_queue(self, tmp_path: Path):
        _spec(tmp_path, "zotero")

        result = runner.invoke(app, ["source-queue", "add", WATCH_URL, "--knowledge-base", str(tmp_path)])

        assert result.exit_code == 1
        assert "sources_without_queue" in result.output
        assert not (tmp_path / "_meta" / "source-queue.yaml").exists()

    def test_refuses_a_knowledge_base_with_no_schema_at_all(self, tmp_path: Path):
        result = runner.invoke(app, ["source-queue", "add", WATCH_URL, "--knowledge-base", str(tmp_path)])

        assert result.exit_code == 1
        assert "knowledge-base-schema.json" in result.output
        assert not (tmp_path / "_meta" / "source-queue.yaml").exists()


class TestSourceQueueList:
    def test_lists_no_rows_on_an_empty_queue(self, tmp_path: Path):
        result = runner.invoke(app, ["source-queue", "list", "--knowledge-base", str(tmp_path)])

        assert result.exit_code == 0, result.output
        assert "empty" in result.output.lower()

    def test_lists_every_row_with_its_status(self, tmp_path: Path):
        queue = _queue(tmp_path)
        first = queue.add(WATCH_URL)
        queue.add(OTHER_WATCH_URL)
        queue.mark(first.key, SourceQueueStatus.FAILED, reason="no-transcript")

        result = runner.invoke(app, ["source-queue", "list", "--knowledge-base", str(tmp_path)])

        assert result.exit_code == 0, result.output
        assert "yt-abc123XYZ_-" in result.output
        assert "yt-zzz999ZZZ_-" in result.output
        assert "failed" in result.output.lower()
        assert "no-transcript" in result.output

    def test_round_trips_an_added_row(self, tmp_path: Path):
        _queue_knowledge_base(tmp_path)
        runner.invoke(app, ["source-queue", "add", WATCH_URL, "--knowledge-base", str(tmp_path)])
        result = runner.invoke(app, ["source-queue", "list", "--knowledge-base", str(tmp_path)])

        assert result.exit_code == 0, result.output
        assert "yt-abc123XYZ_-" in result.output

    def test_an_unreadable_queue_fails_cleanly(self, tmp_path: Path):
        (tmp_path / "_meta").mkdir(parents=True, exist_ok=True)
        (tmp_path / "_meta" / "source-queue.yaml").write_text("source_type: [unclosed\n", encoding="utf-8")

        result = runner.invoke(app, ["source-queue", "list", "--knowledge-base", str(tmp_path)])

        assert result.exit_code == 1
        assert "source queue" in result.output.lower()


class TestSourceQueueRetry:
    def test_retries_a_failed_row(self, tmp_path: Path):
        queue = _queue(tmp_path)
        entry = queue.add(ARXIV_URL)
        queue.mark(entry.key, SourceQueueStatus.FAILED, reason="fetch timed out")

        result = runner.invoke(app, ["source-queue", "retry", entry.key, "--knowledge-base", str(tmp_path)])

        assert result.exit_code == 0, result.output
        assert "[requested]" in result.output

    def test_a_resolved_row_has_nothing_to_retry(self, tmp_path: Path):
        queue = _queue(tmp_path)
        entry = queue.add(ARXIV_URL)
        queue.mark(entry.key, SourceQueueStatus.RESOLVED)

        result = runner.invoke(app, ["source-queue", "retry", entry.key, "--knowledge-base", str(tmp_path)])

        assert result.exit_code == 0, result.output
        assert "nothing to retry" in result.output


class TestHelp:
    def test_the_help_exits_zero_and_names_the_subcommands(self):
        result = runner.invoke(app, ["source-queue", "--help"])

        assert result.exit_code == 0
        assert "add" in result.output
        assert "list" in result.output
        assert "retry" in result.output
