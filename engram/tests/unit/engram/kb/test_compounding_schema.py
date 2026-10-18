"""The compounding block: a derivation map declared per knowledge base, validated against its own page_types."""

import pytest

from pydantic import ValidationError

from engram.kb.contracts import DerivationEntry, KnowledgeBaseSchema, PagesSelector, SourcesSelector

_BASE = {
    "slug": "alpha",
    "page_types": [{"page_type": "paper", "status_values": ["active", "superseded"]}, {"page_type": "flag", "status_values": ["pending", "accepted", "dismissed"]}],
}
_ENTRIES = [
    {"name": "digest-sources", "from": {"plane": "sources", "selector": "undigested"}, "to": ["paper"], "cap_per_run": 40},
    {"name": "flag-ideas", "from": {"plane": "pages", "page_types": ["paper"], "within_hours": 26}, "to": ["flag"], "gate": "owner", "guidance": "Ideas worth the owner's attention."},
]


def test_absent_block_means_no_entries() -> None:
    assert KnowledgeBaseSchema.model_validate(_BASE).compounding.entries == ()


def test_entries_parse_and_round_trip_by_alias() -> None:
    spec = KnowledgeBaseSchema.model_validate({**_BASE, "compounding": {"entries": _ENTRIES}})
    first, second = spec.compounding.entries
    assert isinstance(first.source, SourcesSelector)
    assert isinstance(second.source, PagesSelector)
    assert second.source.page_types == ("paper",)
    assert second.gate == "owner"
    dumped = spec.compounding.model_dump()
    assert dumped["entries"][0]["from"] == {"plane": "sources", "selector": "undigested"}
    assert KnowledgeBaseSchema.model_validate({**_BASE, "compounding": dumped}).compounding == spec.compounding


def test_entry_lookup() -> None:
    spec = KnowledgeBaseSchema.model_validate({**_BASE, "compounding": {"entries": _ENTRIES}})
    assert isinstance(spec.compounding.entry("flag-ideas"), DerivationEntry)
    assert spec.compounding.entry("nope") is None


def test_unknown_output_kind_is_refused() -> None:
    bad = [{"name": "x", "from": {"plane": "sources"}, "to": ["ghost"]}]
    with pytest.raises(ValidationError, match="ghost"):
        KnowledgeBaseSchema.model_validate({**_BASE, "compounding": {"entries": bad}})


def test_unknown_selector_kind_is_refused() -> None:
    bad = [{"name": "x", "from": {"plane": "pages", "page_types": ["ghost"]}, "to": ["flag"]}]
    with pytest.raises(ValidationError, match="ghost"):
        KnowledgeBaseSchema.model_validate({**_BASE, "compounding": {"entries": bad}})


def test_duplicate_names_are_refused() -> None:
    dup = [_ENTRIES[0], {**_ENTRIES[1], "name": "digest-sources"}]
    with pytest.raises(ValidationError, match="digest-sources"):
        KnowledgeBaseSchema.model_validate({**_BASE, "compounding": {"entries": dup}})
