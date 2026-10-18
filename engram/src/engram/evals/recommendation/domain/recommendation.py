"""The recommendation and the typed schema edits it proposes.

`SchemaEditOp` is one of two edits. `RestructureSectionsOp` replaces a page type's canonical sections, and
`AddRelationOp` registers a typed relation and adds its edge field to a page type. `apply_op` applies one edit
to a schema and returns a new `KnowledgeBaseSchema` that has passed the schema's own validation.
`Recommendation` holds a baseline snapshot stamped with the knowledge base's identity, the ranked
`SchemaEditProposal`s and a free-form findings tail.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Annotated, Literal

from pydantic import AliasChoices, BaseModel, ConfigDict, Field

from engram.kb.contracts import FrontmatterFieldSpec, KnowledgeBaseSchema, RelationDecl

SchemaEditOpName = Literal["restructure_sections", "add_relation"]
"""The names of the two schema edits, the values of the `SchemaEditOp` discriminator."""


class RestructureSectionsOp(BaseModel):
    """Replace one page type's canonical H2 sections, the edit that changes how its pages are chunked.

    Attributes:
        op: The discriminator, always `restructure_sections`.
        page_type: The page type whose sections are replaced. The target schema must declare it.
            Files and JSON carry it under the key `kind`.
        canonical_sections: The full ordered list of section slugs that replaces the old one, not a patch.
    """

    model_config = ConfigDict(frozen=True, serialize_by_alias=True)

    op: Literal["restructure_sections"] = "restructure_sections"
    page_type: str = Field(validation_alias=AliasChoices("kind", "page_type"), serialization_alias="kind")
    canonical_sections: tuple[str, ...]


class AddRelationOp(BaseModel):
    """Register a typed relation and add its edge field to a page type, the edit that adds graph edges.

    The edge field must be of type `link` or `link-list`. The op accepts any field, and `apply_op` raises
    on a field of another type.

    Attributes:
        op: The discriminator, always `add_relation`.
        relation: The relation declaration to register on the schema.
        on_page_type: The page type that gains the edge field. Files and JSON carry it under the key `on_kind`.
        edge_field: The frontmatter field to add, of type `link` or `link-list`.
    """

    model_config = ConfigDict(frozen=True, serialize_by_alias=True)

    op: Literal["add_relation"] = "add_relation"
    relation: RelationDecl
    on_page_type: str = Field(validation_alias=AliasChoices("on_kind", "on_page_type"), serialization_alias="on_kind")
    edge_field: FrontmatterFieldSpec


SchemaEditOp = Annotated[RestructureSectionsOp | AddRelationOp, Field(discriminator="op")]
"""One typed schema edit, discriminated on its `op` field."""


def apply_op(spec: KnowledgeBaseSchema, op: SchemaEditOp) -> KnowledgeBaseSchema:
    """Apply one schema edit and return a new validated `KnowledgeBaseSchema`.

    The input schema is never changed. The returned schema has passed every check of the schema's own validation.

    Args:
        spec: The knowledge base schema to edit.
        op: The typed edit to apply, a restructure or an added relation.

    Returns:
        A new validated `KnowledgeBaseSchema` with the edit applied.

    Raises:
        ValueError: When the edit names a page type the schema does not declare. A duplicate relation slug
            or an edge field that is not a link raises `pydantic.ValidationError`, a `ValueError` subclass.
    """
    if isinstance(op, RestructureSectionsOp):
        return _apply_restructure(spec, op)
    return _apply_add_relation(spec, op)


def _apply_restructure(spec: KnowledgeBaseSchema, op: RestructureSectionsOp) -> KnowledgeBaseSchema:
    """Rebuild the schema with the named page type's canonical sections replaced."""
    if spec.page_type(op.page_type) is None:
        msg = f"unknown page type kind {op.page_type!r} for restructure_sections in spec {spec.slug!r}"
        raise ValueError(msg)
    data = spec.model_dump()
    for page_type in data["page_types"]:
        if page_type["page_type"] == op.page_type:
            page_type["canonical_sections"] = list(op.canonical_sections)
    return KnowledgeBaseSchema.model_validate(data)


