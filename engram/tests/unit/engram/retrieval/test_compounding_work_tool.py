"""`AuthoringTools.compounding_work`: one derivation's pending inputs, and `knowledge_base_profile`'s compounding map."""

from __future__ import annotations

import json
import re

from typing import TYPE_CHECKING

import pytest

from pydantic_ai import ModelRetry

from engram.kb.contracts import DerivationEntry, KnowledgeBaseSchema
from engram.kb.domain.schema import CompoundingSpec
from engram.testing.fakes.authoring import FakeAdminGateway
from engram.testing.specs import research_spec
from tests.unit.engram.retrieval.test_authoring_tools import _doors

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path
    from typing import Literal


def _entry(
    name: str,
    source: Mapping[str, object],
    to: tuple[str, ...],
    *,
    cap_per_run: int = 20,
    gate: Literal["none", "owner"] = "none",
    guidance: str = "",
) -> DerivationEntry:
    """One `DerivationEntry`, built through `model_validate` (the JSON alias `from` is a Python keyword)."""
    return DerivationEntry.model_validate(
        {"name": name, "from": source, "to": list(to), "cap_per_run": cap_per_run, "gate": gate, "guidance": guidance}
    )


def _spec_with(*entries: DerivationEntry) -> KnowledgeBaseSchema:
    """A freshly re-validated research spec (never the cached singleton) carrying `entries` as its compounding block."""
    base = KnowledgeBaseSchema.model_validate(research_spec().model_dump(mode="json"))
    return base.model_copy(update={"compounding": CompoundingSpec(entries=entries)})


def _note(note_id: str, status: str = "open", *, archived: bool = False) -> dict[str, object]:
    """A minimally valid `Note` payload for a sidecar fixture."""
    return {
        "id": note_id,
        "anchor": {"exact": "a span"},
        "text": "a note",
        "created_at": "2026-01-01T00:00:00Z",
        "status": status,
        "archived": archived,
    }


async def test_compounding_work_sources_plane_reuses_the_undigested_query_at_the_entrys_cap(tmp_path: Path) -> None:
    admin = FakeAdminGateway(rows=[{"source_id": "yt-abc", "provider": "intake", "item_type": "video", "title": "a talk"}])
    entry = _entry(
        "digest-sources", {"plane": "sources"}, to=("paper",), cap_per_run=5, guidance="Digest with claims quoting the chunks."
    )
    doors, _, _ = _doors(tmp_path, admin, spec=_spec_with(entry))

    result = await doors.compounding_work("research", "digest-sources")

    assert result["entry"] == {
        "name": "digest-sources",
        "to": ["paper"],
        "cap_per_run": 5,
        "gate": "none",
        "guidance": "Digest with claims quoting the chunks.",
    }
    assert result["opening_status"] == "active", "paper declares no status_values"
    assert result["rows"] == [{"source_id": "yt-abc", "provider": "intake", "item_type": "video", "title": "a talk"}]
    sql, params = admin.calls[0]
    assert "kb.sources_raw" in sql
    assert params == {"collection": "research", "limit": 5}


async def test_compounding_work_pages_plane_merges_sorts_newest_first_and_caps(tmp_path: Path) -> None:
    admin = FakeAdminGateway(
        rows=[
            {"page_id": "talk.b", "page_type": "talk", "title": "B", "status": "active", "created": "2026-08-20"},
            {"page_id": "talk.a", "page_type": "talk", "title": "A", "status": "active", "created": "2026-08-22"},
            {"page_id": "talk.c", "page_type": "talk", "title": "C", "status": "active", "created": "2026-08-21"},
        ]
    )
    entry = _entry(
        "flag-ideas",
        {"plane": "pages", "page_types": ["talk"], "within_hours": 48, "status": "active"},
        to=("flag",),
        cap_per_run=2,
        gate="owner",
        guidance="Look for a compounding idea.",
    )
    doors, _, _ = _doors(tmp_path, admin, spec=_spec_with(entry))

    result = await doors.compounding_work("research", "flag-ideas")

    assert result["opening_status"] == "pending", "flag's first declared status"
    assert result["rows"] == [
        {"page_id": "talk.a", "page_type": "talk", "title": "A", "status": "active", "source_id": "", "path": "", "findings": []},
        {"page_id": "talk.c", "page_type": "talk", "title": "C", "status": "active", "source_id": "", "path": "", "findings": []},
    ]
    sql, params = admin.calls[0]
    assert "kb.validation_store" in sql
    assert "kb.index_manifest" in sql
    assert "p.frontmatter->>'digest_of'" in sql, "the source is read through the schema's own link field"
    assert "p.page_type = :page_type" in sql
    assert "p.status <> 'skipped'" in sql
    assert "p.created >=" in sql
    assert "p.status = :status" in sql
    assert "NOT EXISTS" in sql
    assert "= ANY(:to_kinds)" in sql
    assert params == {"collection": "research", "page_type": "talk", "limit": 2, "within_hours": 48, "status": "active", "to_kinds": ["flag"]}


