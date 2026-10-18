"""Unit tests for the frozen write-path row models (`src/engram/indexing/domain/rows.py`).

These pin the row contract: the Load-path row builds construct frozen pydantic
models (mirroring the `EdgeRow` exemplar) whose field set tracks the ORM columns,
so a renamed or removed ORM column fails the model and pyright instead of silently
producing a wrong dict. Three properties are pinned, all pure (no IO, no DB):

- **frozen** — every row model rejects mutation (`ValidationError`).
- **the column-parity guard** — each model's field-name set equals its ORM
  table's column set minus the columns the row dicts never carry (`embedding`,
  the `bigserial` `id`, server-default timestamps). Deriving the ORM side from
  `__table__.columns` makes a renamed/dropped ORM column fail this test.
- **byte-identical `.model_dump()`** — representative dumps equal known-good
  literals, proving the conversion preserves the exact dict the repositories upsert.
"""

from __future__ import annotations

import pytest

from pydantic import ValidationError

from engram.indexing.contracts import ClaimORM, LinkRefORM, SectionORM
from engram.indexing.domain.rows import ClaimQualifierRow, ClaimRow, LinkRefRow, SectionRow, ValidationRow
from engram.indexing.infrastructure.orm import ClaimQualifierORM, ValidationStoreORM

# Each row model excludes the ORM columns the current row dicts never carry: the
# `embedding` vector, the `bigserial` surrogate `id`, and server-default timestamps.
# (`SectionORM`/`ClaimORM` keep their `id` — it is a `Text` primary key, not a serial.)
_EXCLUDED_COLUMNS: dict[type, frozenset[str]] = {
    SectionORM: frozenset({"embedding"}),
    ClaimORM: frozenset({"embedding"}),
    LinkRefORM: frozenset({"id"}),
    ValidationStoreORM: frozenset({"id"}),
    ClaimQualifierORM: frozenset(),
}


def _orm_columns(orm: type, excluded: frozenset[str]) -> set[str]:
    """The ORM table's column names minus the documented row-dict exclusions."""
    return {column.name for column in orm.__table__.columns} - excluded  # pyright: ignore[reportAttributeAccessIssue]


class TestFrozen:
    def test_section_row_is_frozen(self) -> None:
        row = SectionRow(
            id="p#findings",
            collection="research",
            page_id="p",
            canon="findings",
            heading="Findings",
            path="findings",
            ord=0,
            body="body",
            tokens=None,
            body_hash="h",
            summary="",
        )
        with pytest.raises(ValidationError):
            row.heading = "mutated"  # type: ignore[misc]

    def test_link_ref_row_is_frozen(self) -> None:
        row = LinkRefRow(
            collection="research",
            src_page_id="p",
            src_kind="page",
            rel="cites",
            raw_target="q",
            dst_id="q",
            dst_collection="research",
            provenance_claim=None,
        )
        with pytest.raises(ValidationError):
            row.rel = "mutated"  # type: ignore[misc]

    def test_validation_row_is_frozen(self) -> None:
        row = ValidationRow(collection="research", page_id="p", severity="error", rule="missing-quote", message="m", anchor=None)
        with pytest.raises(ValidationError):
            row.message = "mutated"  # type: ignore[misc]

    def test_claim_row_is_frozen(self) -> None:
        row = ClaimRow(
            id="p#c1",
            collection="research",
            page_id="p",
            block_id="c1",
            text="t",
            quote=None,
            loc=None,
            loc_path=None,
            kind="",
            subject_id=None,
            subject_collection=None,
            fields={},
            status="active",
        )
        with pytest.raises(ValidationError):
            row.text = "mutated"  # type: ignore[misc]

    def test_claim_qualifier_row_is_frozen(self) -> None:
        row = ClaimQualifierRow(collection="research", claim_id="p#c1", qualifier="on_dataset", ord=0, target_collection="research", target_id="ent.squad")
        with pytest.raises(ValidationError):
            row.target_id = "mutated"  # type: ignore[misc]