def _apply_add_relation(spec: KnowledgeBaseSchema, op: AddRelationOp) -> KnowledgeBaseSchema:
    """Rebuild the schema with the relation registered and the edge field added to its page type."""
    if spec.page_type(op.on_page_type) is None:
        msg = f"unknown page type kind {op.on_page_type!r} for add_relation in spec {spec.slug!r}"
        raise ValueError(msg)
    data = spec.model_dump()
    data["relations"] = [*data["relations"], op.relation.model_dump()]
    for page_type in data["page_types"]:
        if page_type["page_type"] == op.on_page_type:
            page_type["frontmatter_fields"] = [*page_type["frontmatter_fields"], op.edge_field.model_dump()]
            page_type["edge_fields"] = [*page_type["edge_fields"], op.edge_field.name]
    return KnowledgeBaseSchema.model_validate(data)


class PageWorkOrder(BaseModel):
    """One row of a proposal's migration brief, an imperative per-page authoring action.

    Attributes:
        page_id: The page the action applies to.
        note_path: The knowledge-base-relative path of the note to edit.
        action: The imperative instruction, for example splitting an H2 at a named H3 boundary.
    """

    model_config = ConfigDict(frozen=True)

    page_id: str
    note_path: str
    action: str


class Prediction(BaseModel):
    """A falsifiable claim the proposal makes, which `verify-recommendation` checks after the migration.

    Attributes:
        description: A short human-readable statement of the expected effect.
        target_cases: The dataset case names expected to move.
        metric: The metric the prediction is stated in, for example `recall@5`.
        direction: Whether the metric should increase or merely not regress.
    """

    model_config = ConfigDict(frozen=True)

    description: str
    target_cases: tuple[str, ...]
    metric: str
    direction: Literal["increase", "no_regression"]


class CandidateEdge(BaseModel):
    """A hypothetical typed edge added to the graph for a counterfactual.

    Attributes:
        src_id: The source page id.
        dst_id: The destination page id.
        rel: The relation slug the edge would carry.
    """

    model_config = ConfigDict(frozen=True)

    src_id: str
    dst_id: str
    rel: str


class CounterfactualResult(BaseModel):
    """How many failing gold pairs become reachable once candidate edges are added, measured with no model.

    Attributes:
        n_pairs: The number of failing gold pairs tested.
        reachable_before: How many pairs are reachable in the current graph.
        reachable_after: How many pairs are reachable once the candidate edges are added.
        max_hops: The most hops a path could take.
        candidate_edges: The candidate edges added to the graph.
    """

    model_config = ConfigDict(frozen=True)

    n_pairs: int
    reachable_before: int
    reachable_after: int
    max_hops: int
    candidate_edges: tuple[CandidateEdge, ...]


class MinedLink(BaseModel):
    """One prose link that is in `kb.link_refs` and has no row in `kb.edges`, a candidate for a new relation.

    Attributes:
        src_page_id: The page the link appears on.
        src_type: The source page's page type.
        raw_target: The raw link target text as written.
        dst_page_id: The resolved destination page id, or None when the link resolves to no page.
        dst_type: The destination page's page type, or None when the link resolves to no page.
        rel: The `link_refs` relation the link appeared under.
    """

    model_config = ConfigDict(frozen=True)

    src_page_id: str
    src_type: str
    raw_target: str
    dst_page_id: str | None
    dst_type: str | None
    rel: str


class SimulatedSplit(BaseModel):
    """One simulated split: a failing case's query ranked against one over-budget gold section and its parts.

    Ranks come from a length-normalised lexical score, which stands in for BM25 search, against every
    section in the knowledge base.

    Attributes:
        case_name: The dataset case whose query was ranked.
        page_id: The gold page owning the over-budget section.
        canon: The over-budget section's canonical slug.
        n_parts: How many parts the split produced.
        rank_before: The whole section body's would-be rank for the query, 1-based.
        rank_after: The best part's would-be rank for the query, 1-based.
    """

    model_config = ConfigDict(frozen=True)

    case_name: str
    page_id: str
    canon: str
    n_parts: int
    rank_before: int
    rank_after: int


