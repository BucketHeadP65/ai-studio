"""Golden regression + live-knowledge_base smoke guard for the single frontmatter path.

- **Golden tests** (8 fixture notes): for each note, :func:`_parse_frontmatter_generic` must
  reproduce the golden universal fields + edge set committed to
  `fixtures/parity_golden/<page_type>.json`. Any divergence from the golden is a genuine regression.

- **Smoke test** (the live knowledge base `KNOWLEDGE_BASE_LIVE_PATH` names, skipped when it is unset): parse every page
  in the folders and files its own `knowledge-base-schema.json` declares via the public :func:`parse` API under that schema, tolerating a `NoteGrammarError` per note the same way `index/handlers.py`
  does at index time (a page-level validation issue, not an abort); assert ``swept ≥ 485`` and that
  resolver edge extraction succeeds on each successfully parsed note (no exception; edges is a list).
"""

from __future__ import annotations

import json
import os
import pathlib

import pytest

from engram.indexing.domain.resolver import _frontmatter_relations
from engram.kb.contracts import BaseFrontmatter, KnowledgeBaseSchema, NoteGrammarError, VocabularyIndex, parse
from engram.kb.domain.note import _parse_frontmatter_generic
from engram.testing.specs import research_spec

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

# The root of a live research knowledge base for the smoke sweep. The sweep skips when the variable is unset.
_LIVE_KNOWLEDGE_BASE_ENV = "KNOWLEDGE_BASE_LIVE_PATH"
_LIVE_KNOWLEDGE_BASE_NAMED = os.environ.get(_LIVE_KNOWLEDGE_BASE_ENV)
_LIVE_KNOWLEDGE_BASE = pathlib.Path(_LIVE_KNOWLEDGE_BASE_NAMED).expanduser() if _LIVE_KNOWLEDGE_BASE_NAMED else None
_FIXTURE_DIR = pathlib.Path(__file__).parent / "fixtures" / "research_notes"
_GOLDEN_DIR = pathlib.Path(__file__).parent / "fixtures" / "parity_golden"

_EMPTY_REGISTRY = VocabularyIndex(entity_types={}, relations={}, facets={}, claim_types={})


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _universal_fields(fm: BaseFrontmatter) -> dict[str, object]:
    """Extract the universal (non-type-specific) frontmatter fields for comparison."""
    return {
        "id": fm.id,
        "type": fm.type,
        "title": fm.title,
        "status": fm.status,
        "tasks": list(fm.facet_values("tasks")),
        "domains": list(fm.facet_values("domains")),
        "stages": list(fm.facet_values("stages")),
        "modalities": list(fm.facet_values("modalities")),
        "scale_regimes": list(fm.facet_values("scale_regimes")),
        "aliases": list(fm.aliases),
    }


def _edge_set_sorted(fm: BaseFrontmatter) -> list[list[str]]:
    """Return the (relation, target) list sorted for deterministic comparison."""
    return sorted([rel, lk.target] for rel, lk in _frontmatter_relations(fm))


# ---------------------------------------------------------------------------
# Golden fixture tests (always run; CI-safe)
# ---------------------------------------------------------------------------


def _golden_cases() -> list[tuple[str, pathlib.Path, dict[str, object]]]:
    """Collect (page_type, md_path, golden) triples from the committed golden fixtures."""
    cases: list[tuple[str, pathlib.Path, dict[str, object]]] = []
    for golden_path in sorted(_GOLDEN_DIR.glob("*.json")):
        golden = json.loads(golden_path.read_text(encoding="utf-8"))
        page_type: str = golden["page_type"]
        md_paths = sorted(_FIXTURE_DIR.glob(f"{page_type}-*.md"))
        if not md_paths:
            md_paths = [p for p in sorted(_FIXTURE_DIR.glob("*.md")) if p.stem.startswith(page_type)]
        if md_paths:
            cases.append((page_type, md_paths[0], golden))
    return cases


_GOLDEN_CASES = _golden_cases()


