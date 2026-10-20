"""Unit tests for `ObsidianMirrorStore` against a throwaway `tmp_path` knowledge_base.

Never touches the real Obsidian knowledge_base: every test builds `KnowledgeBaseSettings(path=tmp_path)`
so writes land under pytest's temporary directory.

Active mirrors are named after the (sanitised) item title for human readability; the
permanent identity is the `source_key` in frontmatter, so the store resolves key->path by
reading frontmatter, not by the filename. Archived notes are key-named (out of the graph). The
store reads the current field names only: a note carrying no `source_key` is not a mirror it can
address, and is skipped.
"""

from __future__ import annotations

from datetime import date
from typing import TYPE_CHECKING

import pytest
import yaml

from engram.config import KnowledgeBaseSettings
from engram.sources.contracts import MirrorFile, MirrorStatus
from engram.sources.infrastructure.mirror import ObsidianMirrorStore
from engram.testing.factories.mirror import MirrorFrontmatterFactory
from engram.testing.specs import minimal_spec

if TYPE_CHECKING:
    from pathlib import Path


def _write_note(tmp_path: Path, name: str, frontmatter: dict[str, object]) -> None:
    """Drop a raw note under `Sources/` with exactly `frontmatter`, bypassing the store's own writer."""
    note = tmp_path / "Sources" / name
    note.parent.mkdir(parents=True, exist_ok=True)
    block = yaml.safe_dump(frontmatter, sort_keys=False)
    note.write_text(f"---\n{block}---\n# Body\n", encoding="utf-8")


def _store(tmp_path: Path) -> ObsidianMirrorStore:
    # `KnowledgeBaseSettings`'s shape-validator requires a schema file at the root.
    (tmp_path / "knowledge-base-schema.json").write_text(minimal_spec().model_dump_json(), encoding="utf-8")
    return ObsidianMirrorStore(KnowledgeBaseSettings(path=tmp_path))


async def test_write_creates_active_mirror_named_after_the_title(tmp_path: Path) -> None:
    store = _store(tmp_path)

    await store.write(frontmatter=MirrorFrontmatterFactory.build(), body="# Body")

    assert (tmp_path / "Sources" / "Attention Is All You Need.md").is_file()
    assert not (tmp_path / "Sources" / "ABCD1234.md").exists()


async def test_write_sanitises_filename_illegal_characters(tmp_path: Path) -> None:
    store = _store(tmp_path)

    await store.write(frontmatter=MirrorFrontmatterFactory.build(title="RAG: A/B Study"), body="# Body")

    assert (tmp_path / "Sources" / "RAG - A-B Study.md").is_file()


async def test_write_appends_key_on_title_collision(tmp_path: Path) -> None:
    store = _store(tmp_path)
    await store.write(frontmatter=MirrorFrontmatterFactory.build(title="Same Title", source_key="AAAA1111"), body="# One")

    await store.write(frontmatter=MirrorFrontmatterFactory.build(title="Same Title", source_key="BBBB2222"), body="# Two")

    assert (tmp_path / "Sources" / "Same Title.md").is_file()
    assert (tmp_path / "Sources" / "Same Title (BBBB2222).md").is_file()
    assert set(await store.load_index()) == {"AAAA1111", "BBBB2222"}


async def test_write_renames_when_title_changes_for_the_same_key(tmp_path: Path) -> None:
    store = _store(tmp_path)
    await store.write(frontmatter=MirrorFrontmatterFactory.build(title="First Title"), body="# Body")

    await store.write(frontmatter=MirrorFrontmatterFactory.build(title="Second Title"), body="# Body")

    assert (tmp_path / "Sources" / "Second Title.md").is_file()
    assert not (tmp_path / "Sources" / "First Title.md").exists()
    assert set(await store.load_index()) == {"ABCD1234"}


