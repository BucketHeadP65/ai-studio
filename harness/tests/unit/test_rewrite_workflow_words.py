"""The one-time rewrite of state written under the old workflow words.

Four things have to hold: a page comes back readable under today's models, a retired version keeps
the link that says which version replaced it, a workflow's earned trust survives the content
address moving under it, and a second run finds nothing to do.
"""

from __future__ import annotations

import hashlib
import json

from typing import TYPE_CHECKING, Any

import pytest

from core.ai.workflows.contracts import RunEvidence, Workflow, trust_level

from harness.migrations import rewrite_workflow_words

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from pathlib import Path

_SPEC: dict[str, Any] = {
    "name": "quarterly-report",
    "version": 1,
    "description": "One step, one output.",
    "parameters": {},
    "tasks": [
        {
            "name": "summarise",
            "instructions": "Summarise the reconciled figures in at most three sentences for a reader in a hurry.",
            "output": {"json_schema": {"type": "string"}},
        }
    ],
    "nodes": [{"kind": "run", "id": "summarise", "work": "summarise", "inputs": {}}],
    "output": {"node": "summarise"},
}


def _page(page_id: str, *, superseded: bool = False) -> dict[str, Any]:
    return {
        "id": page_id,
        "type": "workflow",
        "title": "Quarterly report",
        "summary": "One step.",
        "status": "draft",
        "superseded": superseded,
        "applicability": "Use when the quarter closes.",
        "frontmatter": {
            "spec": json.loads(json.dumps(_SPEC)),
            "fingerprint": {"grants": "abc", "world": {"model": "gpt-5.4", "promoted_capabilities": "digest"}},
            "packed_by": "owner:cli",
            "workflow_version": 1,
            "lineage": {
                "parent_name": "quarterly-report",
                "parent_version": 1,
                "ops": [{"op": "replace_task", "name": "summarise", "with": _SPEC["tasks"][0]}],
                "touched": ["summarise"],
                "evidence": "",
            },
        },
    }


class FakeRows:
    """The library's read side over a list of pages, scriptable by the test that builds it."""

    def __init__(self, pages: list[dict[str, Any]], links: list[tuple[str, str]] = []) -> None:  # noqa: B006 -- read-only default
        self.pages = pages
        self.links = links

    async def fetch(self, sql: str, /, *args: object) -> Sequence[Mapping[str, Any]]:
        del args
        if sql is rewrite_workflow_words.SUPERSEDED_BY_SQL:
            return [{"src_id": src, "dst_id": dst} for src, dst in self.links]
        if sql is rewrite_workflow_words.STALE_SQL:
            stale = [p for p in self.pages if rewrite_workflow_words.migrated_document(p["frontmatter"]) is not None]
            return [{"total": len(stale)}]
        return self.pages

    async def fetchval(self, sql: str, /, *args: object) -> str | None:
        del sql, args
        return None


class FakeIngest:
    """Engram's write path as a callable: it keeps every payload it was handed."""

    def __init__(self) -> None:
        self.written: list[dict[str, Any]] = []

    async def __call__(self, payload: Mapping[str, Any], *, collection: str) -> None:
        self.written.append({"payload": dict(payload), "collection": collection})


async def test_a_saved_workflow_comes_back_readable_under_todays_models() -> None:
    """The point of the whole migration: `spec.tasks` no longer parses, and `spec.inline` does."""
    rows, ingest, report = FakeRows([_page("wf.quarterly-report.v1")]), FakeIngest(), rewrite_workflow_words.Report()

    await rewrite_workflow_words.migrate_library(rows, ingest, report=report)

    written = ingest.written[0]["payload"]["frontmatter"]
    assert report.pages == ["wf.quarterly-report.v1"]
    assert Workflow.model_validate(written["spec"]).inline[0].name == "summarise"
    assert written["saved_by"] == "owner:cli"
    assert "packed_by" not in written


async def test_the_orchestrators_model_leaves_the_stamp() -> None:
    """The environment is what a workflow needs, so the model the orchestrator ran on is dropped."""
    rows, ingest, report = FakeRows([_page("wf.quarterly-report.v1")]), FakeIngest(), rewrite_workflow_words.Report()

    await rewrite_workflow_words.migrate_library(rows, ingest, report=report)

    stamp = ingest.written[0]["payload"]["frontmatter"]["fingerprint"]
    assert stamp["environment"] == {"promoted_capabilities": "digest"}
    assert "world" not in stamp