@pytest.mark.parametrize(("page_type", "note_path", "golden"), _GOLDEN_CASES, ids=lambda x: x if isinstance(x, str) else "")
def test_generic_path_matches_golden(page_type: str, note_path: pathlib.Path, golden: dict[str, object]) -> None:
    """Generic path reproduces golden universal fields + edge set for a research fixture note."""
    from core.markdown import split_frontmatter

    text = note_path.read_text(encoding="utf-8")
    data, _ = split_frontmatter(text)
    research = research_spec()

    generic_fm = _parse_frontmatter_generic(data, research, page_type)

    actual_fields = _universal_fields(generic_fm)
    expected_fields = golden["universal_fields"]
    assert actual_fields == expected_fields, (
        f"REGRESSION in universal fields for {note_path.name!r} (page_type={page_type!r}):\n"
        f"  expected: {expected_fields!r}\n"
        f"  actual:   {actual_fields!r}"
    )

    actual_edges = _edge_set_sorted(generic_fm)
    expected_edges = golden["edge_set"]
    assert actual_edges == expected_edges, (
        f"REGRESSION in edge set for {note_path.name!r} (page_type={page_type!r}):\n  expected: {expected_edges!r}\n  actual:   {actual_edges!r}"
    )


# ---------------------------------------------------------------------------
# Live-knowledge_base smoke guard (skipped unless KNOWLEDGE_BASE_LIVE_PATH names a knowledge base)
# ---------------------------------------------------------------------------

_LIVE_KNOWLEDGE_BASE_PRESENT = _LIVE_KNOWLEDGE_BASE is not None and (_LIVE_KNOWLEDGE_BASE / "knowledge-base-schema.json").is_file()


def _declared_pages(root: pathlib.Path, schema: KnowledgeBaseSchema) -> list[pathlib.Path]:
    """Every markdown page in the folders and files `schema` declares under `root`, each once, sorted."""
    pages = {path for folder in schema.note_folders for path in (root / folder).rglob("*.md")}
    pages.update(root / name for name in schema.note_files if (root / name).is_file())
    return sorted(pages)


@pytest.mark.skipif(not _LIVE_KNOWLEDGE_BASE_PRESENT, reason=f"{_LIVE_KNOWLEDGE_BASE_ENV} names no knowledge base with a knowledge-base-schema.json")
def test_live_knowledge_base_smoke_sweep() -> None:
    """Sweep EVERY page in the folders and files the live knowledge base's own schema declares through the single generic path.

    Asserts:
    - `swept >= 485` (count-guard; the knowledge base carries at least 485 notes)
    - every note either parses via the public :func:`parse` API, or raises a `NoteGrammarError`
      naming grammar the note itself violates (e.g. a link-scalar field carrying more than one
      link) — the same fault tolerance `index/handlers.py` gives that error at index time,
      turning it into a page-level validation issue rather than aborting the sweep
    - resolver edge extraction succeeds on each successfully parsed note (no exception, edges is a list)
    """
    assert _LIVE_KNOWLEDGE_BASE is not None
    schema = KnowledgeBaseSchema.from_path(_LIVE_KNOWLEDGE_BASE / "knowledge-base-schema.json")
    swept = 0

    for path in _declared_pages(_LIVE_KNOWLEDGE_BASE, schema):
        text = path.read_text(encoding="utf-8")
        try:
            note = parse(text, source_path=path.relative_to(_LIVE_KNOWLEDGE_BASE).as_posix(), spec=schema)
        except NoteGrammarError:
            swept += 1
            continue
        edges = list(_frontmatter_relations(note.frontmatter))
        assert isinstance(edges, list), f"edge extraction returned non-list for {path.name!r}"
        swept += 1

    assert swept >= 485, f"expected at least 485 notes swept, got {swept} — knowledge base structure may have changed"


# ---------------------------------------------------------------------------
# parse_frontmatter integration: single path reachable via the public API
# ---------------------------------------------------------------------------


def test_parse_frontmatter_research_kind_via_generic() -> None:
    """parse_frontmatter routes all research page_types through the single generic path."""
    from engram.kb.contracts import parse_frontmatter

    research = research_spec()
    data: dict[str, object] = {
        "id": "paper.lightrag-2025",
        "type": "paper",
        "title": "LightRAG",
        "status": "draft",
        "paper_type": "method",
        "year": 2025,
        "zotero_key": "K",
        "source_hash": "h",
    }
    fm = parse_frontmatter(data, research)
    assert isinstance(fm, BaseFrontmatter)
    assert fm.type == "paper"
    # Kind-specific fields land in model_extra (generic path)
    assert fm.model_extra is not None
    assert fm.model_extra["year"] == 2025


def test_parse_frontmatter_with_the_research_spec() -> None:
    """parse_frontmatter against the research bundle's own spec parses an entity note."""
    from engram.kb.contracts import parse_frontmatter

    data: dict[str, object] = {"id": "ent.lightrag", "type": "entity", "title": "LightRAG", "status": "draft", "entity_type": "technique"}
    fm = parse_frontmatter(data, research_spec())
    assert isinstance(fm, BaseFrontmatter)
    assert fm.type == "entity"
