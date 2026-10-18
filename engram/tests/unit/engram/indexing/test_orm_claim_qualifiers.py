"""Unit tests for `kb.claim_qualifiers`'s ORM mapping (`engram.indexing.infrastructure.orm.ClaimQualifierORM`).

Asserts the mapper configuration — table name, schema, the composite primary key, the expected
columns, and the containment (CASCADE) vs citation FK split — without a database (persistence is
the migration's and `ClaimQualifiers`'s integration concern).
"""

from __future__ import annotations

from engram.indexing.infrastructure.orm import ClaimQualifierORM


def test_table_name_and_schema():
    assert ClaimQualifierORM.__tablename__ == "claim_qualifiers"
    assert ClaimQualifierORM.__table__.schema == "kb"


def test_composite_primary_key_is_collection_claim_qualifier_ord():
    assert {column.name for column in ClaimQualifierORM.__table__.primary_key} == {"collection", "claim_id", "qualifier", "ord"}


def test_has_the_qualifier_row_columns():
    columns = set(ClaimQualifierORM.__table__.columns.keys())

    assert columns == {"collection", "claim_id", "qualifier", "ord", "target_collection", "target_id"}


def test_claim_id_is_containment_cascading_from_claims():
    foreign_keys = [fk for fk in ClaimQualifierORM.__table__.foreign_keys if fk.parent.name == "claim_id"]

    assert len(foreign_keys) == 1
    assert foreign_keys[0].target_fullname == "kb.claims.id"
    assert foreign_keys[0].ondelete == "CASCADE"


def test_target_id_is_a_citation_with_no_cascade():
    foreign_keys = [fk for fk in ClaimQualifierORM.__table__.foreign_keys if fk.parent.name == "target_id"]

    assert len(foreign_keys) == 1
    assert foreign_keys[0].target_fullname == "kb.pages.id"
    assert foreign_keys[0].ondelete is None
