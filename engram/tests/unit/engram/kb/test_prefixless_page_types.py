"""A page type with an empty id_prefix accepts free-form ids; prefixed page_types keep their constraint."""

import pytest

from engram.kb.contracts import KnowledgeBaseSchema, parse_frontmatter
from engram.kb.exceptions import FrontmatterError

_SPEC = KnowledgeBaseSchema.model_validate(
    {
        "slug": "estate",
        "page_types": [
            {"page_type": "adr", "id_prefix": "", "status_values": ["proposed", "accepted", "deprecated", "superseded"]},
            {"page_type": "memory-unit", "id_prefix": "mu."},
        ],
    }
)


class TestPrefixlessPageTypes:
    def test_free_form_id_parses_for_prefixless_kind(self) -> None:
        fm = parse_frontmatter({"id": "adr-0009-consolidation", "type": "adr", "title": "T", "status": "accepted"}, _SPEC)
        assert fm.id == "adr-0009-consolidation"

    def test_prefixed_kind_still_enforces_its_prefix(self) -> None:
        with pytest.raises(FrontmatterError, match="must start with"):
            parse_frontmatter({"id": "wrong", "type": "memory-unit", "title": "T", "status": "active"}, _SPEC)
