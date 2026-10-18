r"""Unit tests for the pure embed-input assembler (`src/engram/indexing/domain/card.py`).

These pin the embedding-input contract table (see `docs/reference/compile.md`)
and the re-embed hash invariant (invariant 3 — re-embed only on content change):

| Table       | Embed input                                                          |
|-------------|----------------------------------------------------------------------|
| `pages`   | `"{page_type}: {title} — {summary}"` + one facet line (breadcrumb)      |
| `sections`| `"{page_title} > {heading_path}\n\n{body}"`                        |
| `claims`  | `"{paper_title}: {text}"` + compact `[link targets; text fields]` |
| `raw_chunks`| `"{paper_title} > §{section_path} {heading}\n\n{body}"`          |

card_text is asserted for ≥2 page types; the content hash is sha256 hex and stable
(same input → same digest; different input → different digest). Pure functions, no IO.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import tiktoken

from engram.indexing.contracts import content_hash
from engram.indexing.domain.card import EMBED_TOKEN_BUDGET, card_text, claim_embed_input, raw_chunk_embed_input, section_embed_input
from engram.kb.contracts import ClaimBlock, KnowledgeBaseNote, Section, UnitLink, parse_frontmatter
from engram.kb.contracts import parse as parse_note
from engram.testing.knowledge_base_builder import GOLDEN_LIGHTRAG_PAPER
from engram.testing.specs import research_spec

if TYPE_CHECKING:
    from engram.indexing.domain.models import ResolvedClaim

_ENCODING = tiktoken.get_encoding("cl100k_base")


def _token_len(text: str) -> int:
    """Re-encode `text` and count its `cl100k_base` tokens — the budget assertion primitive."""
    return len(_ENCODING.encode(text, disallowed_special=()))


_RESEARCH_SPEC = research_spec()


_CARD_FACETS = tuple(facet.key for facet in _RESEARCH_SPEC.facets if facet.card)

_RESEARCH_CLAIM_FIELDS = (*_RESEARCH_SPEC.claims.link_fields, *_RESEARCH_SPEC.claims.text_fields) if _RESEARCH_SPEC.claims else ()


def _paper(
    *,
    summary: str | None = "Graph-indexed RAG.",
    tasks: tuple[str, ...] = ("long-doc-qa",),
    stages: tuple[str, ...] = (),
    domains: tuple[str, ...] = (),
) -> KnowledgeBaseNote:
    data: dict[str, object] = {
        "id": "paper.lightrag-2025",
        "type": "paper",
        "title": "LightRAG",
        "summary": summary,
        "status": "draft",
        "paper_type": "method",
        "year": 2025,
        "zotero_key": "K",
        "source_hash": "h",
        "tasks": list(tasks),
        "stages": list(stages),
        "domains": list(domains),
    }
    frontmatter = parse_frontmatter(data, _RESEARCH_SPEC)
    return KnowledgeBaseNote(frontmatter=frontmatter, sections=(), claims=(), source_path="Memory/Papers/lightrag-2025.md")


def _entity(*, summary: str | None = "A retrieval technique.", stages: tuple[str, ...] = ("retrieval",)) -> KnowledgeBaseNote:
    data: dict[str, object] = {
        "id": "ent.lightrag",
        "type": "entity",
        "title": "LightRAG",
        "summary": summary,
        "status": "draft",
        "entity_type": "technique",
        "stages": list(stages),
    }
    frontmatter = parse_frontmatter(data, _RESEARCH_SPEC)
    return KnowledgeBaseNote(frontmatter=frontmatter, sections=(), claims=(), source_path="Memory/Entities/lightrag.md")


class TestCardText:
    """`card_text` = `"{page_type}: {title} — {summary}"` + one breadcrumb facet line."""

    def test_paper_card_text_header(self):
        text = card_text(_paper(), card_facets=_CARD_FACETS)

        assert text.startswith("paper: LightRAG — Graph-indexed RAG.")

    def test_paper_card_text_has_facet_line(self):
        text = card_text(_paper(tasks=("long-doc-qa", "sensemaking-qa"), stages=("retrieval",)), card_facets=_CARD_FACETS)

        lines = text.splitlines()
        assert len(lines) == 2
        assert "long-doc-qa" in lines[1]
        assert "retrieval" in lines[1]

    def test_entity_card_text_header(self):
        text = card_text(_entity(), card_facets=_CARD_FACETS)

        assert text.startswith("entity: LightRAG — A retrieval technique.")

    def test_card_text_without_summary_omits_dash(self):
        text = card_text(_paper(summary=None, tasks=()), card_facets=_CARD_FACETS)

        assert text.splitlines()[0] == "paper: LightRAG"

    def test_card_text_without_facets_is_header_only(self):
        text = card_text(_paper(summary="S.", tasks=(), stages=()), card_facets=_CARD_FACETS)

        assert text == "paper: LightRAG — S."

    def test_the_card_prints_the_marked_facets_in_the_order_given(self):
        note = _paper(tasks=("long-doc-qa",), stages=("retrieval",), domains=("biomedical",))

        assert card_text(note, card_facets=("tasks", "domains", "stages")).endswith("long-doc-qa / biomedical / retrieval")

    def test_a_card_with_no_marked_facets_has_no_facet_line(self):
        assert "\n" not in card_text(_paper(tasks=("long-doc-qa",)), card_facets=())

    def test_the_research_template_marks_tasks_domains_and_stages_for_the_card(self):
        assert _CARD_FACETS == ("tasks", "domains", "stages")


class TestGoldenCardTextByteIdentity:
    """`card_text` for a real golden note stays byte-identical through the identity-migration slice.

    `_facet_line` now reads through `BaseFrontmatter.facet_values` instead of `getattr` — this
    pins the output unchanged for `tasks`/`stages`/`domains`, the knowledge-base-qualified-identity spec's
    Phase A equivalence gate (a changed embed-input byte would void the golden-knowledge-base re-embed
    invariant).
    """

    def test_lightrag_paper_card_text_is_unchanged(self):
        note = parse_note(GOLDEN_LIGHTRAG_PAPER.read_text(), source_path="Memory/Papers/lightrag-2025.md", spec=research_spec())

        text = card_text(note, card_facets=_CARD_FACETS)

        assert text == (
            "paper: LightRAG: Simple and Fast Retrieval-Augmented Generation — Graph-indexed RAG with "
            "dual-level (entity+theme) retrieval and incremental index updates. LLM-judged win-rate gains "
            "over NaiveRAG/RQ-RAG/HyDE/GraphRAG on UltraDomain long corpora (esp. Legal), ~100-token "
            "single-call retrieval — but near-tie vs GraphRAG on Mix.\n"
            "long-doc-qa / sensemaking-qa / mixed / indexing / retrieval"
        )
        assert content_hash(text) == "b7ade7a7ba8d17905d47da2a1b79824336671b991a02bdc0135ee9b9ba9107a8"


class TestSectionEmbedInput:
    r"""`"{page_title} > {heading_path}\n\n{body}"` — the breadcrumb-prefixed section."""

    def test_section_input_has_breadcrumb_and_body(self):
        section = Section(heading="Findings", level=2, path="findings", canon="findings", body="LightRAG wins.", ord=0)

        text = section_embed_input("LightRAG", section)

        assert text == "LightRAG > findings\n\nLightRAG wins."

    def test_nested_section_uses_dotted_path(self):
        section = Section(heading="Graph indexing", level=3, path="approach.graph_indexing", canon=None, body="...", ord=1)

        text = section_embed_input("LightRAG", section)

        assert text.startswith("LightRAG > approach.graph_indexing\n\n")

    def test_preamble_uses_its_reader_facing_heading_not_its_reserved_id(self):
        section = Section(
            kind="preamble", heading="Introduction", level=0, path="__preamble__", canon=None, body="Opening thesis.", ord=0
        )

        text = section_embed_input("LightRAG", section)

        assert text == "LightRAG > Introduction\n\nOpening thesis."


class TestClaimEmbedInput:
    """`"{paper_title}: {text}"` + compact `[link targets; text fields]` suffix, in the declared field order."""

    def test_claim_input_prefixes_paper_title(self):
        claim = ClaimBlock(
            anchor="^c1",
            text="LightRAG beats GraphRAG.",
            loc="§4.2",
            kind="benchmark-result",
            fields={"condition": "hybrid mode"},
            links={"on_dataset": (UnitLink(target="ultradomain"),), "metric": (UnitLink(target="win-rate"),)},
        )
        resolved = _resolved_claim(claim)

        text = claim_embed_input("LightRAG", resolved, claim_fields=_RESEARCH_CLAIM_FIELDS)

        assert text.startswith("LightRAG: LightRAG beats GraphRAG.")
        assert "[ultradomain; win-rate; hybrid mode]" in text

    def test_claim_input_without_qualifiers_has_no_suffix(self):
        claim = ClaimBlock(anchor="^c1", text="A bare claim.", loc="§1", kind="observation")
        resolved = _resolved_claim(claim)

        text = claim_embed_input("LightRAG", resolved, claim_fields=_RESEARCH_CLAIM_FIELDS)

        assert text == "LightRAG: A bare claim."

    def test_the_suffix_follows_the_declared_order_not_the_written_order(self):
        claim = ClaimBlock(
            anchor="^c1",
            text="The policy binds both teams.",
            loc="§2",
            fields={"scope": "tenants", "jurisdiction": "EU"},
            links={"governed_by": (UnitLink(target="policy"),), "applies_to": (UnitLink(target="team-a"), UnitLink(target="team-b"))},
        )

        text = claim_embed_input("Handbook", _resolved_claim(claim), claim_fields=("applies_to", "governed_by", "jurisdiction", "scope"))

        assert text == "Handbook: The policy binds both teams. [team-a; team-b; policy; EU; tenants]"

    def test_a_field_the_order_leaves_out_follows_in_the_claims_own_order_and_an_empty_one_adds_nothing(self):
        claim = ClaimBlock(
            anchor="^c1",
            text="The policy binds both teams.",
            loc="§2",
            fields={"scope": "tenants", "remark": "", "audience": "staff", "jurisdiction": "EU"},
        )

        text = claim_embed_input("Handbook", _resolved_claim(claim), claim_fields=("jurisdiction",))

        assert text == "Handbook: The policy binds both teams. [EU; tenants; staff]"


class TestRawChunkEmbedInput:
    r"""`"{paper_title} > §{section_path} {heading}\n\n{body}"` — the chunk breadcrumb."""

    def test_raw_chunk_input_has_section_breadcrumb(self):
        text = raw_chunk_embed_input("LightRAG", section_path="s4.s1", heading="Datasets", body="600k tokens.")

        assert text == "LightRAG > §s4.s1 Datasets\n\n600k tokens."

    def test_raw_chunk_input_without_path_omits_section_marker(self):
        text = raw_chunk_embed_input("LightRAG", section_path=None, heading="Intro", body="...")

        assert text == "LightRAG > Intro\n\n..."


class TestEmbedInputTokenBudget:
    """Embed inputs are truncated token-exactly to `EMBED_TOKEN_BUDGET` (`cl100k_base`).

    Raw mirror chunks (giant preambles / unnumbered spans) can blow past the
    embedding model's 8192-token input cap; the builders truncate the embed input
    to a token-exact budget so the model never sees an over-budget input, and the
    truncated text is what gets hashed (invariant 3 stays stable). Inputs within
    budget pass through byte-identical.
    """

    def test_budget_is_under_the_models_8192_token_cap(self):
        assert EMBED_TOKEN_BUDGET == 8_000
        assert EMBED_TOKEN_BUDGET < 8192

    def test_undersized_raw_chunk_input_passes_through_byte_identical(self):
        text = raw_chunk_embed_input("LightRAG", section_path="s1", heading="Intro", body="short body")

        assert text == "LightRAG > §s1 Intro\n\nshort body"
        assert _token_len(text) <= EMBED_TOKEN_BUDGET

    def test_oversized_raw_chunk_input_truncates_to_exactly_the_token_budget(self):
        body = " ".join(f"token{i}" for i in range(EMBED_TOKEN_BUDGET * 2))

        text = raw_chunk_embed_input("LightRAG", section_path="s1", heading="Intro", body=body)

        # Re-encode the *result* and assert it is exactly the budget in tokens.
        assert _token_len(text) == EMBED_TOKEN_BUDGET

    def test_oversized_section_input_truncates_to_exactly_the_token_budget(self):
        body = " ".join(f"word{i}" for i in range(EMBED_TOKEN_BUDGET * 2))
        section = Section(heading="Findings", level=2, path="findings", canon="findings", body=body, ord=0)

        text = section_embed_input("LightRAG", section)

        assert _token_len(text) == EMBED_TOKEN_BUDGET

    def test_oversized_card_text_truncates_to_exactly_the_token_budget(self):
        summary = " ".join(f"summary{i}" for i in range(EMBED_TOKEN_BUDGET * 2))

        text = card_text(_paper(summary=summary, tasks=()), card_facets=_CARD_FACETS)

        assert _token_len(text) == EMBED_TOKEN_BUDGET

    def test_oversized_claim_input_truncates_to_exactly_the_token_budget(self):
        long_text = " ".join(f"claim{i}" for i in range(EMBED_TOKEN_BUDGET * 2))
        claim = ClaimBlock(anchor="^c1", text=long_text, loc="§1", kind="observation")
        resolved = _resolved_claim(claim)

        text = claim_embed_input("LightRAG", resolved, claim_fields=_RESEARCH_CLAIM_FIELDS)

        assert _token_len(text) == EMBED_TOKEN_BUDGET

    def test_truncation_is_deterministic(self):
        body = " ".join(f"chunk{i}" for i in range(EMBED_TOKEN_BUDGET * 2))

        first = raw_chunk_embed_input("LightRAG", section_path="s1", heading="H", body=body)
        second = raw_chunk_embed_input("LightRAG", section_path="s1", heading="H", body=body)

        assert first == second


class TestTruncatedHashStability:
    """Hash gating holds through the truncation: it changes iff the truncated text changes (invariant 3)."""

    def test_same_oversized_input_yields_same_hash(self):
        body = " ".join(f"hash{i}" for i in range(EMBED_TOKEN_BUDGET * 2))

        first = content_hash(raw_chunk_embed_input("LightRAG", section_path="s1", heading="H", body=body))
        second = content_hash(raw_chunk_embed_input("LightRAG", section_path="s1", heading="H", body=body))

        assert first == second

    def test_hash_changes_when_the_truncated_text_changes(self):
        # Diverging *within* the budget changes the truncated text → the hash differs.
        a = raw_chunk_embed_input("LightRAG", section_path="s1", heading="H", body="alpha body")
        b = raw_chunk_embed_input("LightRAG", section_path="s1", heading="H", body="beta body")

        assert content_hash(a) != content_hash(b)

    def test_hash_unchanged_when_only_content_past_the_budget_differs(self):
        # Two inputs identical through the token budget but diverging only after it
        # truncate to the same text → the same hash. That is the intended
        # consequence of gating on the truncated (embedded) text.
        prefix = " ".join(f"prefix{i}" for i in range(EMBED_TOKEN_BUDGET * 2))
        a = raw_chunk_embed_input("P", section_path=None, heading=None, body=prefix + " AAA")
        b = raw_chunk_embed_input("P", section_path=None, heading=None, body=prefix + " BBB")

        assert content_hash(a) == content_hash(b)


class TestContentHash:
    """The content hash is a stable sha256 hex digest of the embed input."""

    def test_hash_is_sha256_hex(self):
        digest = content_hash("hello")

        assert len(digest) == 64
        assert all(c in "0123456789abcdef" for c in digest)

    def test_same_input_same_hash(self):
        assert content_hash("LightRAG: A claim.") == content_hash("LightRAG: A claim.")

    def test_different_input_different_hash(self):
        assert content_hash("LightRAG: A claim.") != content_hash("LightRAG: A different claim.")

    def test_known_vector(self):
        import hashlib

        assert content_hash("hello") == hashlib.sha256(b"hello").hexdigest()


def _resolved_claim(claim: ClaimBlock) -> ResolvedClaim:
    """Resolve a single-claim paper note and return its one resolved claim (the card input shape)."""
    from engram.indexing.contracts import resolve

    note = KnowledgeBaseNote(
        frontmatter=parse_frontmatter(
            {
                "id": "paper.lightrag-2025",
                "type": "paper",
                "title": "LightRAG",
                "status": "draft",
                "paper_type": "method",
                "year": 2025,
                "zotero_key": "K",
                "source_hash": "h",
            },
            _RESEARCH_SPEC,
        ),
        sections=(),
        claims=(claim,),
        source_path="Memory/Papers/lightrag-2025.md",
    )
    return resolve((note,)).notes[0].claims[0]