class TestColumnParity:
    """Each row model's field set must equal its ORM columns minus the exclusions."""

    def test_section_row_matches_section_orm(self) -> None:
        assert set(SectionRow.model_fields) == _orm_columns(SectionORM, _EXCLUDED_COLUMNS[SectionORM])

    def test_claim_row_matches_claim_orm(self) -> None:
        assert set(ClaimRow.model_fields) == _orm_columns(ClaimORM, _EXCLUDED_COLUMNS[ClaimORM])

    def test_link_ref_row_matches_link_ref_orm(self) -> None:
        assert set(LinkRefRow.model_fields) == _orm_columns(LinkRefORM, _EXCLUDED_COLUMNS[LinkRefORM])

    def test_validation_row_matches_validation_store_orm(self) -> None:
        assert set(ValidationRow.model_fields) == _orm_columns(ValidationStoreORM, _EXCLUDED_COLUMNS[ValidationStoreORM])

    def test_claim_qualifier_row_matches_claim_qualifier_orm(self) -> None:
        assert set(ClaimQualifierRow.model_fields) == _orm_columns(ClaimQualifierORM, _EXCLUDED_COLUMNS[ClaimQualifierORM])


class TestModelDumpByteIdentical:
    """A representative dump must equal the exact dict the repositories upsert today."""

    def test_section_row_dump(self) -> None:
        dumped = SectionRow(
            id="p#approach",
            collection="research",
            page_id="p",
            canon=None,
            heading="Approach",
            path="approach",
            ord=2,
            body="the body",
            tokens=None,
            body_hash="bh",
            summary="the body",
        ).model_dump()
        assert dumped == {
            "id": "p#approach",
            "collection": "research",
            "page_id": "p",
            "canon": None,
            "heading": "Approach",
            "path": "approach",
            "ord": 2,
            "body": "the body",
            "tokens": None,
            "body_hash": "bh",
            "summary": "the body",
        }

    def test_claim_row_dump_keeps_the_declared_text_fields_as_one_mapping(self) -> None:
        dumped = ClaimRow(
            id="doc.handbook#c1",
            collection="handbooks",
            page_id="doc.handbook",
            block_id="c1",
            text="The policy binds both teams.",
            quote=None,
            loc="§2",
            loc_path="s2",
            kind="",
            subject_id=None,
            subject_collection=None,
            fields={"jurisdiction": "EU", "scope": "tenants"},
            status="active",
        ).model_dump()
        assert dumped == {
            "id": "doc.handbook#c1",
            "collection": "handbooks",
            "page_id": "doc.handbook",
            "block_id": "c1",
            "text": "The policy binds both teams.",
            "quote": None,
            "loc": "§2",
            "loc_path": "s2",
            "kind": "",
            "subject_id": None,
            "subject_collection": None,
            "fields": {"jurisdiction": "EU", "scope": "tenants"},
            "status": "active",
        }

    def test_link_ref_row_dump(self) -> None:
        dumped = LinkRefRow(
            collection="research",
            src_page_id="paper.x",
            src_kind="claim",
            rel="outperforms",
            raw_target="paper.y",
            dst_id="paper.y",
            dst_collection="research",
            provenance_claim="paper.x#c1",
        ).model_dump()
        assert dumped == {
            "collection": "research",
            "src_page_id": "paper.x",
            "src_kind": "claim",
            "rel": "outperforms",
            "raw_target": "paper.y",
            "dst_id": "paper.y",
            "dst_collection": "research",
            "provenance_claim": "paper.x#c1",
        }

    def test_validation_row_dump(self) -> None:
        dumped = ValidationRow(
            collection="research",
            page_id="paper.x",
            severity="warning",
            rule="comparative-missing-qualifiers",
            message="missing on_task",
            anchor="^c3",
        ).model_dump()
        assert dumped == {
            "collection": "research",
            "page_id": "paper.x",
            "severity": "warning",
            "rule": "comparative-missing-qualifiers",
            "message": "missing on_task",
            "anchor": "^c3",
        }
