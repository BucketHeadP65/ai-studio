"""Frozen write-path row models for the Load stage, one per `kb.*` table.

Each model matches one table's columns in `engram.indexing.infrastructure.orm`, and the Load stage
builds the repository row dict by `model_dump`. A renamed or removed ORM column makes the model and
pyright fail rather than emit a wrong dict.

Field names and types match the ORM columns exactly, **excluding** the columns the row dicts
never carry: the `embedding` vector, the `bigserial` surrogate `id`, and server-default
timestamps. Each field is typed as the static type of the expression assigned at the build site,
so `.model_dump()` reproduces the dict value for value (`LinkRefRow.src_kind` matches
`ResolvedEdge.src_type`, `ValidationRow.severity` matches `ValidationIssue.severity`, `ClaimRow.kind`
matches `ClaimBlock.kind`).

No database or IO imports.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict

from engram.indexing.domain.models import Severity


class SectionRow(BaseModel):
    """A `kb.sections` row (an H2/H3 leaf section), matching its columns except the embedding.

    `collection` is containment: a section belongs to its page, in the same knowledge base.
    """

    model_config = ConfigDict(frozen=True)

    id: str
    collection: str
    page_id: str
    canon: str | None
    heading: str
    path: str
    ord: int
    body: str
    tokens: int | None
    body_hash: str
    summary: str


class ClaimRow(BaseModel):
    """A `kb.claims` row (a `^cN` claim block), matching its columns except the embedding.

    The targets of the claim's declared link fields are not columns here. They land as
    `ClaimQualifierRow`s, one per target, so a multilink field keeps every target.
    `collection` is containment (the claim's own page, same knowledge base). `subject_collection`
    is a citation paired with `subject_id`, `None` iff `subject_id` is `None` (the null-pairing CHECK).
    """

    model_config = ConfigDict(frozen=True)

    id: str
    collection: str
    page_id: str
    block_id: str
    text: str
    quote: str | None
    loc: str | None
    loc_path: str | None
    kind: str
    subject_id: str | None
    subject_collection: str | None
    fields: dict[str, str]
    """The claim's non-empty declared text fields by field name, each value as written."""
    status: str


class ClaimQualifierRow(BaseModel):
    """A `kb.claim_qualifiers` row: one qualifier target, named by the claim-grammar field.

    `qualifier` is the name of the declared link field the target was written under. `ord` preserves authored
    order within `(collection, claim_id, qualifier)`. A multilink field writes one row per target
    with `ord` 0, 1, 2 and so on, and a single-link field always writes `ord=0`.
    `target_collection` is a citation column, set to the claim's own collection.
    """

    model_config = ConfigDict(frozen=True)

    collection: str
    claim_id: str
    qualifier: str
    ord: int
    target_collection: str
    target_id: str


class LinkRefRow(BaseModel):
    """A `kb.link_refs` row (one outbound link target), matching its columns except the bigserial id.

    `dst_collection` is a citation paired with `dst_id`: the row's own collection when `dst_id`
    resolved, `None` when the link is dangling (the null-pairing CHECK).
    """

    model_config = ConfigDict(frozen=True)

    collection: str
    src_page_id: str
    src_kind: Literal["page", "claim"]
    rel: str
    raw_target: str
    dst_id: str | None
    dst_collection: str | None
    provenance_claim: str | None


class ValidationRow(BaseModel):
    """A `kb.validation_store` row (one persisted validation issue), matching its columns except the bigserial id."""

    model_config = ConfigDict(frozen=True)

    collection: str
    page_id: str
    severity: Severity
    rule: str
    message: str
    anchor: str | None
