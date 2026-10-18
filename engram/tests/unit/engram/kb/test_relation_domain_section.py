"""Unit tests for `RelationDecl.domain_section` (`src/engram/kb/domain/schema.py`).

`domain_section`, when set, must name a canonical section slug of the relation's domain page
type — `KnowledgeBaseSchema` cross-checks it the same way it cross-checks `edge_fields` and
`path_fields` against their page type's declared fields.
"""

from __future__ import annotations

import pytest

from pydantic import ValidationError

from engram.kb.contracts import KnowledgeBaseSchema

_PAGE_TYPES = [
    {"page_type": "moc", "id_prefix": "moc.", "canonical_sections": [{"slug": "tl_dr", "heading": "TL;DR"}, "map"]},
    {"page_type": "entity", "id_prefix": "ent."},
]


def _spec(domain: str, domain_section: str) -> dict[str, object]:
    return {
        "slug": "test-kb",
        "page_types": _PAGE_TYPES,
        "relations": [{"slug": "maps", "domain": domain, "range": "any page", "domain_section": domain_section}],
    }


def test_a_domain_section_naming_a_canonical_section_of_its_domain_page_type_is_valid() -> None:
    spec = KnowledgeBaseSchema.model_validate(_spec("moc", "map"))

    assert next(r for r in spec.relations if r.slug == "maps").domain_section == "map"


def test_domain_section_defaults_to_unset() -> None:
    spec = KnowledgeBaseSchema.model_validate(
        {"slug": "test-kb", "page_types": _PAGE_TYPES, "relations": [{"slug": "cites", "domain": "entity", "range": "entity"}]}
    )

    assert next(r for r in spec.relations if r.slug == "cites").domain_section is None


def test_a_domain_section_not_a_canonical_section_of_its_domain_page_type_is_refused() -> None:
    with pytest.raises(ValidationError, match="domain_section"):
        KnowledgeBaseSchema.model_validate(_spec("moc", "findings"))


def test_a_domain_section_whose_domain_names_an_undeclared_page_type_is_refused() -> None:
    with pytest.raises(ValidationError, match="domain_section"):
        KnowledgeBaseSchema.model_validate(_spec("ghost", "map"))