async def test_a_retired_version_keeps_the_link_to_the_one_that_replaced_it() -> None:
    """Rewriting a superseded page without its link would offer the retired version beside the current one."""
    pages = [_page("wf.quarterly-report.v1", superseded=True), _page("wf.quarterly-report.v2")]
    rows = FakeRows(pages, [("wf.quarterly-report.v1", "wf.quarterly-report.v2")])
    ingest, report = FakeIngest(), rewrite_workflow_words.Report()

    await rewrite_workflow_words.migrate_library(rows, ingest, report=report)

    retired = next(w["payload"]["frontmatter"] for w in ingest.written if w["payload"]["frontmatter"]["id"].endswith("v1"))
    assert retired["superseded_by"] == "[[wf.quarterly-report.v2]]"
    assert report.unlinked == []


async def test_a_superseded_page_with_no_link_to_restore_is_reported_rather_than_rewritten() -> None:
    """Losing the link is unrecoverable, so the page is left as it is and named in the report."""
    rows = FakeRows([_page("wf.quarterly-report.v1", superseded=True)])
    ingest, report = FakeIngest(), rewrite_workflow_words.Report()

    await rewrite_workflow_words.migrate_library(rows, ingest, report=report)

    assert ingest.written == []
    assert report.unlinked == ["wf.quarterly-report.v1"]


async def test_a_second_run_finds_nothing_to_do() -> None:
    """Idempotent: the migrated page is already in the new shape, so nothing is written twice."""
    page = _page("wf.quarterly-report.v1")
    rows, ingest, report = FakeRows([page]), FakeIngest(), rewrite_workflow_words.Report()
    await rewrite_workflow_words.migrate_library(rows, ingest, report=report)
    page["frontmatter"] = ingest.written[0]["payload"]["frontmatter"]

    second = rewrite_workflow_words.Report()
    await rewrite_workflow_words.migrate_library(rows, FakeIngest(), report=second)

    assert await rewrite_workflow_words.needs_library_migration(rows, rewrite_workflow_words.STALE_SQL) is False
    assert second.pages == []
    assert second.lines() == ["nothing to migrate: every page, file and workflow file already reads the new keys"]


def test_a_rewritten_page_that_would_not_parse_is_refused_rather_than_written() -> None:
    """A page nothing can read back is worse than one still carrying the old key."""
    page = _page("wf.quarterly-report.v1")
    page["frontmatter"]["spec"]["nodes"] = [{"kind": "run", "id": "summarise"}]

    with pytest.raises(ValueError, match="does not validate"):
        rewrite_workflow_words.migrated_document(page["frontmatter"])


def test_a_workflow_file_on_disk_gets_the_new_key(tmp_path: Path) -> None:
    """The one on-disk workflow keyed `tasks:` is rewritten in place, and a second pass leaves it alone."""
    folder = tmp_path / "workflows"
    folder.mkdir()
    file = folder / "source-remove.md"
    file.write_text("---\nworkflow:\n  name: x\n  tasks:\n    - name: glue\n---\n", encoding="utf-8")

    report = rewrite_workflow_words.Report()
    rewrite_workflow_words.migrate_workflow_files(tmp_path, report=report)
    again = rewrite_workflow_words.Report()
    rewrite_workflow_words.migrate_workflow_files(tmp_path, report=again)

    assert "\n  inline:\n" in file.read_text(encoding="utf-8")
    assert report.workflow_files == [str(file)]
    assert again.workflow_files == []


async def test_a_lineages_old_adaptation_op_names_take_their_new_ones() -> None:
    """An op name nothing dispatches on any more would make the child's own diff unreadable."""
    rows, ingest, report = FakeRows([_page("wf.quarterly-report.v2")]), FakeIngest(), rewrite_workflow_words.Report()

    await rewrite_workflow_words.migrate_library(rows, ingest, report=report)

    ops = ingest.written[0]["payload"]["frontmatter"]["lineage"]["ops"]
    assert [op["op"] for op in ops] == ["replace_inline_step"]
    assert report.lineages == 1


def test_the_evidence_a_workflow_earned_follows_it_to_its_new_content_address(tmp_path: Path) -> None:
    """The whole point: renaming a key moves the hash, and trust keyed on the old one would read unverified.

    `address_before` rebuilds the pre-wave hash through the models, because a library row is a
    lossy copy of what was hashed: its jsonb column dropped every null the document carried.
    """
    log = tmp_path / "evidence.jsonl"
    before = rewrite_workflow_words.address_before(_SPEC)
    log.write_text(
        "\n".join(
            json.dumps(record)
            for record in (
                RunEvidence(workflow_id="wf.quarterly-report.v1", spec_hash=before, outcome="completed").model_dump(mode="json"),
                RunEvidence(workflow_id="wf.quarterly-report.v1", spec_hash=before, outcome="failed").model_dump(mode="json"),
                RunEvidence(workflow_id="wf.other.v1", spec_hash="untouched", outcome="completed").model_dump(mode="json"),
            )
        )
        + "\n",
        encoding="utf-8",
    )
    earned = trust_level([RunEvidence.model_validate(json.loads(line)) for line in log.read_text().splitlines()], spec_hash=before)

    migrated = rewrite_workflow_words.migrated_document(_page("wf.quarterly-report.v1")["frontmatter"])
    assert migrated is not None
    was, now = migrated.was, migrated.now
    assert was == before
    assert was is not None
    assert now is not None
    report = rewrite_workflow_words.Report()
    rewrite_workflow_words.rekey_evidence(log, {was: now}, report=report)

    kept = [RunEvidence.model_validate(json.loads(line)) for line in log.read_text().splitlines()]
    after = trust_level(kept, spec_hash=now)
    assert report.evidence_rekeyed == 2
    assert after == earned, "the level, the counts and the timestamps read exactly what they read before"
    assert [record.spec_hash for record in kept][2] == "untouched", "another workflow's records are left alone"