async def test_write_emits_parseable_yaml_frontmatter_that_round_trips_key_fields(tmp_path: Path) -> None:
    store = _store(tmp_path)

    await store.write(frontmatter=MirrorFrontmatterFactory.build(keywords=["attention"]), body="# Body")

    text = (tmp_path / "Sources" / "Attention Is All You Need.md").read_text(encoding="utf-8")
    assert text.startswith("---\n")
    _, block, _ = text.split("---\n", 2)
    parsed = yaml.safe_load(block)
    assert parsed["source_key"] == "ABCD1234"
    assert parsed["source_version"] == 7
    assert parsed["source_hash"] == "deadbeef"
    assert parsed["status"] == "active"
    assert parsed["title"] == "Attention Is All You Need"
    assert parsed["keywords"] == ["attention"]
    assert "tags" not in parsed


async def test_load_index_returns_mirror_file_for_written_note(tmp_path: Path) -> None:
    store = _store(tmp_path)
    await store.write(frontmatter=MirrorFrontmatterFactory.build(), body="# Body")

    index = await store.load_index()

    assert set(index) == {"ABCD1234"}
    assert index["ABCD1234"] == MirrorFile(
        source_key="ABCD1234",
        source_version=7,
        source_hash="deadbeef",
        provider="example-library",
        converter="azure-di/prebuilt-layout+formulas",
        status=MirrorStatus.ACTIVE,
        url=None,
        doi=None,
    )


async def test_load_index_reads_the_url_and_doi_a_mirror_names(tmp_path: Path) -> None:
    store = _store(tmp_path)
    await store.write(frontmatter=MirrorFrontmatterFactory.build(url="https://arxiv.org/abs/2401.00001", doi="10.1000/example.42"), body="# Body")

    index = await store.load_index()

    assert (index["ABCD1234"].url, index["ABCD1234"].doi) == ("https://arxiv.org/abs/2401.00001", "10.1000/example.42")


async def test_load_index_round_trips_the_on_disk_converter_label(tmp_path: Path) -> None:
    store = _store(tmp_path)
    await store.write(frontmatter=MirrorFrontmatterFactory.build(converter="arxiv-html/pandoc"), body="# Body")

    index = await store.load_index()

    assert index["ABCD1234"].converter == "arxiv-html/pandoc"