class SimulationResult(BaseModel):
    """The measured rank change from simulating the split of one restructure proposal.

    Lower rank is better, so a positive `mean_rank_before - mean_rank_after` gap supports the split.

    Attributes:
        n_measured: How many (case, section) pairs produced a rank before and after.
        n_unsplittable: How many over-budget sections the paragraph split could not divide.
        mean_rank_before: The mean would-be rank of the unsplit bodies, 0.0 when nothing was measured.
        mean_rank_after: The mean would-be rank of the best parts, 0.0 when nothing was measured.
        splits: The individual simulated splits.
    """

    model_config = ConfigDict(frozen=True)

    n_measured: int
    n_unsplittable: int
    mean_rank_before: float
    mean_rank_after: float
    splits: tuple[SimulatedSplit, ...]


class ProposalEvidence(BaseModel):
    """The measured evidence a proposal is ranked and gated on.

    Attributes:
        case_names: The failing cases this proposal explains.
        recall_deficit: How far the bucket's recall falls below the run's overall recall. Proposals are ranked by it.
        counterfactual: The graph counterfactual for an add-relation proposal, or None.
        mined_links: The prose links with no edge behind an add-relation proposal.
        oversized_sections: For a restructure proposal, the over-budget section rows as formatted strings.
        simulation: For a restructure proposal under `--simulate`, the simulated split's measured rank
            change. None when the simulation was not asked for or nothing was measurable.
    """

    model_config = ConfigDict(frozen=True)

    case_names: tuple[str, ...]
    recall_deficit: float
    counterfactual: CounterfactualResult | None
    mined_links: tuple[MinedLink, ...]
    oversized_sections: tuple[str, ...]
    simulation: SimulationResult | None = None


class SchemaEditProposal(BaseModel):
    """One typed schema edit the schema accepts, with its evidence, predictions and migration brief.

    Attributes:
        id: A stable slug identifying the proposal, for example `add-relation-extends-on-note`.
        op: The typed spec edit.
        evidence: The measured evidence backing the proposal.
        predictions: The falsifiable predictions the proposal makes.
        migration: The per-page work orders, never empty for a restructure op.
        rationale: A short prose explanation of why the edit should help.
    """

    model_config = ConfigDict(frozen=True)

    id: str
    op: SchemaEditOp
    evidence: ProposalEvidence
    predictions: tuple[Prediction, ...]
    migration: tuple[PageWorkOrder, ...]
    rationale: str


class StructuralFinding(BaseModel):
    """A ranked free-form finding about a change the two typed edits cannot express, never applied as an edit.

    Attributes:
        finding_type: What the finding proposes: a new page type, a compound layer design, or a machinery idea.
            Files and JSON carry it under the key `kind`.
        title: A short title for the finding.
        body: Free-form design prose. A layer finding groups a page type and its relations into one coherent design.
        related_cases: The dataset cases the finding is grounded in.
    """

    model_config = ConfigDict(frozen=True, serialize_by_alias=True)

    finding_type: Literal["page_type", "layer", "machinery"] = Field(
        validation_alias=AliasChoices("kind", "finding_type"), serialization_alias="kind"
    )
    title: str
    body: str
    related_cases: tuple[str, ...]


class KnowledgeBaseStamp(BaseModel):
    """The indexed knowledge base's identity, so a verify run knows it is comparing against the same knowledge base.

    Attributes:
        collection: The indexed collection.
        spec_hash: The schema fingerprint from `kb.index_fingerprint`.
        manifest_digest: A sha256 over the sorted path and content hashes of `kb.index_manifest`.
        n_pages: The number of indexed pages.
    """

    model_config = ConfigDict(frozen=True)

    collection: str
    spec_hash: str
    manifest_digest: str
    n_pages: int


