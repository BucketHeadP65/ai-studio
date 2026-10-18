"""Apply selected recommendation proposals to a knowledge base's `knowledge-base-schema.json`.

`apply_schema_edits` applies the selected proposals' edits to the current schema and rewrites the file in the
working tree only. It never indexes, never edits notes, never regenerates datasets and never runs git.
Applying the same selection again skips every edit already in place, leaves the file byte-identical and
reports `changed=False`. A relation with the same slug and a different declaration is a conflict and raises.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict

from engram.evals.recommendation.domain.recommendation import AddRelationOp, RestructureSectionsOp, apply_op
from engram.evals.recommendation.infrastructure.recommendation_files import load_recommendation
from engram.kb.contracts import load_knowledge_base_schema

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from engram.evals.recommendation.domain.recommendation import SchemaEditOp
    from engram.kb.contracts import KnowledgeBaseSchema


class ApplySchemaEditsReport(BaseModel):
    """The outcome of one `apply_schema_edits` run, what changed and what was already in place.

    Attributes:
        applied_ids: The proposal ids whose edits this run applied to the schema.
        already_applied_ids: The proposal ids whose effect was already present and was skipped.
        knowledge_base_schema_path: The `knowledge-base-schema.json` path the run targeted.
        changed: Whether the file's bytes actually changed.
    """

    model_config = ConfigDict(frozen=True)

    applied_ids: tuple[str, ...]
    already_applied_ids: tuple[str, ...]
    knowledge_base_schema_path: str
    changed: bool


def apply_schema_edits(*, recommendation_path: Path, proposal_ids: Sequence[str], knowledge_base_path: Path) -> ApplySchemaEditsReport:
    """Apply the selected proposals to the knowledge base's schema and rewrite its schema file, working tree only.

    Args:
        recommendation_path: The recommendation YAML file `evals recommend` wrote, to select proposals from.
        proposal_ids: The proposal ids to apply, validated against the recommendation before any write.
        knowledge_base_path: The knowledge base root whose `knowledge-base-schema.json` is rewritten.

    Returns:
        An `ApplySchemaEditsReport` naming the applied and skipped proposals and whether the file changed.

    Raises:
        ValueError: On an empty selection, an unknown proposal id, or an edit the current schema refuses.
    """
    recommendation = load_recommendation(recommendation_path)
    by_id = {proposal.id: proposal for proposal in recommendation.proposals}
    if not proposal_ids:
        msg = f"no proposal ids selected, pass --id for each proposal to apply (recommendation has: {', '.join(sorted(by_id)) or 'none'})"
        raise ValueError(msg)
    unknown = [proposal_id for proposal_id in proposal_ids if proposal_id not in by_id]
    if unknown:
        msg = f"unknown proposal id(s) {', '.join(unknown)}. The recommendation has: {', '.join(sorted(by_id)) or 'none'}"
        raise ValueError(msg)

    spec = load_knowledge_base_schema(knowledge_base_path)
    applied: list[str] = []
    already_applied: list[str] = []
    for proposal_id in proposal_ids:
        op = by_id[proposal_id].op
        if _op_already_applied(spec, op):
            already_applied.append(proposal_id)
            continue
        spec = apply_op(spec, op)
        applied.append(proposal_id)

    target = knowledge_base_path / "knowledge-base-schema.json"
    rendered = spec.model_dump_json(indent=2) + "\n"
    changed = not target.exists() or target.read_text(encoding="utf-8") != rendered
    if changed:
        target.write_text(rendered, encoding="utf-8")
    return ApplySchemaEditsReport(
        applied_ids=tuple(applied), already_applied_ids=tuple(already_applied), knowledge_base_schema_path=str(target), changed=changed
    )


def _op_already_applied(spec: KnowledgeBaseSchema, op: SchemaEditOp) -> bool:
    """Whether the edit's full effect is already in the schema."""
    if isinstance(op, RestructureSectionsOp):
        page_type = spec.page_type(op.page_type)
        return page_type is not None and page_type.canonical_section_slugs == op.canonical_sections
    return _add_relation_already_applied(spec, op)


def _add_relation_already_applied(spec: KnowledgeBaseSchema, op: AddRelationOp) -> bool:
    """Whether the relation and its edge field are already declared exactly as the edit would add them."""
    if not any(declared == op.relation for declared in spec.relations):
        return False
    page_type = spec.page_type(op.on_page_type)
    if page_type is None:
        return False
    field_present = any(field == op.edge_field for field in page_type.frontmatter_fields)
    return field_present and op.edge_field.name in page_type.edge_fields
