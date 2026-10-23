"""Unit tests for `SqliteZoteroConnector` against a synthetic Zotero `sqlite`.

The fixture (`zotero_sqlite_factory`, in the sibling `conftest.py`) seeds an imported
article, two linked-file items (base-relative + absolute), a linked-URL item, a
trashed item, a PDF-less item, and a second collection. These tests pin the JOINs,
all three PDF-path kinds, the linked-URL / trashed / PDF-less exclusions, and the
optional collection filter.
"""

from __future__ import annotations

import sqlite3

from contextlib import closing
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from engram.config import KnowledgeBaseSettings, SourceResources
from engram.sources.contracts import SourceType
from sqlalchemy import text
from sqlalchemy.exc import OperationalError

from engram_connectors.zotero.contracts import LibrarySettings, SqliteZoteroConnector
from engram_connectors.zotero.infrastructure import connector as connector_module

if TYPE_CHECKING:
    from collections.abc import Callable

    from engram.sources.contracts import SourceItem

    from tests.unit.zotero.conftest import SeededZotero


@pytest.fixture
def seeded(zotero_sqlite_factory: Callable[[], SeededZotero]) -> SeededZotero:
    return zotero_sqlite_factory()


@pytest.fixture
def connector(seeded: SeededZotero) -> SqliteZoteroConnector:
    # base_attachment_dir/collection are passed explicitly so these tests stay hermetic —
    # the repo only ever sees the seeded data dir, never any ambient/machine config.
    return SqliteZoteroConnector(LibrarySettings(data_dir=seeded.data_dir, base_attachment_dir=seeded.base_attachment_dir, collection=""))


def _by_key(items: list[SourceItem], key: str) -> SourceItem:
    return next(item for item in items if item.provider_key == key)


def test_pdf_attachment_filter_constants_pin_the_zotero_schema_values() -> None:
    # These select the PDF-with-a-local-file attachments in the items query.
    # A future edit to either magic value must trip this test (and the rendered
    # SQL must keep building from the constants), not silently change the knowledge base.
    assert connector_module._PDF_CONTENT_TYPE == "application/pdf"
    assert connector_module._LOCAL_FILE_LINK_MODES == (0, 1, 2)
    assert "'application/pdf'" in connector_module._ITEMS_SQL
    assert "IN (0, 1, 2)" in connector_module._ITEMS_SQL


async def test_list_items_with_pdf_includes_imported_and_linked_file_parents(connector: SqliteZoteroConnector, seeded: SeededZotero):
    keys = {item.provider_key for item in await connector.list_items_with_pdf()}

    assert keys == {seeded.article_key, seeded.linked_rel_key, seeded.linked_abs_key}


async def test_list_items_with_pdf_maps_scalar_metadata(connector: SqliteZoteroConnector, seeded: SeededZotero):
    item = _by_key(await connector.list_items_with_pdf(), seeded.article_key)

    assert item.title == seeded.title
    assert item.doi == seeded.doi
    assert item.url == seeded.url
    assert item.year == seeded.year
    assert item.item_type == "journalArticle"


async def test_list_items_with_pdf_carries_the_zotero_select_deep_link(connector: SqliteZoteroConnector, seeded: SeededZotero):
    item = _by_key(await connector.list_items_with_pdf(), seeded.article_key)

    assert item.source_link == f"zotero://select/library/items/{seeded.article_key}"


async def test_list_items_with_pdf_maps_creators_collections_tags(connector: SqliteZoteroConnector, seeded: SeededZotero):
    item = _by_key(await connector.list_items_with_pdf(), seeded.article_key)

    assert [(c.first_name, c.last_name) for c in item.creators] == seeded.creator_names
    assert item.collections == [seeded.collection_name]
    assert item.tags == [seeded.tag_name]


async def test_list_items_with_pdf_resolves_storage_path_to_the_seeded_file(connector: SqliteZoteroConnector, seeded: SeededZotero):
    item = _by_key(await connector.list_items_with_pdf(), seeded.article_key)

    assert item.pdf_path == seeded.pdf_path
    assert item.pdf_path.exists()


async def test_list_items_with_pdf_resolves_attachments_relative_path_under_base_dir(
    connector: SqliteZoteroConnector, seeded: SeededZotero
):
    item = _by_key(await connector.list_items_with_pdf(), seeded.linked_rel_key)

    assert item.pdf_path == seeded.linked_rel_pdf_path


async def test_list_items_with_pdf_resolves_absolute_linked_path_verbatim(connector: SqliteZoteroConnector, seeded: SeededZotero):
    item = _by_key(await connector.list_items_with_pdf(), seeded.linked_abs_key)

    assert item.pdf_path == seeded.linked_abs_pdf_path


async def test_a_null_attachment_path_is_a_missing_file_for_that_item_only(connector: SqliteZoteroConnector, seeded: SeededZotero):
    with closing(sqlite3.connect(seeded.data_dir / "zotero.sqlite")) as database:
        database.execute("UPDATE itemAttachments SET path = NULL WHERE itemID = 9")
        database.commit()

    items = await connector.list_items_with_pdf()

    assert not _by_key(items, seeded.linked_abs_key).pdf_path.exists()
    assert {item.provider_key for item in items} == {seeded.article_key, seeded.linked_rel_key, seeded.linked_abs_key}


async def test_list_items_with_pdf_excludes_linked_url(connector: SqliteZoteroConnector, seeded: SeededZotero):
    keys = {item.provider_key for item in await connector.list_items_with_pdf()}

    assert seeded.linked_url_key not in keys