class CaseSnapshot(BaseModel):
    """One case's baseline result, which a verify run compares against.

    Attributes:
        case_name: The dataset case name.
        recall: The case's primary recall.
        gold_ids: The gold page ids for the case.
        cited_ids: The page ids the agent actually cited.
    """

    model_config = ConfigDict(frozen=True)

    case_name: str
    recall: float
    gold_ids: tuple[str, ...]
    cited_ids: tuple[str, ...]


class BaselineSnapshot(BaseModel):
    """The baseline evaluation run a recommendation rests on, which a verify run after the edits compares against.

    Attributes:
        dataset: The dataset the baseline was run on.
        run_id: The saved `evaluation_runs` row id, or None when the run was not saved.
        policy_source: Where the policy came from, the active policy, an explicit path, or the default.
        evidence_grade: Whether the run used a tuned policy (`residual-of-tuned`) or the shipped baseline (`raw-baseline`).
        knowledge_base: The knowledge base stamp the baseline was measured against.
        overall: The overall metric values keyed by metric name.
        cases: The per-case snapshots a verify run compares against.
    """

    model_config = ConfigDict(frozen=True)

    dataset: str
    run_id: str | None
    policy_source: Literal["active-policy", "explicit", "default"]
    evidence_grade: Literal["residual-of-tuned", "raw-baseline"]
    knowledge_base: KnowledgeBaseStamp
    overall: Mapping[str, float]
    cases: tuple[CaseSnapshot, ...]


class Recommendation(BaseModel):
    """A finished recommendation: a baseline snapshot, ranked proposals and a findings tail.

    Attributes:
        version: The recommendation file format version.
        collection: The collection the recommendation was produced for.
        created_at: When the recommendation was produced, timezone-aware.
        baseline: The baseline evaluation run the recommendation rests on.
        proposals: The ranked schema edit proposals, strongest evidence first.
        findings: The free-form findings about changes the typed edits cannot express.
    """

    model_config = ConfigDict(frozen=True)

    version: str = "0.1"
    collection: str
    created_at: datetime
    baseline: BaselineSnapshot
    proposals: tuple[SchemaEditProposal, ...]
    findings: tuple[StructuralFinding, ...]


class DraftProposal(BaseModel):
    """One draft proposal the model writes, the half of a `SchemaEditProposal` that is not measured.

    It holds only what the model writes. The gate pairs it with the measured `ProposalEvidence` to build
    the final `SchemaEditProposal`.

    Attributes:
        id: A stable slug identifying the proposal, for example `add-relation-extends-on-note`.
        op: The typed schema edit the proposal makes.
        case_names: The diagnosis case identifiers this proposal claims to explain, copied verbatim.
        rationale: A short prose explanation of why the edit should help.
        predictions: The falsifiable predictions the proposal makes.
        migration: The per-page work orders. A restructure op without them is dropped.
    """

    model_config = ConfigDict(frozen=True)

    id: str
    op: SchemaEditOp
    case_names: tuple[str, ...]
    rationale: str
    predictions: tuple[Prediction, ...]
    migration: tuple[PageWorkOrder, ...]


class DraftRecommendation(BaseModel):
    """The draft recommendation the model writes: draft proposals, a findings tail and an overall rationale.

    It holds only what the model writes. The baseline snapshot, the knowledge base stamp and each proposal's
    evidence are measured and added after it.

    Attributes:
        proposals: The draft schema edit proposals, unranked. The gate ranks them by measured evidence.
        findings: The free-form findings about changes the two typed edits cannot express.
        rationale: The overall rationale tying the proposals to the diagnosis.
    """

    model_config = ConfigDict(frozen=True)

    proposals: tuple[DraftProposal, ...]
    findings: tuple[StructuralFinding, ...]
    rationale: str


class OversizedSection(BaseModel):
    """One over-budget section row with its body, the input of the split simulation.

    Attributes:
        page_id: The owning page.
        canon: The section's canonical slug.
        tokens: The section's token count (stored, or counted at read time when the row carries none).
        body: The section's markdown body, the text the split cuts.
    """

    model_config = ConfigDict(frozen=True)

    page_id: str
    canon: str
    tokens: int
    body: str
