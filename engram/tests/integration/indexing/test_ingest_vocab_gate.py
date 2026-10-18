"""Integration: `IngestPage`'s vocabulary gate refuses an unregistered facet value, then clears via T6.

Mirrors `test_vocab_governance_flag.py`'s pattern (real filesystem vocabulary + fake UoW, no
database): a knowledge base built by `KnowledgeBaseBuilder` carries the real golden mini-vocabularies, an
`IngestPage` payload with an unregistered `tasks` value is refused with the named finding, the real
`VocabGovernance.propose` + `.approve` land it in `_meta/vocabulary/facets.md`, and re-ingesting the
same payload against the freshly re-parsed vocabulary succeeds.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from engram.indexing.contracts import IngestPage, IngestPageHandler, IngestResult, IngestValidationError, VocabGovernance
from engram.kb.contracts import AuthoringPayload, parse_vocabulary
from engram.testing.fakes.embedding import FakeEmbedding
from engram.testing.fakes.uow import FakeKnowledgeBaseUnitOfWork
from engram.testing.knowledge_base_builder import KnowledgeBaseBuilder
from engram.testing.specs import research_spec

if TYPE_CHECKING:
    from pathlib import Path

    from engram.kb.contracts import VocabularyIndex

PAGE_ID = "ent.evidence-audit"
WRITTEN = "workflows"
"""A collection no knowledge base indexes — the only page_type `IngestPage` accepts."""


def _payload() -> AuthoringPayload:
    """An entity page whose `tasks` facet value ("teleportation") the golden vocabulary lacks.

    `entity_type="technique"` and the relation-free claims stay registered, so the one finding this
    payload can raise is `unknown-facet-value` — the fact T6's governance can actually clear.
    """
    return AuthoringPayload(
        frontmatter={
            "id": PAGE_ID,
            "type": "entity",
            "title": "Evidence audit",
            "summary": "A multi-pass audit over a contested claim.",
            "status": "draft",
            "entity_type": "technique",
            "tasks": ["teleportation"],
        },
        knowledge_base_schema=research_spec(),
    )


def _registry(knowledge_base: Path) -> VocabularyIndex:
    """Re-parse `<knowledge_base>/_meta/vocabulary/*.md` fresh off disk (the gate's own composition)."""
    vocabulary_dir = knowledge_base / "_meta" / "vocabulary"
    docs = [(p.name, p.read_text(encoding="utf-8")) for p in sorted(vocabulary_dir.glob("*.md"))]
    return parse_vocabulary(docs).index


def _handler(vocabulary: VocabularyIndex) -> IngestPageHandler:
    """A fresh handler over its own empty `FakeKnowledgeBaseUnitOfWork`, gated by `vocabulary`."""
    return IngestPageHandler(FakeKnowledgeBaseUnitOfWork, FakeEmbedding(), vocabulary=vocabulary, spec=research_spec())


async def test_unknown_facet_value_is_refused_then_passes_after_a_real_propose_approve(tmp_path: Path):
    knowledge_base = KnowledgeBaseBuilder(tmp_path).build_minimal()

    with pytest.raises(IngestValidationError) as exc_info:
        await _handler(_registry(knowledge_base)).handle(IngestPage(collection=WRITTEN, payload=_payload()))
    (finding,) = exc_info.value.findings
    assert finding.rule == "unknown-facet-value"
    assert "teleportation" in finding.message

    governance = VocabGovernance(knowledge_base)
    governance.propose(facet="tasks", value="teleportation", page_id=PAGE_ID)
    governance.approve(facet="tasks", value="teleportation")

    result = await _handler(_registry(knowledge_base)).handle(IngestPage(collection=WRITTEN, payload=_payload()))

    assert isinstance(result, IngestResult)
    assert result.page_id == PAGE_ID
