"""Integration: the vocab-governance propose→approve loop clears a live index's `unknown-facet-value` flag.

Runs the real five-stage `IndexKnowledgeBaseHandler` (real `FilesystemKnowledgeBaseReader`, fake repos — the
`test_handlers.py` idiom) over a knowledge base carrying one paper with an unregistered `tasks` value. The
first index flags `unknown-facet-value`; `VocabGovernance.propose` + `.approve` land the value in
`_meta/vocabulary/facets.md` via the real `engram.init.contracts.write_scaffold` union-merge; a fresh second index
(re-reading the knowledge base from disk) is clean. `reject` is checked separately: it drops the pending row
and never touches the vocabulary file (byte-compare).

Each index gets its own fresh `FakeKnowledgeBaseUnitOfWork` (no stored fingerprint), so both runs are full
rebuilds reading the knowledge base fresh off disk — the vocabulary edit between them is never masked by
incremental dirty-set narrowing.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from engram.indexing.contracts import FilesystemKnowledgeBaseReader, IndexKnowledgeBase, IndexKnowledgeBaseHandler, VocabGovernance
from engram.kb.contracts import load_knowledge_base_schema
from engram.testing.fakes.embedding import FakeEmbedding
from engram.testing.fakes.uow import FakeKnowledgeBaseUnitOfWork, FakeValidationReportSink
from engram.testing.knowledge_base_builder import KnowledgeBaseBuilder

if TYPE_CHECKING:
    from pathlib import Path

# A minimal paper note whose `tasks` value ("teleportation") is seeded nowhere in the golden
# mini-vocabularies — the same unregistered value `test_validator.py` uses for this exact rule.
_PAPER = """\
---
id: paper.x
type: paper
title: X
summary: A short summary.
status: draft
zotero_key: ZKEY0000
year: 2025
paper_type: method
source_hash: bd72
tasks: [teleportation]
---

## TL;DR

A paper about teleportation retrieval.
"""


def _build_knowledge_base(tmp_path: Path) -> Path:
    """A knowledge base with one paper carrying an unregistered `tasks` value, vocabularies + meta stubs."""
    builder = KnowledgeBaseBuilder(tmp_path)
    builder.add_unit_document("Papers", "x", _PAPER)
    builder.add_vocabulary()
    builder.add_meta_stubs()
    builder.add_knowledge_base_schema()
    return tmp_path


def _handler(knowledge_base_root: Path) -> IndexKnowledgeBaseHandler:
    """A fresh handler over a real filesystem reader + fakes, its own empty UoW (forces a full rebuild)."""
    return IndexKnowledgeBaseHandler(
        FilesystemKnowledgeBaseReader(knowledge_base_root), FakeEmbedding(), FakeKnowledgeBaseUnitOfWork, FakeValidationReportSink(), spec=load_knowledge_base_schema(knowledge_base_root)
    )


async def test_propose_then_approve_clears_the_unknown_facet_value_flag(tmp_path: Path):
    knowledge_base = _build_knowledge_base(tmp_path)

    before = await _handler(knowledge_base).handle(IndexKnowledgeBase(collection="research", embed=False))
    assert any(issue.rule == "unknown-facet-value" and "teleportation" in issue.message for issue in before.validation.errors)

    governance = VocabGovernance(knowledge_base)
    governance.propose(facet="tasks", value="teleportation", page_id="paper.x")
    governance.approve(facet="tasks", value="teleportation")

    after = await _handler(knowledge_base).handle(IndexKnowledgeBase(collection="research", embed=False))
    assert "unknown-facet-value" not in {issue.rule for issue in after.validation.errors}


async def test_reject_leaves_the_registry_untouched(tmp_path: Path):
    knowledge_base = _build_knowledge_base(tmp_path)
    registry_path = knowledge_base / "_meta" / "vocabulary" / "facets.md"
    before_bytes = registry_path.read_bytes()

    governance = VocabGovernance(knowledge_base)
    governance.propose(facet="tasks", value="teleportation", page_id="paper.x")
    governance.reject(facet="tasks", value="teleportation")

    assert registry_path.read_bytes() == before_bytes
    assert governance.pending() == []