async def test_compounding_work_pages_plane_runs_one_query_per_declared_kind(tmp_path: Path) -> None:
    admin = FakeAdminGateway(rows=[])
    entry = _entry("flag-ideas", {"plane": "pages", "page_types": ["talk", "paper"]}, to=("flag",))
    doors, _, _ = _doors(tmp_path, admin, spec=_spec_with(entry))

    result = await doors.compounding_work("research", "flag-ideas")

    assert [params["page_type"] for _sql, params in admin.calls] == ["talk", "paper"]
    for sql, _params in admin.calls:
        assert "p.created >=" not in sql, "within_hours is unset, its clause is left out"
        assert "p.status = :status" not in sql, "status is unset, its clause is left out"
    assert result["rows"] == []


async def test_compounding_work_notes_plane_lists_open_threads_under_the_folder_capped(tmp_path: Path) -> None:
    notes_dir = tmp_path / ".agent-threads"
    (notes_dir / "docs" / "adr").mkdir(parents=True)
    (notes_dir / "docs" / "adr" / "0001-a.md.threads.json").write_text(json.dumps({"notes": [_note("n1")]}), encoding="utf-8")
    (notes_dir / "docs" / "b.md.threads.json").write_text(
        json.dumps({"notes": [_note("n2"), _note("n3", archived=True), _note("n4", "resolved")]}),
        encoding="utf-8",
    )
    (notes_dir / "docs" / "z.md.threads.json").write_text(json.dumps({"notes": [_note("n5")]}), encoding="utf-8")
    (notes_dir / "docs" / "c.md.threads.json").write_text(json.dumps({"notes": [_note("n6", "resolved")]}), encoding="utf-8")
    (notes_dir / "docs" / "broken.md.threads.json").write_text("{not valid json", encoding="utf-8")
    (notes_dir / "docs" / "shape.md.threads.json").write_text(json.dumps([]), encoding="utf-8")
    (notes_dir / "README.md.threads.json").write_text(json.dumps({"notes": [_note("n7")]}), encoding="utf-8")
    entry = _entry("link-notes", {"plane": "notes", "folder": "docs"}, to=("flag",), cap_per_run=2)
    doors, _, _ = _doors(tmp_path, spec=_spec_with(entry))

    result = await doors.compounding_work("research", "link-notes")

    assert result["rows"] == [
        {"path": "docs/adr/0001-a.md", "open_notes": 1},
        {"path": "docs/b.md", "open_notes": 1},
    ], "c.md has no open note, broken.md and shape.md are unreadable, README.md sits outside the folder, z.md loses to the cap"


async def test_compounding_work_notes_plane_finds_the_home_above_the_knowledge_base_root(tmp_path: Path) -> None:
    knowledge_base_root = tmp_path / "docs"
    (knowledge_base_root / "initiatives").mkdir(parents=True)
    notes_dir = tmp_path / ".agent-threads" / "docs" / "initiatives"
    notes_dir.mkdir(parents=True)
    (notes_dir / "x.md.threads.json").write_text(json.dumps({"notes": [_note("n1")]}), encoding="utf-8")
    (notes_dir / "latin1.md.threads.json").write_bytes(b'{"notes": [{"status": "open", "text": "caf\xe9"}]}')
    entry = _entry("notes-to-analysis", {"plane": "notes", "folder": "initiatives"}, to=("flag",))
    doors, _, _ = _doors(knowledge_base_root, spec=_spec_with(entry))

    result = await doors.compounding_work("research", "notes-to-analysis")

    assert result["rows"] == [{"path": "initiatives/x.md", "open_notes": 1}], (
        "the home is the ancestor owning .agent-threads, the path is knowledge-base-relative, and a non-UTF8 sidecar is skipped"
    )


async def test_compounding_work_of_an_unknown_name_names_every_declared_entry(tmp_path: Path) -> None:
    spec = _spec_with(
        _entry("digest-sources", {"plane": "sources"}, to=("paper",)),
        _entry("flag-ideas", {"plane": "pages", "page_types": ["talk"]}, to=("flag",)),
    )
    doors, _, _ = _doors(tmp_path, spec=spec)

    with pytest.raises(ModelRetry, match=re.escape("no derivation 'nope' in knowledge base 'research', the entries are digest-sources, flag-ideas")):
        await doors.compounding_work("research", "nope")


