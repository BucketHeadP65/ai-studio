"""A knowledge base's own claim fields round-trip through parse, index and `api.claims`.

The schema here declares one page type whose claims carry a text field (`jurisdiction`), a link field
(`applies_to`) and an edge relation (`governs`) of its own. One page with two claims is indexed into a
freshly migrated database and read back through the read-only `kb_reader` role. Each claim's text field
comes back in `api.claims.fields`, each link field as its target pages' titles in written order in
`api.claims.links`, and the claim's edge carries the text field in its qualifiers. The database test skips
when Docker is unavailable (the `migrated` fixture raises `pytest.skip`).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import NullPool, text
from sqlalchemy.ext.asyncio import create_async_engine

from engram.indexing.contracts import (
    FilesystemKnowledgeBaseReader,
    IndexKnowledgeBase,
    IndexKnowledgeBaseHandler,
    SqlAlchemyKnowledgeBaseUnitOfWork,
)
from engram.indexing.infrastructure import orm
from engram.init.contracts import scaffold
from engram.kb.contracts import (
    CanonicalSection,
    ClaimGrammarSpec,
    KnowledgeBaseSchema,
    PageTypeSpec,
    RelationDecl,
    VocabEntry,
    parse,
    render,
)
from engram.retrieval.infrastructure.gateway import ReadOnlySqlGateway
from engram.testing.fakes.embedding import FakeEmbedding
from engram.testing.fakes.uow import FakeValidationReportSink
from engram.testing.knowledge_base_builder import KnowledgeBaseBuilder

if TYPE_CHECKING:
    from pathlib import Path

    from engram.indexing.contracts import IndexReport

_COLLECTION = "statutes"

_PAGE_PATH = "Units/provision/data-retention.md"

_PAGE = """---
id: provision.data-retention
type: provision
title: Data retention
status: draft
---

## Findings

- Personal data is kept no longer than its purpose needs. ^c1
    - loc:: Article 5(1)(e)
    - quote:: "kept for no longer than is necessary"
    - kind:: holding
    - governs:: [[consent]]
    - applies_to:: [[portability]], [[erasure]]
    - jurisdiction:: EU
- A controller records each retention period. ^c2
    - loc:: Article 30
    - kind:: holding
    - applies_to:: [[consent]]
    - jurisdiction:: UK
"""

# Each link target's file stem and title. A title differs from its stem so a served title is never an id.
_TARGETS = {"consent": "Consent", "portability": "Right to portability", "erasure": "Right to erasure"}

# The columns `api.claims` serves, in view order.
_CLAIM_COLUMNS = (
    "claim_id",
    "collection",
    "page_id",
    "page_title",
    "subject_name",
    "text",
    "quote",
    "loc",
    "kind",
    "fields",
    "links",
    "status",
)


def _spec() -> KnowledgeBaseSchema:
    """One page type whose claims declare a text field, a link field and an edge relation of their own."""
    return KnowledgeBaseSchema(
        slug=_COLLECTION,
        page_types=(
            PageTypeSpec(
                page_type="provision", id_prefix="provision.", canonical_sections=(CanonicalSection(slug="findings"),), claims_enabled=True
            ),
        ),
        relations=(RelationDecl(slug="governs", domain="provision", range="provision", description="The provision governs the target"),),
        claim_types=(VocabEntry(value="holding", definition="What a provision holds"),),
        claims=ClaimGrammarSpec(
            section="findings", edge_relations=("governs",), multilink_fields=("applies_to",), text_scalar_fields=("jurisdiction",)
        ),
    )


def _target_page(stem: str, title: str) -> str:
    """A claimless provision page a claim links to."""
    return f"---\nid: provision.{stem}\ntype: provision\ntitle: {title}\nstatus: draft\n---\n"


def _knowledge_base(root: Path, spec: KnowledgeBaseSchema) -> Path:
    """Write the page, its link targets, the vocabulary the schema seeds and the orientation stubs under `root`."""
    builder = KnowledgeBaseBuilder(root)
    builder.add_meta_stubs()
    builder.add_knowledge_base_schema(spec)
    for relative, body in scaffold(spec).items():
        if relative.startswith("_meta/vocabulary/"):
            (root / relative).parent.mkdir(parents=True, exist_ok=True)
            (root / relative).write_text(body, encoding="utf-8")
    for stem, title in _TARGETS.items():
        builder.add_unit_document("provision", stem, _target_page(stem, title))
    builder.add_unit_document("provision", "data-retention", _PAGE)
    return root


async def _index(knowledge_base: Path, migrated: str, spec: KnowledgeBaseSchema) -> IndexReport:
    """Run one full index of `knowledge_base` into the migrated database."""
    engine = create_async_engine(migrated, poolclass=NullPool)
    await orm.register_pgvector(engine)
    try:
        handler = IndexKnowledgeBaseHandler(
            FilesystemKnowledgeBaseReader(knowledge_base),
            FakeEmbedding(),
            lambda: SqlAlchemyKnowledgeBaseUnitOfWork(engine),
            FakeValidationReportSink(),
            spec=spec,
        )
        return await handler.handle(IndexKnowledgeBase(collection=_COLLECTION, embed=False))
    finally:
        await engine.dispose()


async def _stored_qualifier_names(migrated: str) -> set[str]:
    """Every distinct `kb.claim_qualifiers.qualifier` of the collection, read as the admin since the reader has no `kb` grant."""
    engine = create_async_engine(migrated, poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            result = await conn.execute(
                text("SELECT DISTINCT qualifier FROM kb.claim_qualifiers WHERE collection = :collection"), {"collection": _COLLECTION}
            )
            return {str(name) for name in result.scalars()}
    finally:
        await engine.dispose()


class TestDeclaredClaimFieldsRoundTrip:
    def test_the_page_renders_back_to_its_own_text(self) -> None:
        spec = _spec()

        assert render(parse(_PAGE, source_path=_PAGE_PATH, spec=spec), spec=spec) == _PAGE

    async def test_api_claims_serves_each_claims_declared_fields_and_the_edge_carries_its_text_field(
        self, tmp_path: Path, migrated: str, reader_url: str
    ) -> None:
        spec = _spec()

        report = await _index(_knowledge_base(tmp_path, spec), migrated, spec)

        assert report.validation.errors == ()
        assert report.claims == 2
        engine = create_async_engine(reader_url, poolclass=NullPool)
        try:
            gateway = ReadOnlySqlGateway(engine, collection=_COLLECTION)
            claims = await gateway.rows(
                "SELECT * FROM api.claims WHERE collection = :collection ORDER BY claim_id", {"collection": _COLLECTION}
            )
            edges = await gateway.rows(
                "SELECT src_id, rel, dst_title, qualifiers FROM api.edges WHERE src_collection = :collection AND provenance_claim IS NOT NULL",
                {"collection": _COLLECTION},
            )
        finally:
            await engine.dispose()
        assert claims.columns == _CLAIM_COLUMNS
        assert [(row["claim_id"], row["fields"], row["links"]) for row in claims.rows] == [
            ("provision.data-retention#c1", {"jurisdiction": "EU"}, {"applies_to": ["Right to portability", "Right to erasure"]}),
            ("provision.data-retention#c2", {"jurisdiction": "UK"}, {"applies_to": ["Consent"]}),
        ]
        assert [dict(row) for row in edges.rows] == [
            {"src_id": "provision.data-retention#c1", "rel": "governs", "dst_title": "Consent", "qualifiers": {"jurisdiction": "EU"}}
        ]
        assert await _stored_qualifier_names(migrated) == {"applies_to"}
