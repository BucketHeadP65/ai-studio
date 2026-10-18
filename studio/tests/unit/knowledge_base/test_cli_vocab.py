r"""Unit tests for the `studio vocab` CLI subcommand group (`src/studio/knowledge_base/cli_vocab.py`).

Pure filesystem — no DB, no container — the same style as `init scaffold`'s tests
(`test_cli_init.py`): `CliRunner.invoke` over a real `tmp_path` knowledge base, no `ctx.obj` seam needed.

The contract under test:

* `vocab propose --facet --value --page [--knowledge-base]` — records a pending row, idempotent on
  `(facet, value)`.
* `vocab list [--knowledge-base]` — echoes the pending queue, or a clean empty note.
* `vocab approve --facet --value [--knowledge-base]` — union-merges the value into
  `_meta/vocabulary/facets.md` and clears its pending row; exits 1 on an unknown proposal.
* `vocab reject --facet --value [--knowledge-base]` — drops the pending row, vocabulary untouched; exits 1
  on an unknown proposal.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from engram.indexing.contracts import VocabGovernance
from engram.kb.contracts import parse_vocabulary
from engram.testing.knowledge_base_builder import KnowledgeBaseBuilder
from typer.testing import CliRunner

from studio.knowledge_base.cli import app

if TYPE_CHECKING:
    from pathlib import Path

runner = CliRunner()


def _knowledge_base(tmp_path: Path) -> Path:
    return KnowledgeBaseBuilder(tmp_path).build_minimal()


def _facets(knowledge_base: Path):
    vocabulary_dir = knowledge_base / "_meta" / "vocabulary"
    docs = [(p.name, p.read_text(encoding="utf-8")) for p in sorted(vocabulary_dir.glob("*.md"))]
    return parse_vocabulary(docs).index


class TestPropose:
    def test_propose_records_a_pending_row(self, tmp_path: Path):
        knowledge_base = _knowledge_base(tmp_path)

        result = runner.invoke(app, ["vocab", "propose", "--facet", "tasks", "--value", "teleportation", "--page", "paper.x", "--knowledge-base", str(knowledge_base)])

        assert result.exit_code == 0, result.output
        assert "teleportation" in result.output
        rows = VocabGovernance(knowledge_base).pending()
        assert [(row.facet, row.value, row.page_id) for row in rows] == [("tasks", "teleportation", "paper.x")]

    def test_propose_is_idempotent(self, tmp_path: Path):
        knowledge_base = _knowledge_base(tmp_path)
        args = ["vocab", "propose", "--facet", "tasks", "--value", "teleportation", "--page", "paper.x", "--knowledge-base", str(knowledge_base)]

        runner.invoke(app, args)
        second = runner.invoke(app, args)

        assert second.exit_code == 0, second.output
        assert len(VocabGovernance(knowledge_base).pending()) == 1


class TestList:
    def test_list_reports_no_pending_proposals_cleanly(self, tmp_path: Path):
        knowledge_base = _knowledge_base(tmp_path)

        result = runner.invoke(app, ["vocab", "list", "--knowledge-base", str(knowledge_base)])

        assert result.exit_code == 0, result.output
        assert "No pending" in result.output

    def test_list_renders_each_pending_proposal(self, tmp_path: Path):
        knowledge_base = _knowledge_base(tmp_path)
        runner.invoke(app, ["vocab", "propose", "--facet", "tasks", "--value", "teleportation", "--page", "paper.x", "--knowledge-base", str(knowledge_base)])

        result = runner.invoke(app, ["vocab", "list", "--knowledge-base", str(knowledge_base)])

        assert result.exit_code == 0, result.output
        assert "tasks" in result.output
        assert "teleportation" in result.output
        assert "paper.x" in result.output


class TestApprove:
    def test_approve_lands_the_value_and_clears_pending(self, tmp_path: Path):
        knowledge_base = _knowledge_base(tmp_path)
        runner.invoke(app, ["vocab", "propose", "--facet", "tasks", "--value", "teleportation", "--page", "paper.x", "--knowledge-base", str(knowledge_base)])

        result = runner.invoke(app, ["vocab", "approve", "--facet", "tasks", "--value", "teleportation", "--knowledge-base", str(knowledge_base)])

        assert result.exit_code == 0, result.output
        assert "teleportation" in _facets(knowledge_base).facets["tasks"]
        assert VocabGovernance(knowledge_base).pending() == []

    def test_approve_of_an_unknown_proposal_exits_one(self, tmp_path: Path):
        knowledge_base = _knowledge_base(tmp_path)

        result = runner.invoke(app, ["vocab", "approve", "--facet", "tasks", "--value", "teleportation", "--knowledge-base", str(knowledge_base)])

        assert result.exit_code == 1


class TestReject:
    def test_reject_clears_pending_and_leaves_the_registry_untouched(self, tmp_path: Path):
        knowledge_base = _knowledge_base(tmp_path)
        registry_path = knowledge_base / "_meta" / "vocabulary" / "facets.md"
        before = registry_path.read_bytes()
        runner.invoke(app, ["vocab", "propose", "--facet", "tasks", "--value", "teleportation", "--page", "paper.x", "--knowledge-base", str(knowledge_base)])

        result = runner.invoke(app, ["vocab", "reject", "--facet", "tasks", "--value", "teleportation", "--knowledge-base", str(knowledge_base)])

        assert result.exit_code == 0, result.output
        assert registry_path.read_bytes() == before
        assert VocabGovernance(knowledge_base).pending() == []

    def test_reject_of_an_unknown_proposal_exits_one(self, tmp_path: Path):
        knowledge_base = _knowledge_base(tmp_path)

        result = runner.invoke(app, ["vocab", "reject", "--facet", "tasks", "--value", "teleportation", "--knowledge-base", str(knowledge_base)])

        assert result.exit_code == 1