def test_re_keying_the_evidence_twice_rewrites_nothing_the_second_time(tmp_path: Path) -> None:
    """Idempotent: the second pass finds no old hash."""
    log = tmp_path / "evidence.jsonl"
    log.write_text(
        json.dumps(RunEvidence(workflow_id="wf.a.v1", spec_hash="old", outcome="completed").model_dump(mode="json")) + "\n",
        encoding="utf-8",
    )
    first, second = rewrite_workflow_words.Report(), rewrite_workflow_words.Report()

    rewrite_workflow_words.rekey_evidence(log, {"old": "new"}, report=first)
    rewrite_workflow_words.rekey_evidence(log, {"old": "new"}, report=second)

    assert first.evidence_rekeyed == 1
    assert second.evidence_rekeyed == 0
    assert json.loads(log.read_text().strip())["spec_hash"] == "new"


def test_a_line_that_is_not_json_is_copied_through(tmp_path: Path) -> None:
    """The evidence log reads forgivingly, and a migration that dropped a damaged line would lose history."""
    log = tmp_path / "evidence.jsonl"
    good = json.dumps(RunEvidence(workflow_id="wf.a.v1", spec_hash="old", outcome="completed").model_dump(mode="json"))
    log.write_text(f"{good}\n{{not json\n", encoding="utf-8")
    report = rewrite_workflow_words.Report()

    rewrite_workflow_words.rekey_evidence(log, {"old": "new"}, report=report)

    assert log.read_text().splitlines()[1] == "{not json"
    assert report.evidence_rekeyed == 1


def test_the_pre_wave_address_is_rebuilt_from_a_row_that_lost_its_nulls() -> None:
    """A jsonb column drops nulls, so the row is not the document that was hashed. Rebuild, never read."""
    full = _SPEC | {"inline": _SPEC["tasks"]}
    del full["tasks"]
    canonical = Workflow.model_validate(full).model_dump(mode="json")
    canonical["tasks"] = canonical.pop("inline")
    hashed_at_save = hashlib.sha256(json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

    lossy = json.loads(json.dumps(_SPEC))
    lossy["nodes"] = [{key: value for key, value in node.items() if value is not None} for node in lossy["nodes"]]

    assert rewrite_workflow_words.address_before(lossy) == hashed_at_save
    assert hashlib.sha256(json.dumps(lossy, sort_keys=True, separators=(",", ":")).encode()).hexdigest() != hashed_at_save


async def test_one_unreadable_page_is_reported_and_the_rest_are_rewritten() -> None:
    """A page that cannot be rewritten costs itself, never the eighty-nine beside it."""
    broken = _page("wf.broken.v1")
    broken["frontmatter"]["spec"]["nodes"] = [{"kind": "run", "id": "summarise"}]
    rows = FakeRows([broken, _page("wf.quarterly-report.v1")])
    ingest, report = FakeIngest(), rewrite_workflow_words.Report()

    await rewrite_workflow_words.migrate_library(rows, ingest, report=report)

    assert report.pages == ["wf.quarterly-report.v1"]
    assert len(report.refused) == 1
    assert "wf.broken.v1" in report.refused[0]
    assert report.clean is False
    assert "left alone, the rewrite would not validate:" in report.lines()


async def test_a_re_run_still_answers_the_addresses_a_crashed_run_left_unkeyed() -> None:
    """The re-key must be resumable: a page already rewritten still names both of its addresses."""
    page = _page("wf.quarterly-report.v1")
    rows, ingest, report = FakeRows([page]), FakeIngest(), rewrite_workflow_words.Report()
    moved = await rewrite_workflow_words.migrate_library(rows, ingest, report=report)
    page["frontmatter"] = ingest.written[0]["payload"]["frontmatter"]

    again = await rewrite_workflow_words.migrate_library(rows, FakeIngest(), report=rewrite_workflow_words.Report())

    assert await rewrite_workflow_words.moved_addresses(rows, rewrite_workflow_words.addresses_of) == moved
    assert again == moved, "a page in the new shape still answers the address its evidence was filed under"
