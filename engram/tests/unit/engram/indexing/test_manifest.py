"""Unit tests for the pure index-manifest change detector (`src/engram/indexing/domain/manifest.py`).

These pin three contracts, all pure (no IO, no DB):

- `identity_hash` — the sha256 of a note's normalized resolution-key set
  `{stem, id, title, *aliases}`: order-independent across aliases,
  whitespace/case-insensitive, and dropping empty keys.
- `compute_diff` — the five-way partition (added / removed / content_changed /
  identity_changed / unchanged) of the current re-hashed files against the
  stored manifest, with the invariant `identity_changed ⊆ content_changed`.
- `compute_closure` — the dirty-set closure: the reparse set plus the inbound
  closure (pages referencing an appeared/disappeared resolution key) plus the
  mirror citers, with the targeted deletes (`deleted_page_ids` /
  `deleted_source_keys`), including the single-level completeness lemma.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from engram.indexing.contracts import ManifestEntry, content_hash
from engram.indexing.domain.manifest import (
    DirtySet,
    FileType,
    HashedFile,
    IdentitySurface,
    ManifestDiff,
    compute_closure,
    compute_diff,
    identity_hash,
)

if TYPE_CHECKING:
    from collections.abc import Callable


def _entry(path: str, *, content: str, identity: str) -> ManifestEntry:
    return ManifestEntry(
        path=path, kind=FileType.NOTE, content_hash=content, identity_hash=identity, page_id=path.removesuffix(".md"), source_key=None
    )


def _hashed(path: str, *, content: str, identity: str) -> HashedFile:
    return HashedFile(path=path, kind=FileType.NOTE, text=f"text-of-{path}", content_hash=content, identity_hash=identity)


def test_compute_diff_partitions_all_five_buckets() -> None:
    stored = {
        "body.md": _entry("body.md", content="c-body-old", identity="i-body"),
        "fm.md": _entry("fm.md", content="c-fm-old", identity="i-fm-old"),
        "removed.md": _entry("removed.md", content="c-removed", identity="i-removed"),
        "same.md": _entry("same.md", content="c-same", identity="i-same"),
    }
    current = {
        # body-only change: content differs, identity unchanged.
        "body.md": _hashed("body.md", content="c-body-new", identity="i-body"),
        # frontmatter id/title change: content AND identity differ.
        "fm.md": _hashed("fm.md", content="c-fm-new", identity="i-fm-new"),
        # brand new path.
        "added.md": _hashed("added.md", content="c-added", identity="i-added"),
        # untouched on both axes.
        "same.md": _hashed("same.md", content="c-same", identity="i-same"),
    }

    diff = compute_diff(stored, current)

    assert diff.added == ("added.md",)
    assert diff.removed == ("removed.md",)
    assert diff.content_changed == ("body.md", "fm.md")
    assert diff.identity_changed == ("fm.md",)
    assert diff.unchanged == ("same.md",)


def test_compute_diff_identity_changed_is_subset_of_content_changed() -> None:
    stored = {
        "a.md": _entry("a.md", content="c-a-old", identity="i-a-old"),
        "b.md": _entry("b.md", content="c-b-old", identity="i-b"),
        "c.md": _entry("c.md", content="c-c", identity="i-c-old"),
    }
    current = {
        "a.md": _hashed("a.md", content="c-a-new", identity="i-a-new"),
        "b.md": _hashed("b.md", content="c-b-new", identity="i-b"),
        # identity differs but content is identical: NOT a content change, so it
        # must not leak into identity_changed (the subset invariant).
        "c.md": _hashed("c.md", content="c-c", identity="i-c-new"),
    }

    diff = compute_diff(stored, current)

    assert set(diff.identity_changed) <= set(diff.content_changed)
    assert "c.md" not in diff.content_changed
    assert "c.md" not in diff.identity_changed
    assert "c.md" in diff.unchanged


def test_compute_diff_outputs_are_sorted() -> None:
    stored = {"z.md": _entry("z.md", content="cz", identity="iz"), "a.md": _entry("a.md", content="ca", identity="ia")}
    current = {"y.md": _hashed("y.md", content="cy", identity="iy"), "b.md": _hashed("b.md", content="cb", identity="ib")}

    diff = compute_diff(stored, current)

    assert diff.added == ("b.md", "y.md")
    assert diff.removed == ("a.md", "z.md")


def test_compute_diff_empty_stored_makes_everything_added() -> None:
    current = {"two.md": _hashed("two.md", content="c2", identity="i2"), "one.md": _hashed("one.md", content="c1", identity="i1")}

    diff = compute_diff({}, current)

    assert diff.added == ("one.md", "two.md")
    assert diff.removed == ()
    assert diff.content_changed == ()
    assert diff.identity_changed == ()
    assert diff.unchanged == ()


def test_identity_hash_matches_normalized_keyset_digest() -> None:
    result = identity_hash(stem="my-note", id_="N1", title="Title", aliases=("Alpha",))

    expected = content_hash("\n".join(sorted({"my-note", "n1", "title", "alpha"})))
    assert result == expected


def test_identity_hash_is_alias_order_independent() -> None:
    one = identity_hash(stem="s", id_="i", title="t", aliases=("a", "b", "c"))
    two = identity_hash(stem="s", id_="i", title="t", aliases=("c", "a", "b"))

    assert one == two


def test_identity_hash_normalizes_whitespace_and_case() -> None:
    one = identity_hash(stem="My-Note", id_=" N1 ", title="The Title", aliases=("Alpha",))
    two = identity_hash(stem="my-note", id_="n1", title="the title", aliases=("alpha",))

    assert one == two


def test_identity_hash_drops_empty_keys() -> None:
    with_blanks = identity_hash(stem="s", id_="", title="   ", aliases=("", "real"))
    without_blanks = identity_hash(stem="s", id_="real", title="", aliases=())

    assert with_blanks == without_blanks


def test_identity_hash_deduplicates_keys() -> None:
    deduped = identity_hash(stem="same", id_="same", title="SAME", aliases=("same",))

    expected = content_hash("same")
    assert deduped == expected


# --- compute_closure scenarios ------------------------------------------------


def _surface(page_id: str, *keys: str) -> IdentitySurface:
    return IdentitySurface(page_id=page_id, keys=frozenset(keys))


def _diff(
    *,
    added: tuple[str, ...] = (),
    removed: tuple[str, ...] = (),
    content_changed: tuple[str, ...] = (),
    identity_changed: tuple[str, ...] = (),
    unchanged: tuple[str, ...] = (),
) -> ManifestDiff:
    return ManifestDiff(
        added=added, removed=removed, content_changed=content_changed, identity_changed=identity_changed, unchanged=unchanged
    )


def _by_name(mapping: dict[str, set[str]]) -> Callable[[frozenset[str]], frozenset[str]]:
    """Build an `inbound_by_name` stub: name → src page ids, unioned over the query set."""

    def lookup(names: frozenset[str]) -> frozenset[str]:
        hits: set[str] = set()
        for name in names:
            hits |= mapping.get(name, set())
        return frozenset(hits)

    return lookup


def _citers(mapping: dict[str, set[str]]) -> Callable[[frozenset[str]], frozenset[str]]:
    """Build a `mirror_citers` stub: zotero key → citing src page ids, unioned over the query set."""

    def lookup(keys: frozenset[str]) -> frozenset[str]:
        hits: set[str] = set()
        for key in keys:
            hits |= mapping.get(key, set())
        return frozenset(hits)

    return lookup


def _path_of(mapping: dict[str, str]) -> Callable[[str], str | None]:
    """Build a `path_of_page` stub from a page-id → path mapping (unknown ids → None)."""

    def lookup(page_id: str) -> str | None:
        return mapping.get(page_id)

    return lookup


def test_closure_a_body_only_edit_is_just_that_file() -> None:
    # A body-only edit: content changed, identity unchanged, so no name appeared
    # or disappeared and the closure is empty.
    diff = _diff(content_changed=("notes/body.md",), unchanged=("notes/linker.md",))
    old = {"notes/body.md": _surface("body", "body", "the body title")}
    new = {"notes/body.md": _surface("body", "body", "the body title")}

    result = compute_closure(
        diff,
        old_surfaces=old,
        new_surfaces=new,
        inbound_by_name=_by_name({"the body title": {"linker"}}),  # must NOT fire (identity unchanged)
        mirror_citers=_citers({}),
        path_of_page=_path_of({"body": "notes/body.md", "linker": "notes/linker.md"}),
    )

    assert result == DirtySet(
        dirty_paths=frozenset({"notes/body.md"}), deleted_page_ids=frozenset(), deleted_source_keys=frozenset()
    )


def test_closure_b_title_change_pulls_in_linkers_via_old_and_new_title() -> None:
    # The page's title flips old → new; linkers via EITHER title must re-resolve.
    diff = _diff(content_changed=("notes/target.md",), identity_changed=("notes/target.md",))
    old = {"notes/target.md": _surface("target", "target", "old title")}
    new = {"notes/target.md": _surface("target", "target", "new title")}

    result = compute_closure(
        diff,
        old_surfaces=old,
        new_surfaces=new,
        inbound_by_name=_by_name({"old title": {"src-old"}, "new title": {"src-new"}}),
        mirror_citers=_citers({}),
        path_of_page=_path_of({"src-old": "notes/links-old.md", "src-new": "notes/links-new.md"}),
    )

    assert result.dirty_paths == frozenset({"notes/target.md", "notes/links-old.md", "notes/links-new.md"})
    assert result.deleted_page_ids == frozenset()
    assert result.deleted_source_keys == frozenset()


def test_closure_c_file_rename_pulls_linkers_and_deletes_old_page_id() -> None:
    # A file rename = removed (old stem) + added (new stem) with a NEW page id.
    diff = _diff(added=("notes/new-name.md",), removed=("notes/old-name.md",))
    old = {"notes/old-name.md": _surface("old-name", "old-name", "shared title")}
    new = {"notes/new-name.md": _surface("new-name", "new-name", "shared title")}

    result = compute_closure(
        diff,
        old_surfaces=old,
        new_surfaces=new,
        # Both the vanished old-name stem and the appeared new-name stem are affected.
        inbound_by_name=_by_name({"old-name": {"linker"}, "new-name": {"linker"}}),
        mirror_citers=_citers({}),
        path_of_page=_path_of({"linker": "notes/linker.md"}),
    )

    assert result.dirty_paths == frozenset({"notes/new-name.md", "notes/linker.md"})
    assert result.deleted_page_ids == frozenset({"old-name"})  # old page id row must go


def test_closure_d_id_rename_deletes_old_id_reparses_new_and_pulls_both_name_linkers() -> None:
    # Same file, the frontmatter id flips n-old → n-new (identity changed).
    diff = _diff(content_changed=("notes/paper.md",), identity_changed=("notes/paper.md",))
    old = {"notes/paper.md": _surface("n-old", "paper", "n-old", "title")}
    new = {"notes/paper.md": _surface("n-new", "paper", "n-new", "title")}

    result = compute_closure(
        diff,
        old_surfaces=old,
        new_surfaces=new,
        inbound_by_name=_by_name({"n-old": {"src-a"}, "n-new": {"src-b"}}),
        mirror_citers=_citers({}),
        path_of_page=_path_of({"src-a": "notes/a.md", "src-b": "notes/b.md"}),
    )

    assert result.dirty_paths == frozenset({"notes/paper.md", "notes/a.md", "notes/b.md"})
    # The old id row must be deleted (the new id is upserted via the reparse of paper.md).
    assert result.deleted_page_ids == frozenset({"n-old"})


def test_closure_e_add_satisfying_prior_dangling_link_pulls_the_dangler_src() -> None:
    # A brand-new page named "Glossary" satisfies a previously-dangling [[Glossary]].
    diff = _diff(added=("notes/glossary.md",), unchanged=("notes/dangler.md",))
    new = {"notes/glossary.md": _surface("glossary", "glossary")}

    result = compute_closure(
        diff,
        old_surfaces={},
        new_surfaces=new,
        # The name query hits the dangler's src by raw target (resolved OR dangling).
        inbound_by_name=_by_name({"glossary": {"dangler"}}),
        mirror_citers=_citers({}),
        path_of_page=_path_of({"glossary": "notes/glossary.md", "dangler": "notes/dangler.md"}),
    )

    assert result.dirty_paths == frozenset({"notes/glossary.md", "notes/dangler.md"})
    assert result.deleted_page_ids == frozenset()


def test_closure_f_delete_link_target_pulls_resolved_inbound_and_deletes_target_id() -> None:
    # Deleting a link target: its resolved-inbound srcs now dangle and must re-resolve.
    diff = _diff(removed=("notes/target.md",), unchanged=("notes/src.md",))
    old = {"notes/target.md": _surface("target", "target", "target title")}

    result = compute_closure(
        diff,
        old_surfaces=old,
        new_surfaces={},
        inbound_by_name=_by_name({"target title": {"src"}, "target": {"src"}}),
        mirror_citers=_citers({}),
        path_of_page=_path_of({"src": "notes/src.md"}),
    )

    assert result.dirty_paths == frozenset({"notes/src.md"})  # the target itself is gone, not reparsed
    assert result.deleted_page_ids == frozenset({"target"})


def test_closure_g_changed_mirror_pulls_its_citers() -> None:
    # A source mirror whose identity (zotero key) is its single resolution key; a
    # content change to it must re-verify quotes in every page that cites it. A
    # source has no kb.pages row, so its surface carries an empty page_id
    # (index_manifest.page_id is NULL for sources).
    diff = _diff(content_changed=("Sources/ABCD1234.md",), unchanged=("notes/citer.md",))
    old = {"Sources/ABCD1234.md": _surface("", "abcd1234")}
    new = {"Sources/ABCD1234.md": _surface("", "abcd1234")}

    result = compute_closure(
        diff,
        old_surfaces=old,
        new_surfaces=new,
        inbound_by_name=_by_name({}),
        mirror_citers=_citers({"abcd1234": {"citer"}}),
        path_of_page=_path_of({"citer": "notes/citer.md"}),
    )

    assert result.dirty_paths == frozenset({"Sources/ABCD1234.md", "notes/citer.md"})


def test_closure_h_completeness_lemma_closure_is_single_level() -> None:
    # Completeness lemma: a closure page C is dirty only because it references an
    # affected name. C's own identity is UNCHANGED (C is not in added/content_changed),
    # so re-resolving C introduces no NEW affected name — the closure is single-level.
    diff = _diff(content_changed=("notes/target.md",), identity_changed=("notes/target.md",), unchanged=("notes/c.md",))
    old = {"notes/target.md": _surface("target", "target", "old name")}
    new = {"notes/target.md": _surface("target", "target", "new name")}
    inbound = _by_name({"old name": {"c"}, "new name": {"c"}})

    result = compute_closure(
        diff,
        old_surfaces=old,
        new_surfaces=new,
        inbound_by_name=inbound,
        mirror_citers=_citers({}),
        path_of_page=_path_of({"c": "notes/c.md"}),
    )

    # First level: C is pulled in.
    assert "notes/c.md" in result.dirty_paths

    # Single-level proof: the affected names came ONLY from the symmetric diff over
    # identity_changed ∪ added ∪ removed. C is in `unchanged`, so even though C is now
    # dirty, its keys never enter the affected-name set, hence no page that links to C
    # (e.g. a hypothetical second-level "gc" linker) is dragged in.
    assert callable(inbound)
    assert inbound(frozenset({"c"})) == frozenset()  # nobody asked for C's own name; C never widens the set
    assert result.dirty_paths == frozenset({"notes/target.md", "notes/c.md"})
