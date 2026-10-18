"""Loading a knowledge base's `knowledge-base-schema.json` from its folder."""

from __future__ import annotations

from typing import TYPE_CHECKING

from engram.kb.domain.schema import KnowledgeBaseSchema, _refuse_missing_spec

if TYPE_CHECKING:
    from pathlib import Path


def load_knowledge_base_schema(knowledge_base_path: Path) -> KnowledgeBaseSchema:
    """Load a knowledge base's spec from `<knowledge_base_path>/knowledge-base-schema.json`.

    `knowledge_base_path`'s `_meta/documentation-policy.md` and `_meta/compounding-policy.md` guidance is
    merged over the spec's own (`engram.kb.infrastructure.guidance_pages`).

    Args:
        knowledge_base_path: Path to the knowledge base root directory.

    Returns:
        The knowledge base's `KnowledgeBaseSchema`.

    Raises:
        KnowledgeBaseSchemaMissingError: `knowledge_base_path` carries no `knowledge-base-schema.json`. Naming a template to
            start one from through `studio init --template <slug>`.
    """
    candidate = knowledge_base_path / "knowledge-base-schema.json"
    if not candidate.is_file():
        _refuse_missing_spec(knowledge_base_path)
    return KnowledgeBaseSchema.from_path(candidate)
