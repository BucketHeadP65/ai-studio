"""Unit tests for `VocabGovernance` (`src/engram/indexing/infrastructure/vocab_governance.py`).

Every row round-trips through a real file under `tmp_path` — fakes over mocks, no monkeypatching:
`propose`/`approve`/`reject` write, `pending` reads back. `approve` additionally exercises the real
`engram.init.contracts.write_scaffold` union-merge over a real `facets.md` (built via
`KnowledgeBaseBuilder.add_vocabulary()`, the golden mini-vocabularies), so the vocabulary-landing half is
pinned against the real merge, not a stand-in.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest

from engram.indexing.contracts import ProposalUnknownError, VocabGovernance
from engram.kb.contracts import parse_vocabulary
from engram.testing.knowledge_base_builder import KnowledgeBaseBuilder

if TYPE_CHECKING:
    from pathlib import Path

_FIXED_TIME = datetime(2026, 8, 20, 12, 0, 0, tzinfo=UTC)


def _clock() -> datetime:
    return _FIXED_TIME


def _facets(knowledge_base: Path):
    """Parse `<knowledge_base>/_meta/vocabulary/*.md` into a `VocabularyIndex` (the same read the indexer does)."""
    vocabulary_dir = knowledge_base / "_meta" / "vocabulary"
    docs = [(p.name, p.read_text(encoding="utf-8")) for p in sorted(vocabulary_dir.glob("*.md"))]
    return parse_vocabulary(docs).index


class TestPending:
    def test_missing_pending_file_is_an_empty_list(self, tmp_path: Path):
        governance = VocabGovernance(tmp_path)

        assert governance.pending() == []


class TestPropose:
    def test_propose_appends_a_pending_row(self, tmp_path: Path):
        governance = VocabGovernance(tmp_path, clock=_clock)

        proposal = governance.propose(facet="tasks", value="teleportation", page_id="paper.x")

        assert proposal.facet == "tasks"
        assert proposal.value == "teleportation"
        assert proposal.page_id == "paper.x"
        assert proposal.proposed_at == _FIXED_TIME
        assert governance.pending() == [proposal]

    def test_propose_persists_across_instances(self, tmp_path: Path):
        VocabGovernance(tmp_path, clock=_clock).propose(facet="tasks", value="teleportation", page_id="paper.x")

        reloaded = VocabGovernance(tmp_path).pending()

        assert [row.value for row in reloaded] == ["teleportation"]

    def test_propose_is_idempotent_on_facet_and_value(self, tmp_path: Path):
        governance = VocabGovernance(tmp_path, clock=_clock)
        first = governance.propose(facet="tasks", value="teleportation", page_id="paper.x")

        second = governance.propose(facet="tasks", value="teleportation", page_id="paper.y")

        assert second == first  # first sighting wins, not re-stamped with the second page_id
        assert len(governance.pending()) == 1

    def test_propose_creates_the_registry_directory_if_absent(self, tmp_path: Path):
        governance = VocabGovernance(tmp_path, clock=_clock)

        governance.propose(facet="tasks", value="teleportation", page_id="paper.x")

        assert (tmp_path / "_meta" / "vocabulary" / "pending.yaml").is_file()

    def test_two_different_proposals_both_land(self, tmp_path: Path):
        governance = VocabGovernance(tmp_path, clock=_clock)

        governance.propose(facet="tasks", value="teleportation", page_id="paper.x")
        governance.propose(facet="domains", value="astrology", page_id="paper.y")

        assert {(row.facet, row.value) for row in governance.pending()} == {("tasks", "teleportation"), ("domains", "astrology")}


class TestReject:
    def test_reject_removes_the_pending_row(self, tmp_path: Path):
        governance = VocabGovernance(tmp_path, clock=_clock)
        governance.propose(facet="tasks", value="teleportation", page_id="paper.x")

        governance.reject(facet="tasks", value="teleportation")

        assert governance.pending() == []

    def test_reject_leaves_other_rows_untouched(self, tmp_path: Path):
        governance = VocabGovernance(tmp_path, clock=_clock)
        governance.propose(facet="tasks", value="teleportation", page_id="paper.x")
        governance.propose(facet="domains", value="astrology", page_id="paper.y")

        governance.reject(facet="tasks", value="teleportation")

        assert [row.value for row in governance.pending()] == ["astrology"]

    def test_reject_of_an_unknown_proposal_raises(self, tmp_path: Path):
        governance = VocabGovernance(tmp_path, clock=_clock)

        with pytest.raises(ProposalUnknownError):
            governance.reject(facet="tasks", value="teleportation")

    def test_reject_leaves_the_registry_file_byte_identical(self, tmp_path: Path):
        knowledge_base = KnowledgeBaseBuilder(tmp_path).build_minimal()
        registry_path = knowledge_base / "_meta" / "vocabulary" / "facets.md"
        before = registry_path.read_bytes()
        governance = VocabGovernance(knowledge_base, clock=_clock)
        governance.propose(facet="tasks", value="teleportation", page_id="paper.x")

        governance.reject(facet="tasks", value="teleportation")

        assert registry_path.read_bytes() == before


class TestApprove:
    def test_approve_lands_the_value_in_the_registry(self, tmp_path: Path):
        knowledge_base = KnowledgeBaseBuilder(tmp_path).build_minimal()
        governance = VocabGovernance(knowledge_base, clock=_clock)
        governance.propose(facet="tasks", value="teleportation", page_id="paper.x")

        governance.approve(facet="tasks", value="teleportation")

        assert "teleportation" in _facets(knowledge_base).facets["tasks"]

    def test_approve_clears_the_pending_row(self, tmp_path: Path):
        knowledge_base = KnowledgeBaseBuilder(tmp_path).build_minimal()
        governance = VocabGovernance(knowledge_base, clock=_clock)
        governance.propose(facet="tasks", value="teleportation", page_id="paper.x")

        governance.approve(facet="tasks", value="teleportation")

        assert governance.pending() == []

    def test_approve_preserves_other_registered_values(self, tmp_path: Path):
        knowledge_base = KnowledgeBaseBuilder(tmp_path).build_minimal()
        governance = VocabGovernance(knowledge_base, clock=_clock)
        governance.propose(facet="tasks", value="teleportation", page_id="paper.x")

        governance.approve(facet="tasks", value="teleportation")

        assert "long-doc-qa" in _facets(knowledge_base).facets["tasks"]  # a pre-existing seed value, untouched

    def test_approve_of_an_already_registered_value_is_a_clean_no_op(self, tmp_path: Path):
        knowledge_base = KnowledgeBaseBuilder(tmp_path).build_minimal()
        governance = VocabGovernance(knowledge_base, clock=_clock)
        governance.propose(facet="tasks", value="long-doc-qa", page_id="paper.x")  # already seeded

        governance.approve(facet="tasks", value="long-doc-qa")

        assert list(_facets(knowledge_base).facets["tasks"]).count("long-doc-qa") == 1
        assert governance.pending() == []

    def test_approve_of_an_unknown_proposal_raises(self, tmp_path: Path):
        knowledge_base = KnowledgeBaseBuilder(tmp_path).build_minimal()
        governance = VocabGovernance(knowledge_base, clock=_clock)

        with pytest.raises(ProposalUnknownError):
            governance.approve(facet="tasks", value="teleportation")

    def test_approve_creates_facets_md_when_absent(self, tmp_path: Path):
        builder = KnowledgeBaseBuilder(tmp_path)
        builder.add_vocabulary(names=("entity-types", "relation-types"))  # no facets.md yet
        builder.add_meta_stubs()
        governance = VocabGovernance(tmp_path, clock=_clock)
        governance.propose(facet="confidence", value="high", page_id="paper.x")

        governance.approve(facet="confidence", value="high")

        assert "high" in _facets(tmp_path).facets["confidence"]

    def test_approve_writes_a_value_holding_a_pipe_as_one_table_row_of_a_new_facets_md(self, tmp_path: Path):
        builder = KnowledgeBaseBuilder(tmp_path)
        builder.add_vocabulary(names=("entity-types", "relation-types"))
        builder.add_meta_stubs()
        governance = VocabGovernance(tmp_path, clock=_clock)
        governance.propose(facet="venue", value="Grand Hall | East Wing", page_id="paper.x")

        governance.approve(facet="venue", value="Grand Hall | East Wing")

        assert _facets(tmp_path).facets["venue"] == {"Grand Hall | East Wing": "proposed via vocab governance"}

    def test_approve_merges_a_value_holding_a_pipe_as_one_table_row_beside_the_registered_ones(self, tmp_path: Path):
        builder = KnowledgeBaseBuilder(tmp_path)
        builder.add_vocabulary(names=("entity-types", "relation-types"))
        builder.add_meta_stubs()
        governance = VocabGovernance(tmp_path, clock=_clock)
        governance.propose(facet="venue", value="Plain Hall", page_id="paper.x")
        governance.approve(facet="venue", value="Plain Hall")
        governance.propose(facet="venue", value="Grand Hall | East Wing", page_id="paper.y")

        governance.approve(facet="venue", value="Grand Hall | East Wing")

        assert _facets(tmp_path).facets["venue"] == {
            "Plain Hall": "proposed via vocab governance",
            "Grand Hall | East Wing": "proposed via vocab governance",
        }


def _sections(text: str) -> dict[str, str]:
    """Split a `facets.md` into its H2 sections keyed by heading line, the text before the first H2 under `""`."""
    sections: dict[str, str] = {}
    heading = ""
    lines: list[str] = []
    for line in text.splitlines():
        if line.startswith("## "):
            sections[heading] = "\n".join(lines).rstrip("\n")
            heading, lines = line, []
        else:
            lines.append(line)
    sections[heading] = "\n".join(lines).rstrip("\n")
    return sections


_TASKS_HEADING = "## `tasks` — on paper, entity, comparison, playbook, question"
_SCALE_HEADING = "## `scale_regimes` — on playbook, comparison"


class TestApproveKeepsCuratedText:
    def test_approve_leaves_every_other_section_byte_identical(self, tmp_path: Path):
        knowledge_base = KnowledgeBaseBuilder(tmp_path).build_minimal()
        registry_path = knowledge_base / "_meta" / "vocabulary" / "facets.md"
        before = _sections(registry_path.read_text(encoding="utf-8"))
        governance = VocabGovernance(knowledge_base, clock=_clock)
        governance.propose(facet="tasks", value="retrieval", page_id="paper.x")

        governance.approve(facet="tasks", value="retrieval")

        after = _sections(registry_path.read_text(encoding="utf-8"))
        assert {k: v for k, v in after.items() if k != _TASKS_HEADING} == {k: v for k, v in before.items() if k != _TASKS_HEADING}

    def test_approve_keeps_the_prose_of_a_facet_documented_as_prose(self, tmp_path: Path):
        knowledge_base = KnowledgeBaseBuilder(tmp_path).build_minimal()
        registry_path = knowledge_base / "_meta" / "vocabulary" / "facets.md"
        before = _sections(registry_path.read_text(encoding="utf-8"))[_TASKS_HEADING]
        governance = VocabGovernance(knowledge_base, clock=_clock)
        governance.propose(facet="tasks", value="retrieval", page_id="paper.x")

        governance.approve(facet="tasks", value="retrieval")

        after = _sections(registry_path.read_text(encoding="utf-8"))[_TASKS_HEADING]
        assert after.startswith(before)
        assert "| Value | Definition |" not in after
        assert {"open-domain-qa", "sensemaking-qa", "retrieval"} <= set(_facets(knowledge_base).facets["tasks"])

    def test_approve_adds_a_row_to_a_facet_documented_as_a_table(self, tmp_path: Path):
        knowledge_base = KnowledgeBaseBuilder(tmp_path).build_minimal()
        registry_path = knowledge_base / "_meta" / "vocabulary" / "facets.md"
        before = _sections(registry_path.read_text(encoding="utf-8"))[_SCALE_HEADING]
        governance = VocabGovernance(knowledge_base, clock=_clock)
        governance.propose(facet="scale_regimes", value="planet-scale", page_id="paper.x")

        governance.approve(facet="scale_regimes", value="planet-scale")

        after = _sections(registry_path.read_text(encoding="utf-8"))[_SCALE_HEADING]
        assert after == before + "\n| `planet-scale` | proposed via vocab governance |"

    def test_approve_of_an_already_registered_value_leaves_the_file_byte_identical(self, tmp_path: Path):
        knowledge_base = KnowledgeBaseBuilder(tmp_path).build_minimal()
        registry_path = knowledge_base / "_meta" / "vocabulary" / "facets.md"
        before = registry_path.read_bytes()
        governance = VocabGovernance(knowledge_base, clock=_clock)
        governance.propose(facet="tasks", value="long-doc-qa", page_id="paper.x")

        governance.approve(facet="tasks", value="long-doc-qa")

        assert registry_path.read_bytes() == before

    def test_approve_keeps_prose_values_when_the_section_holds_a_note_table(self, tmp_path: Path):
        registry_path = tmp_path / "_meta" / "vocabulary" / "facets.md"
        registry_path.parent.mkdir(parents=True)
        registry_path.write_text(
            "# Facets\n\n## `foo` — on all\n\nSeed: `a`, `b`.\n\n| Note | Text |\n|---|---|\n| scope | papers only |\n",
            encoding="utf-8",
        )
        governance = VocabGovernance(tmp_path, clock=_clock)
        governance.propose(facet="foo", value="c", page_id="paper.x")

        governance.approve(facet="foo", value="c")

        assert set(_facets(tmp_path).facets["foo"]) == {"a", "b", "c"}
        assert "| scope | papers only |\n" in registry_path.read_text(encoding="utf-8")

    def test_approve_of_a_new_facet_appends_its_section_after_the_file(self, tmp_path: Path):
        knowledge_base = KnowledgeBaseBuilder(tmp_path).build_minimal()
        registry_path = knowledge_base / "_meta" / "vocabulary" / "facets.md"
        before = registry_path.read_text(encoding="utf-8")
        governance = VocabGovernance(knowledge_base, clock=_clock)
        governance.propose(facet="confidence", value="high", page_id="paper.x")

        governance.approve(facet="confidence", value="high")

        after = registry_path.read_text(encoding="utf-8")
        assert after.startswith(before.rstrip("\n"))
        assert after.endswith("## `confidence`: on all\n\n| Value | Definition |\n|---|---|\n| `high` | proposed via vocab governance |\n")


class TestAtomicity:
    def test_a_stray_temp_file_from_an_interrupted_write_is_ignored(self, tmp_path: Path):
        governance = VocabGovernance(tmp_path, clock=_clock)
        governance.propose(facet="tasks", value="teleportation", page_id="paper.x")
        (tmp_path / "_meta" / "vocabulary" / "pending.yaml.tmp").write_text("not: [valid, yaml,", encoding="utf-8")

        reloaded = VocabGovernance(tmp_path).pending()

        assert [row.value for row in reloaded] == ["teleportation"]

    def test_the_temp_file_never_survives_a_successful_write(self, tmp_path: Path):
        governance = VocabGovernance(tmp_path, clock=_clock)

        governance.propose(facet="tasks", value="teleportation", page_id="paper.x")

        assert not (tmp_path / "_meta" / "vocabulary" / "pending.yaml.tmp").exists()