async def test_load_index_defaults_converter_for_a_note_without_the_key(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _write_note(
        tmp_path,
        "HANDWRITTEN.md",
        {"source_key": "HAND0001", "source_version": 3, "source_hash": "cafef00d", "provider": "example-library", "status": "active", "url": None, "doi": None},
    )

    index = await store.load_index()

    assert index["HAND0001"].converter == "unknown"


async def test_a_note_with_no_source_key_is_not_a_mirror_the_store_can_address(tmp_path: Path) -> None:
    # The store reads the current field names only: identity is `source_key`. A file under
    # `Sources/` carrying anything else is skipped rather than guessed at, so a half-migrated or
    # hand-dropped file can never claim a key.
    store = _store(tmp_path)
    _write_note(
        tmp_path,
        "Unkeyed.md",
        {"title": "Unkeyed", "source_version": 3, "source_hash": "cafef00d", "provider": "example-library", "status": "active"},
    )

    assert await store.load_index() == {}
    assert await store.load_source_links() == {}


async def test_a_note_naming_no_provider_is_skipped_by_the_index(tmp_path: Path) -> None:
    # `provider` has no default: it is what a sync run's orphan pass narrows its own mirrors by, so
    # a guessed name would archive another provider's. One such file is skipped, never fatal, and
    # the rest of the walk still loads.
    store = _store(tmp_path)
    _write_note(
        tmp_path, "NoProvider.md", {"source_key": "NOPROV01", "source_version": 1, "source_hash": "cafef00d", "status": "active"}
    )
    _write_note(
        tmp_path,
        "Fine.md",
        {"source_key": "FINE0001", "source_version": 1, "source_hash": "deadbeef", "provider": "example-library", "status": "active", "url": None, "doi": None},
    )

    index = await store.load_index()

    assert set(index) == {"FINE0001"}


class TestLoadSourceLinks:
    """`load_source_links` answers the deep link each mirror carries, the one read the OKF export needs."""

    async def test_maps_each_source_key_to_its_own_link(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        await store.write(frontmatter=MirrorFrontmatterFactory.build(source_key="AAAA1111", title="One", source_link="ref://one"), body="#")
        await store.write(frontmatter=MirrorFrontmatterFactory.build(source_key="BBBB2222", title="Two", source_link="ref://two"), body="#")

        assert await store.load_source_links() == {"AAAA1111": "ref://one", "BBBB2222": "ref://two"}

    async def test_a_mirror_with_an_empty_link_is_absent_from_the_map(self, tmp_path: Path) -> None:
        # Absent rather than present-and-empty, so a caller reads a hit as "there is a link".
        store = _store(tmp_path)
        await store.write(frontmatter=MirrorFrontmatterFactory.build(source_key="AAAA1111", title="One", source_link=""), body="#")
        await store.write(frontmatter=MirrorFrontmatterFactory.build(source_key="BBBB2222", title="Two", source_link="ref://two"), body="#")

        assert await store.load_source_links() == {"BBBB2222": "ref://two"}

    async def test_a_blank_link_is_absent_too(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        _write_note(tmp_path, "Blank.md", {"source_key": "AAAA1111", "source_link": "   "})

        assert await store.load_source_links() == {}

    async def test_values_are_stripped(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        _write_note(tmp_path, "Padded.md", {"source_key": "AAAA1111", "source_link": "  ref://one  "})

        assert await store.load_source_links() == {"AAAA1111": "ref://one"}

    async def test_archived_mirrors_are_scanned_alongside_active_ones(self, tmp_path: Path) -> None:
        # A page digesting a soft-deleted source still cites it, so its link must stay reachable.
        store = _store(tmp_path)
        await store.write(frontmatter=MirrorFrontmatterFactory.build(source_key="AAAA1111", title="Active", source_link="ref://act"), body="#")
        await store.write(frontmatter=MirrorFrontmatterFactory.build(source_key="BBBB2222", title="Gone", source_link="ref://gone"), body="#")
        assert await store.soft_delete("BBBB2222")

        assert await store.load_source_links() == {"AAAA1111": "ref://act", "BBBB2222": "ref://gone"}

    async def test_no_sources_folder_yet_is_an_empty_map_not_an_error(self, tmp_path: Path) -> None:
        store = _store(tmp_path)

        assert await store.load_source_links() == {}


async def test_read_body_resolves_by_frontmatter_key_not_filename(tmp_path: Path) -> None:
    store = _store(tmp_path)
    await store.write(frontmatter=MirrorFrontmatterFactory.build(), body="# Body\n\nProse.")

    assert await store.read_body("ABCD1234") == "# Body\n\nProse."


async def test_read_body_returns_none_when_absent(tmp_path: Path) -> None:
    store = _store(tmp_path)

    assert await store.read_body("MISSING") is None


async def test_soft_delete_moves_note_to_key_named_archive_and_flips_status(tmp_path: Path) -> None:
    store = _store(tmp_path)
    await store.write(frontmatter=MirrorFrontmatterFactory.build(), body="# Body")

    moved = await store.soft_delete("ABCD1234")

    assert moved is True
    assert not (tmp_path / "Sources" / "Attention Is All You Need.md").exists()
    archived = tmp_path / "Sources" / "_archive" / "ABCD1234.md"
    assert archived.is_file()
    _, block, _ = archived.read_text(encoding="utf-8").split("---\n", 2)
    assert yaml.safe_load(block)["status"] == "removed"


async def test_soft_delete_makes_active_read_body_return_none(tmp_path: Path) -> None:
    store = _store(tmp_path)
    await store.write(frontmatter=MirrorFrontmatterFactory.build(), body="# Body")

    await store.soft_delete("ABCD1234")

    assert await store.read_body("ABCD1234") is None


async def test_soft_deleted_mirror_is_indexed_as_removed(tmp_path: Path) -> None:
    store = _store(tmp_path)
    await store.write(frontmatter=MirrorFrontmatterFactory.build(), body="# Body")
    await store.soft_delete("ABCD1234")

    index = await store.load_index()

    assert index["ABCD1234"].status is MirrorStatus.REMOVED


async def test_soft_delete_returns_false_when_nothing_to_move(tmp_path: Path) -> None:
    store = _store(tmp_path)

    assert await store.soft_delete("MISSING") is False


async def test_rewrite_after_archive_restores_active_and_removes_archived_copy(tmp_path: Path) -> None:
    store = _store(tmp_path)
    await store.write(frontmatter=MirrorFrontmatterFactory.build(), body="# Body")
    await store.soft_delete("ABCD1234")

    await store.write(frontmatter=MirrorFrontmatterFactory.build(source_version=8), body="# Rewritten")

    assert (tmp_path / "Sources" / "Attention Is All You Need.md").is_file()
    assert not (tmp_path / "Sources" / "_archive" / "ABCD1234.md").exists()
    index = await store.load_index()
    assert index["ABCD1234"].status is MirrorStatus.ACTIVE
    assert index["ABCD1234"].source_version == 8
    assert await store.read_body("ABCD1234") == "# Rewritten"


async def test_write_persists_assets_under_sources(tmp_path: Path) -> None:
    store = _store(tmp_path)
    assets = {"_assets/2412.15605/x1.png": b"\x89PNG one", "_assets/2412.15605/x2.png": b"\x89PNG two"}

    await store.write(frontmatter=MirrorFrontmatterFactory.build(), body="# Body ![x1](_assets/2412.15605/x1.png)", assets=assets)

    for relpath, data in assets.items():
        assert (tmp_path / "Sources" / relpath).read_bytes() == data


async def test_write_overwrites_assets_on_reconvert(tmp_path: Path) -> None:
    store = _store(tmp_path)
    relpath = "_assets/2412.15605/x1.png"
    await store.write(frontmatter=MirrorFrontmatterFactory.build(), body="# Body", assets={relpath: b"v1"})

    await store.write(frontmatter=MirrorFrontmatterFactory.build(source_version=8), body="# Body", assets={relpath: b"v2"})

    assert (tmp_path / "Sources" / relpath).read_bytes() == b"v2"


@pytest.mark.parametrize("relpath", ["../evil.png", "/etc/evil.png", "_assets/../../evil.png"])
async def test_write_rejects_asset_paths_escaping_sources(tmp_path: Path, relpath: str) -> None:
    store = _store(tmp_path)

    with pytest.raises(ValueError, match="asset path"):
        await store.write(frontmatter=MirrorFrontmatterFactory.build(), body="# Body", assets={relpath: b"x"})

    assert not (tmp_path / "evil.png").exists()


async def test_a_zotero_mirrors_rendered_frontmatter_carries_no_channel_or_published_keys(tmp_path: Path) -> None:
    # Full-equality pin: a Zotero mirror (channel, published and meta unset) renders exactly what every
    # pre-existing field produces via `model_dump`, with the three optional keys entirely absent.
    # A `channel: null`, `published: null` or `meta: {}` line would change the rendered frontmatter of
    # every Zotero note on its next re-sync.
    store = _store(tmp_path)
    frontmatter = MirrorFrontmatterFactory.build()

    await store.write(frontmatter=frontmatter, body="# Body")

    text = (tmp_path / "Sources" / "Attention Is All You Need.md").read_text(encoding="utf-8")
    _, block, _ = text.split("---\n", 2)
    parsed = yaml.safe_load(block)

    assert "channel" not in parsed
    assert "published" not in parsed
    assert "meta" not in parsed
    expected = {k: v for k, v in frontmatter.model_dump(mode="json").items() if k not in ("channel", "published", "meta")}
    assert parsed == expected


async def test_a_mirrors_metadata_renders_as_a_mapping_when_set(tmp_path: Path) -> None:
    store = _store(tmp_path)
    frontmatter = MirrorFrontmatterFactory.build(meta={"region": "north", "issued_on": "2024-01-02"})

    await store.write(frontmatter=frontmatter, body="# Body")

    text = (tmp_path / "Sources" / "Attention Is All You Need.md").read_text(encoding="utf-8")
    _, block, _ = text.split("---\n", 2)
    parsed = yaml.safe_load(block)

    assert parsed["meta"] == {"region": "north", "issued_on": "2024-01-02"}, "a date-shaped value reads back as text"
    assert (await store.load_index())["ABCD1234"].source_version == 7, "the next sync still reads the mirror back"


async def test_a_youtube_mirrors_channel_and_published_render_when_set(tmp_path: Path) -> None:
    store = _store(tmp_path)
    frontmatter = MirrorFrontmatterFactory.build(provider="youtube", channel="Some Channel", published=date(2024, 3, 5))

    await store.write(frontmatter=frontmatter, body="# Body")

    text = (tmp_path / "Sources" / "Attention Is All You Need.md").read_text(encoding="utf-8")
    _, block, _ = text.split("---\n", 2)
    parsed = yaml.safe_load(block)

    assert parsed["channel"] == "Some Channel"
    assert parsed["published"] == "2024-03-05"


async def test_a_youtube_mirror_without_channel_or_published_renders_neither(tmp_path: Path) -> None:
    store = _store(tmp_path)
    frontmatter = MirrorFrontmatterFactory.build(provider="youtube", channel=None, published=None)

    await store.write(frontmatter=frontmatter, body="# Body")

    text = (tmp_path / "Sources" / "Attention Is All You Need.md").read_text(encoding="utf-8")
    _, block, _ = text.split("---\n", 2)
    parsed = yaml.safe_load(block)

    assert "channel" not in parsed
    assert "published" not in parsed


async def test_a_write_after_load_index_reads_no_other_mirror(tmp_path: Path) -> None:
    store = _store(tmp_path)
    for index in range(20):
        await store.write(frontmatter=MirrorFrontmatterFactory.build(title=f"Doc {index}", source_key=f"K{index:04d}"), body="text")
    await store.load_index()
    for note in (tmp_path / "Sources").glob("*.md"):
        note.chmod(0)

    try:
        await store.write(frontmatter=MirrorFrontmatterFactory.build(title="A new doc", source_key="KNEW"), body="text")
    finally:
        for note in (tmp_path / "Sources").glob("*.md"):
            note.chmod(0o644)

    assert (tmp_path / "Sources" / "A new doc.md").is_file()


async def test_a_rewrite_after_load_index_renames_the_mirror_it_holds(tmp_path: Path) -> None:
    store = _store(tmp_path)
    await store.write(frontmatter=MirrorFrontmatterFactory.build(title="Old title", source_key="K1"), body="one")
    await store.load_index()

    await store.write(frontmatter=MirrorFrontmatterFactory.build(title="New title", source_key="K1"), body="two")
    await store.write(frontmatter=MirrorFrontmatterFactory.build(title="Newer title", source_key="K1"), body="three")

    assert sorted(note.name for note in (tmp_path / "Sources").glob("*.md")) == ["Newer title.md"]


async def test_a_soft_delete_after_load_index_forgets_the_key(tmp_path: Path) -> None:
    store = _store(tmp_path)
    await store.write(frontmatter=MirrorFrontmatterFactory.build(title="Doc", source_key="K1"), body="one")
    await store.load_index()

    assert await store.soft_delete("K1")
    assert not await store.soft_delete("K1")
    assert await store.read_body("K1") is None
