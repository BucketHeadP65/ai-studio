"""Which knowledge base a surface is acting on, and how far it has moved since its index, read off fakes."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from engram.identity import LastRun, changed_count, disk_content_hashes, read_identity, read_stamp
from engram.retrieval.domain import SqlResult
from engram.testing.fakes.gateway import FakeSqlGateway
from engram.testing.fakes.knowledge_base_reader import FakeKnowledgeBaseReader

COMPILED = datetime(2026, 8, 26, 23, 50, tzinfo=UTC)


def _gateway(
    *, revision: str | None = "0034", indexed_at: datetime | None = COMPILED, manifest: dict[str, str] | None = None
) -> FakeSqlGateway:
    present = revision is not None
    return FakeSqlGateway(
        routed={
            "to_regclass": SqlResult(rows=({"present": present},), columns=("present",)),
            "alembic_version": SqlResult(rows=({"version_num": revision},) if present else (), columns=("version_num",)),
            "index_fingerprint": SqlResult(
                rows=({"indexed_at": indexed_at},) if indexed_at is not None else (), columns=("indexed_at",)
            ),
            "index_manifest": SqlResult(
                rows=tuple({"path": path, "content_hash": digest} for path, digest in (manifest or {}).items()),
                columns=("path", "content_hash"),
            ),
        }
    )


async def _identity(gateway: FakeSqlGateway, *, reader: FakeKnowledgeBaseReader | None = None, last_run: LastRun | None = None):
    return await read_identity(
        gateway,
        on_disk=disk_content_hashes(reader if reader is not None else FakeKnowledgeBaseReader()),
        slug="repo-docs",
        root=Path("/x/engram/docs"),
        database="localhost:5432/engram",
        collection="repo-docs",
        code_revision="0034",
        last_run=last_run,
    )


class TestReadIdentity:
    async def test_a_never_compiled_knowledge_base_has_no_time_and_reads_never(self):
        identity = await _identity(_gateway(indexed_at=None))

        assert identity.last_indexed_at is None
        assert identity.last_index_outcome == "never"
        assert identity.last_index_reason == ""

    async def test_a_compiled_knowledge_base_with_no_run_record_reads_ok(self):
        identity = await _identity(_gateway())

        assert identity.last_indexed_at == COMPILED
        assert identity.last_index_outcome == "ok"

    async def test_a_failed_run_newer_than_the_fingerprint_reads_failed_with_its_reason(self):
        run = LastRun(finished_at=COMPILED.replace(hour=23, minute=59), outcome="failed", detail="connection refused")

        identity = await _identity(_gateway(), last_run=run)

        assert identity.last_index_outcome == "failed"
        assert identity.last_index_reason == "connection refused"

    async def test_a_failed_run_older_than_the_fingerprint_reads_ok(self):
        run = LastRun(finished_at=COMPILED.replace(hour=1), outcome="failed", detail="connection refused")

        identity = await _identity(_gateway(), last_run=run)

        assert identity.last_index_outcome == "ok"

    async def test_both_revisions_are_reported_when_they_differ(self):
        identity = await _identity(_gateway(revision="0033"))

        assert identity.db_revision == "0033"
        assert identity.code_revision == "0034"

    async def test_a_database_with_no_version_table_reads_no_revision(self):
        identity = await _identity(_gateway(revision=None))

        assert identity.db_revision is None

    async def test_the_identity_carries_what_the_caller_named(self):
        identity = await _identity(_gateway())

        assert identity.slug == "repo-docs"
        assert identity.root == Path("/x/engram/docs")
        assert identity.database == "localhost:5432/engram"
        assert identity.collection == "repo-docs"

    async def test_changed_since_is_zero_when_disk_equals_the_manifest(self):
        reader = FakeKnowledgeBaseReader(units=[("Units/a.md", "alpha"), ("Units/b.md", "beta")])

        identity = await _identity(_gateway(manifest=disk_content_hashes(reader)), reader=reader)

        assert identity.changed_since == 0

    async def test_changed_since_counts_an_edit_and_an_addition(self):
        before = FakeKnowledgeBaseReader(units=[("Units/a.md", "alpha"), ("Units/b.md", "beta")])
        after = FakeKnowledgeBaseReader(units=[("Units/a.md", "alpha edited"), ("Units/b.md", "beta"), ("Units/new.md", "new")])

        identity = await _identity(_gateway(manifest=disk_content_hashes(before)), reader=after)

        assert identity.changed_since == 2

    async def test_changed_since_equals_the_file_count_when_the_manifest_is_empty(self):
        reader = FakeKnowledgeBaseReader(units=[("Units/a.md", "alpha")], sources=[("Sources/s.md", "mirror")])

        identity = await _identity(_gateway(manifest={}), reader=reader)

        assert identity.changed_since == 2


class TestReadStamp:
    async def test_an_unchanged_knowledge_base_stamps_zero(self):
        reader = FakeKnowledgeBaseReader(units=[("Units/a.md", "alpha"), ("Units/b.md", "beta")])
        stored = disk_content_hashes(reader)

        stamp = await read_stamp(_gateway(manifest=stored), reader, slug="repo-docs", collection="repo-docs")

        assert stamp.slug == "repo-docs"
        assert stamp.indexed_at == COMPILED
        assert stamp.changed_since == 0

    async def test_an_edit_an_addition_and_a_removal_each_count_once(self):
        before = FakeKnowledgeBaseReader(units=[("Units/a.md", "alpha"), ("Units/b.md", "beta"), ("Units/gone.md", "old")])
        after = FakeKnowledgeBaseReader(units=[("Units/a.md", "alpha edited"), ("Units/b.md", "beta"), ("Units/new.md", "new")])

        stamp = await read_stamp(_gateway(manifest=disk_content_hashes(before)), after, slug="repo-docs", collection="repo-docs")

        assert stamp.changed_since == 3

    async def test_a_never_compiled_knowledge_base_counts_every_file(self):
        reader = FakeKnowledgeBaseReader(units=[("Units/a.md", "alpha")], sources=[("Sources/s.md", "mirror")])

        stamp = await read_stamp(_gateway(indexed_at=None), reader, slug="fresh", collection="fresh")

        assert stamp.indexed_at is None
        assert stamp.changed_since == 2


class TestChangedCount:
    async def test_a_trimmed_manifest_read_fails_loudly_rather_than_undercounting(self):
        gateway = _gateway()
        gateway.routed["index_manifest"] = SqlResult(rows=(), columns=("path", "content_hash"), truncated_message="trimmed")

        with pytest.raises(RuntimeError, match="freshness count would be wrong"):
            await read_stamp(gateway, FakeKnowledgeBaseReader(), slug="repo-docs", collection="repo-docs")

    def test_source_mirrors_are_hashed_beside_unit_notes(self):
        reader = FakeKnowledgeBaseReader(units=[("Units/a.md", "alpha")], sources=[("Sources/s.md", "mirror")])

        assert set(disk_content_hashes(reader)) == {"Units/a.md", "Sources/s.md"}

    def test_the_count_is_the_symmetric_difference_plus_the_edits(self):
        stored = {"a": "1", "b": "2", "c": "3"}
        current = {"a": "1", "b": "9", "d": "4"}

        assert changed_count(stored, current) == 3
