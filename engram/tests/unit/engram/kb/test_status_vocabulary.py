"""Per-page_type status vocabularies: spec-declared values validate; defaults hold when undeclared."""

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


class TestStatusVocabulary:
    def test_spec_declared_status_parses(self) -> None:
        fm = parse_frontmatter({"id": "adr-0009-x", "type": "adr", "title": "T", "status": "accepted"}, _SPEC)
        assert fm.status == "accepted"

    def test_undeclared_kind_keeps_the_default_eight(self) -> None:
        fm = parse_frontmatter({"id": "mu.x", "type": "memory-unit", "title": "T", "status": "active"}, _SPEC)
        assert fm.status == "active"

    def test_value_outside_the_kind_vocabulary_is_rejected(self) -> None:
        with pytest.raises(FrontmatterError, match="status"):
            parse_frontmatter({"id": "adr-0010-y", "type": "adr", "title": "T", "status": "stub"}, _SPEC)

    def test_default_vocabulary_rejects_unknown_status(self) -> None:
        with pytest.raises(FrontmatterError, match="status"):
            parse_frontmatter({"id": "mu.z", "type": "memory-unit", "title": "T", "status": "bogus"}, _SPEC)