async def test_the_connection_to_the_copy_refuses_a_write(connector: SqliteZoteroConnector):
    async with connector._connect() as connection:
        await connection.execute(text("SELECT count(*) FROM items"))
        with pytest.raises(OperationalError, match="readonly database"):
            await connection.execute(text("CREATE TABLE written (x INTEGER)"))


def test_pdf_path_does_not_raise_for_attachments_when_base_dir_is_none(seeded: SeededZotero):
    connector = SqliteZoteroConnector(LibrarySettings(data_dir=seeded.data_dir, base_attachment_dir=None, collection=""))

    # Total function: must return a (non-existent) Path, never raise.
    resolved = connector._pdf_path("ATTR0001", "attachments:Sub/Foo.pdf")

    assert resolved == Path("Sub/Foo.pdf")
    assert not resolved.exists()


async def test_list_items_with_pdf_filters_to_the_named_collection(seeded: SeededZotero):
    connector = SqliteZoteroConnector(
        LibrarySettings(data_dir=seeded.data_dir, base_attachment_dir=seeded.base_attachment_dir, collection=seeded.other_collection_name)
    )

    keys = {item.provider_key for item in await connector.list_items_with_pdf()}

    assert keys == {seeded.linked_abs_key}


async def test_list_items_with_pdf_empty_collection_returns_all(seeded: SeededZotero):
    connector = SqliteZoteroConnector(
        LibrarySettings(data_dir=seeded.data_dir, base_attachment_dir=seeded.base_attachment_dir, collection="")
    )

    keys = {item.provider_key for item in await connector.list_items_with_pdf()}

    assert keys == {seeded.article_key, seeded.linked_rel_key, seeded.linked_abs_key}


async def test_list_items_with_pdf_excludes_trashed_and_pdf_less(connector: SqliteZoteroConnector, seeded: SeededZotero):
    keys = {item.provider_key for item in await connector.list_items_with_pdf()}

    assert seeded.trashed_key not in keys
    assert seeded.no_pdf_key not in keys


async def test_get_returns_the_seeded_item(connector: SqliteZoteroConnector, seeded: SeededZotero):
    item = await connector.get(seeded.article_key)

    assert item is not None
    assert item.provider_key == seeded.article_key
    assert item.pdf_path == seeded.pdf_path


async def test_get_unknown_key_returns_none(connector: SqliteZoteroConnector):
    assert await connector.get("BADKEY") is None


async def test_get_trashed_key_returns_none(connector: SqliteZoteroConnector, seeded: SeededZotero):
    assert await connector.get(seeded.trashed_key) is None


@pytest.mark.skipif(
    not Path.home().joinpath("Zotero/zotero.sqlite").exists(), reason="drift guard: real ~/Zotero/zotero.sqlite not present"
)
async def test_real_library_has_the_expected_pdf_corpus():
    """Drift guard: the real-schema JOINs still run and map to well-formed items.

    Skipped automatically when the real DB is absent (CI / other machines), so no
    custom pytest marker registration is needed. Deliberately asserts NO count: the
    live library is mutable (the user edits it underneath the suite), so a threshold
    like ">= 30" is flaky. This guards against *schema* drift — the query would raise
    if Zotero renamed a column/table — and against row-mapping breakage, not knowledge base size.
    """
    connector = SqliteZoteroConnector(LibrarySettings(data_dir=Path.home() / "Zotero"))

    items = await connector.list_items_with_pdf()

    assert all(item.provider_key for item in items)
    assert all(isinstance(item.pdf_path, Path) for item in items)


class TestClaims:
    def test_zotero_claims_no_url(self, connector: SqliteZoteroConnector):
        assert connector.claims("https://arxiv.org/abs/2401.01234") is None
        assert connector.claims("https://www.youtube.com/watch?v=abc123XYZ_-") is None


class TestSourceTypeOf:
    @pytest.mark.parametrize("item_type", ["article", "blogpost", "webpage", "newspaperArticle", "MagazineArticle"])
    def test_article_shaped_types_fold_to_article(self, connector: SqliteZoteroConnector, item_type: str):
        assert connector.source_type_of(item_type) is SourceType.ARTICLE

    @pytest.mark.parametrize("item_type", ["journalArticle", "preprint", "conferencePaper", "book", "thesis", ""])
    def test_everything_else_folds_to_paper(self, connector: SqliteZoteroConnector, item_type: str):
        assert connector.source_type_of(item_type) is SourceType.PAPER


class TestBuild:
    """`SqliteZoteroConnector.build` reads the `SourceResources` object's `zotero` settings block."""

    async def test_the_block_points_the_connector_at_that_library(self, tmp_path: Path, seeded: SeededZotero):
        (tmp_path / "knowledge-base-schema.json").write_text("{}", encoding="utf-8")
        block = {
            "data_dir": str(seeded.data_dir),
            "base_attachment_dir": str(seeded.base_attachment_dir),
            "collection": seeded.other_collection_name,
        }

        connector = SqliteZoteroConnector.build(SourceResources(knowledge_base=KnowledgeBaseSettings(path=tmp_path), settings={"zotero": block}))

        keys = {item.provider_key for item in await connector.list_items_with_pdf()}
        assert keys == {seeded.linked_abs_key}

    def test_no_block_still_builds_a_connector(self, tmp_path: Path):
        (tmp_path / "knowledge-base-schema.json").write_text("{}", encoding="utf-8")

        connector = SqliteZoteroConnector.build(SourceResources(knowledge_base=KnowledgeBaseSettings(path=tmp_path)))

        assert isinstance(connector, SqliteZoteroConnector)


async def test_the_preview_lists_the_same_items_with_nothing_awaiting_resolution(connector: SqliteZoteroConnector):
    preview = await connector.preview_items()

    assert list(preview.items) == await connector.list_items_with_pdf()
    assert preview.would_resolve == ()
