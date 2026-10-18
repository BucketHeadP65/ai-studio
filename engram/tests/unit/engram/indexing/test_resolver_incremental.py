"""Unit tests for the incremental resolver seams (`src/engram/indexing/domain/resolver.py`).

These pin the incremental contract:

* `build_link_index_from_pages` builds the same `LinkIndex` from `kb.pages`
  key rows (`id`, `slug`, `title`, `aliases`) that `build_link_index` builds
  from the equivalent notes — the two share one key-extraction helper, so for
  identical key inputs the indexes are byte-identical;
* `LinkIndex.merge` overlays a right operand whose keys **win** on conflict, so a
  dirty note's fresh identity overrides the stale clean-DB page view for the same key;
* `resolve_with_index` resolves a set of notes against a pre-built (merged) index,
  and `resolve` is just `resolve_with_index(notes, build_link_index(notes))`.

No DB, no IO: `PageKeyRow` rows are hand-built stubs and notes reuse the same
memory-domain builders as `test_resolver.py`.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from engram.indexing.contracts import resolve
from engram.indexing.domain.resolver import LinkIndex, build_link_index, build_link_index_from_pages, resolve_with_index
from engram.kb.contracts import KnowledgeBaseNote, UnitLink, parse_frontmatter
from engram.testing.specs import research_spec

_RESEARCH_SPEC = research_spec()


@dataclass(frozen=True)
class _PageRow:
    """A hand-built `kb.pages` key row matching the `PageKeyRow` protocol."""

    id: str
    slug: str
    title: str
    aliases: tuple[str, ...] = field(default_factory=tuple)


def _entity_note(*, id_: str, title: str, aliases: tuple[str, ...] = (), source_path: str | None = None) -> KnowledgeBaseNote:
    data: dict[str, object] = {
        "id": id_,
        "type": "entity",
        "title": title,
        "status": "draft",
        "aliases": list(aliases),
        "entity_type": "technique",
    }
    frontmatter = parse_frontmatter(data, _RESEARCH_SPEC)
    path = source_path or f"Memory/Entities/{id_.removeprefix('ent.')}.md"
    return KnowledgeBaseNote(frontmatter=frontmatter, sections=(), claims=(), source_path=path)


def _paper_note(
    *,
    id_: str = "paper.lightrag-2025",
    title: str = "LightRAG",
    aliases: tuple[str, ...] = (),
    source_path: str = "Memory/Papers/lightrag-2025.md",
    proposes: tuple[str, ...] = (),
) -> KnowledgeBaseNote:
    data: dict[str, object] = {
        "id": id_,
        "type": "paper",
        "title": title,
        "status": "draft",
        "aliases": list(aliases),
        "paper_type": "method",
        "year": 2025,
        "zotero_key": "K",
        "source_hash": "h",
        "proposes": [f"[[{t}]]" for t in proposes],
    }
    frontmatter = parse_frontmatter(data, _RESEARCH_SPEC)
    return KnowledgeBaseNote(frontmatter=frontmatter, sections=(), claims=(), source_path=source_path)


class TestBuilderParity:
    """An index from page-key rows equals one from the equivalent notes (shared key logic)."""

    def test_byte_identical_for_equivalent_inputs(self):
        note = _entity_note(id_="ent.lightrag", title="LightRAG", aliases=("light-rag",), source_path="Memory/Entities/lightrag.md")
        row = _PageRow(id="ent.lightrag", slug="lightrag", title="LightRAG", aliases=("light-rag",))

        from_notes = build_link_index((note,))
        from_rows = build_link_index_from_pages((row,))

        assert from_rows.by_key == from_notes.by_key

    def test_rows_resolve_links_like_notes(self):
        row = _PageRow(id="ent.lightrag", slug="lightrag", title="LightRAG", aliases=("light-rag",))
        from_rows = build_link_index_from_pages((row,))

        for target in ("lightrag", "LightRAG", "LIGHT-RAG", "ent.lightrag"):
            assert from_rows.resolve(UnitLink(target=target)) == "ent.lightrag"
        assert from_rows.resolve(UnitLink(target="missing")) is None

    def test_empty_keys_are_skipped(self):
        row = _PageRow(id="ent.x", slug="x", title="X", aliases=("", "  "))

        index = build_link_index_from_pages((row,))

        assert "" not in index.by_key
        assert index.resolve(UnitLink(target="x")) == "ent.x"


class TestMerge:
    """`merge` overlays the right operand: its keys win on conflict (dirty overrides DB view)."""

    def test_right_operand_wins_on_key_conflict(self):
        left = LinkIndex(by_key={"shared": "page.old", "only-left": "page.left"})
        right = LinkIndex(by_key={"shared": "page.new", "only-right": "page.right"})

        merged = left.merge(right)

        assert merged.by_key == {"shared": "page.new", "only-left": "page.left", "only-right": "page.right"}

    def test_renamed_title_resolves_to_the_page_after_merge(self):
        clean = build_link_index_from_pages((_PageRow(id="ent.lightrag", slug="lightrag", title="Old Title"),))
        dirty = build_link_index((_entity_note(id_="ent.lightrag", title="New Title", source_path="Memory/Entities/lightrag.md"),))

        merged = clean.merge(dirty)

        assert merged.resolve(UnitLink(target="New Title")) == "ent.lightrag"

    def test_merge_does_not_mutate_either_operand(self):
        left = LinkIndex(by_key={"k": "page.left"})
        right = LinkIndex(by_key={"k": "page.right"})

        left.merge(right)

        assert left.by_key == {"k": "page.left"}
        assert right.by_key == {"k": "page.right"}


class TestResolveWithIndex:
    """`resolve_with_index` resolves notes against a pre-built index; `resolve` delegates to it."""

    def test_resolve_delegates_to_resolve_with_index(self):
        target = _entity_note(id_="ent.lightrag", title="LightRAG")
        note = _paper_note(proposes=("lightrag",))
        notes = (note, target)

        assert resolve(notes) == resolve_with_index(notes, build_link_index(notes))

    def test_premerged_index_drives_the_resolved_split(self):
        clean = build_link_index_from_pages((_PageRow(id="ent.lightrag", slug="lightrag", title="LightRAG"),))
        dirty_note = _paper_note(
            id_="paper.survey-2025", title="A Survey", source_path="Memory/Papers/survey-2025.md", proposes=("lightrag",)
        )
        index = clean.merge(build_link_index((dirty_note,)))

        result = resolve_with_index((dirty_note,), index)
        resolved = next(n for n in result.notes if n.note.frontmatter.id == dirty_note.frontmatter.id)

        assert any(e.rel == "proposes" and e.dst_id == "ent.lightrag" for e in resolved.edges)
        assert result.warnings == ()

    def test_premerged_index_flags_a_dangling_split(self):
        clean = build_link_index_from_pages((_PageRow(id="ent.other", slug="other", title="Other"),))
        dirty_note = _paper_note(proposes=("ghost-entity",))
        index = clean.merge(build_link_index((dirty_note,)))

        result = resolve_with_index((dirty_note,), index)

        assert any(w.rule == "dangling-link" and "ghost-entity" in w.message for w in result.warnings)
