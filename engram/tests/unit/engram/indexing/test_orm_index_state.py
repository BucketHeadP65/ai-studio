"""Unit tests for the index-private incremental-ingestion ORM models.

Asserts the mapper configuration — table names, the `kb` schema, the primary-key column sets,
the expected columns, and `link_refs.src_page_id`'s cascading foreign key to `kb.pages.id` —
without a database (persistence is each model's own migration's and the repositories' integration
concern). The four from `alembic/versions/0006_compile_state.py` plus `PageTypeContractORM` from
`alembic/versions/0036_page_type_contracts.py`.
"""

from __future__ import annotations

from engram.indexing.contracts import IndexFingerprintORM, IndexManifestORM, LinkRefORM
from engram.indexing.infrastructure.orm import PageTypeContractORM, ValidationStoreORM


class TestIndexManifestORM:
    def test_table_name_and_schema(self):
        assert IndexManifestORM.__tablename__ == "index_manifest"
        assert IndexManifestORM.__table__.schema == "kb"

    def test_composite_primary_key_is_collection_path(self):
        assert {column.name for column in IndexManifestORM.__table__.primary_key} == {"collection", "path"}

    def test_has_the_manifest_columns(self):
        columns = set(IndexManifestORM.__table__.columns.keys())

        assert {"collection", "path", "kind", "content_hash", "identity_hash", "page_id", "source_key", "indexed_at"} <= columns


class TestLinkRefORM:
    def test_table_name_and_schema(self):
        assert LinkRefORM.__tablename__ == "link_refs"
        assert LinkRefORM.__table__.schema == "kb"

    def test_id_is_the_primary_key(self):
        assert {column.name for column in LinkRefORM.__table__.primary_key} == {"id"}

    def test_has_the_link_ref_columns(self):
        columns = set(LinkRefORM.__table__.columns.keys())

        assert {"id", "collection", "src_page_id", "src_kind", "rel", "raw_target", "dst_id", "provenance_claim"} <= columns

    def test_src_page_id_cascades_from_memory_pages(self):
        foreign_keys = [fk for fk in LinkRefORM.__table__.foreign_keys if fk.parent.name == "src_page_id"]

        assert len(foreign_keys) == 1
        assert foreign_keys[0].target_fullname == "kb.pages.id"
        assert foreign_keys[0].ondelete == "CASCADE"


class TestValidationStoreORM:
    def test_table_name_and_schema(self):
        assert ValidationStoreORM.__tablename__ == "validation_store"
        assert ValidationStoreORM.__table__.schema == "kb"

    def test_id_is_the_primary_key(self):
        assert {column.name for column in ValidationStoreORM.__table__.primary_key} == {"id"}

    def test_has_the_validation_store_columns(self):
        columns = set(ValidationStoreORM.__table__.columns.keys())

        assert {"id", "collection", "page_id", "severity", "rule", "message", "anchor"} <= columns

    def test_page_id_is_deliberately_not_a_foreign_key(self):
        assert ValidationStoreORM.__table__.foreign_keys == set()


class TestIndexFingerprintORM:
    def test_table_name_and_schema(self):
        assert IndexFingerprintORM.__tablename__ == "index_fingerprint"
        assert IndexFingerprintORM.__table__.schema == "kb"

    def test_collection_is_the_primary_key(self):
        assert {column.name for column in IndexFingerprintORM.__table__.primary_key} == {"collection"}

    def test_has_the_fingerprint_columns(self):
        columns = set(IndexFingerprintORM.__table__.columns.keys())

        assert {"collection", "indexer_version", "spec_hash", "vocabulary_hash", "embed_model", "indexed_at"} <= columns


class TestPageTypeContractORM:
    def test_table_name_and_schema(self):
        assert PageTypeContractORM.__tablename__ == "page_type_contracts"
        assert PageTypeContractORM.__table__.schema == "kb"

    def test_composite_primary_key_is_collection_kind(self):
        assert {column.name for column in PageTypeContractORM.__table__.primary_key} == {"collection", "page_type"}

    def test_has_the_contract_column(self):
        columns = set(PageTypeContractORM.__table__.columns.keys())

        assert {"collection", "page_type", "contract"} <= columns