async def test_knowledge_base_profile_carries_compounding_dumped_by_alias(tmp_path: Path) -> None:
    entry = _entry("digest-sources", {"plane": "sources"}, to=("paper",), cap_per_run=7, gate="owner", guidance="hi")
    doors, _, _ = _doors(tmp_path, spec=_spec_with(entry))

    profile = await doors.knowledge_base_profile("research")

    assert profile["compounding"] == [
        {
            "name": "digest-sources",
            "from": {"plane": "sources", "selector": "undigested"},
            "to": ("paper",),
            "cap_per_run": 7,
            "gate": "owner",
            "guidance": "hi",
        }
    ]


async def test_pages_rows_carry_the_source_the_file_and_the_sorted_validation_findings(tmp_path: Path) -> None:
    admin = FakeAdminGateway(
        rows=[
            {
                "page_id": "paper.a.limits",
                "page_type": "paper",
                "title": "A",
                "status": "active",
                "created": "2026-10-06",
                "source_id": "arxiv-1",
                "path": "Wiki/Papers/a.limits.md",
                "findings": ["quote-not-in-source", "missing-loc", "quote-not-in-source"],
            },
            {
                "page_id": "paper.b",
                "page_type": "paper",
                "title": "B",
                "status": "active",
                "created": "2026-10-05",
                "source_id": None,
                "path": None,
                "findings": None,
            },
        ]
    )
    entry = _entry("flag-ideas", {"plane": "pages", "page_types": ["paper"]}, to=("flag",))
    doors, _, _ = _doors(tmp_path, admin, spec=_spec_with(entry))

    result = await doors.compounding_work("research", "flag-ideas")

    assert result["rows"] == [
        {
            "page_id": "paper.a.limits",
            "page_type": "paper",
            "title": "A",
            "status": "active",
            "source_id": "arxiv-1",
            "path": "Wiki/Papers/a.limits.md",
            "findings": ["missing-loc", "quote-not-in-source"],
        },
        {"page_id": "paper.b", "page_type": "paper", "title": "B", "status": "active", "source_id": "", "path": "", "findings": []},
    ]


async def test_named_page_ids_read_exactly_those_pages_whatever_their_age_or_links(tmp_path: Path) -> None:
    admin = FakeAdminGateway(rows=[])
    entry = _entry("flag-ideas", {"plane": "pages", "page_types": ["talk", "paper"], "within_hours": 26}, to=("flag",), cap_per_run=1)
    doors, _, _ = _doors(tmp_path, admin, spec=_spec_with(entry))

    await doors.compounding_work("research", "flag-ideas", page_ids=["paper.a", " ", "paper.b", "paper.a"])

    assert len(admin.calls) == 1, "one query over every page type, not one per page type"
    sql, params = admin.calls[0]
    assert "p.id = ANY(:page_ids)" in sql
    assert "p.page_type = ANY(:page_types)" in sql
    assert "p.status <> 'skipped'" in sql
    assert "NOT EXISTS" not in sql, "a page a flag already cites is still read"
    assert "p.created >=" not in sql, "no window"
    assert params == {"collection": "research", "page_types": ["talk", "paper"], "page_ids": ["paper.a", "paper.b"]}


async def test_an_empty_page_ids_list_reads_the_entrys_own_batch(tmp_path: Path) -> None:
    admin = FakeAdminGateway(rows=[])
    entry = _entry("flag-ideas", {"plane": "pages", "page_types": ["talk"], "within_hours": 26}, to=("flag",))
    doors, _, _ = _doors(tmp_path, admin, spec=_spec_with(entry))

    await doors.compounding_work("research", "flag-ideas", page_ids=[])

    sql, _params = admin.calls[0]
    assert "p.created >=" in sql
    assert "NOT EXISTS" in sql


async def test_page_ids_for_an_entry_over_sources_is_refused_naming_the_plane(tmp_path: Path) -> None:
    entry = _entry("digest-sources", {"plane": "sources"}, to=("paper",))
    doors, _, _ = _doors(tmp_path, FakeAdminGateway(rows=[]), spec=_spec_with(entry))

    with pytest.raises(ModelRetry, match=r"page_ids narrows only an entry over pages, and 'digest-sources' reads sources"):
        await doors.compounding_work("research", "digest-sources", page_ids=["paper.a"])
