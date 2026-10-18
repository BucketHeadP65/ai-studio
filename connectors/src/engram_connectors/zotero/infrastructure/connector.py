"""The Zotero connector, which reads a Zotero library from a read-only copy of `zotero.sqlite`.

It never opens the original database. Each call copies `zotero.sqlite` and any `-wal` and `-shm` files into a
private temporary directory, reads the copy read-only and removes it afterwards. It works whether or not Zotero is
running and never changes the source.
"""

from __future__ import annotations

import shutil
import tempfile

from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Final, Literal

from engram.sources.contracts import Creator, SourceType, url_key
from pydantic import BaseModel, BeforeValidator, ConfigDict, field_validator
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from engram_connectors.zotero.domain import ZoteroItem, library_settings

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from engram.config import SourceResources
    from engram.sources.contracts import Connector, SourceItem
    from sqlalchemy.ext.asyncio import AsyncConnection

    from engram_connectors.zotero.domain import LibrarySettings

# A PDF attachment with a resolvable local file: `contentType` is a PDF and
# `linkMode` is one of imported_file (0), imported_url (1) or linked_file (2),
# but NOT linked_url (3), a web link with no local file. Restricting to this set
# also excludes NULL (a row without a link mode has no resolvable file), which is
# intended. These two are Zotero schema constants, not user input.
_PDF_CONTENT_TYPE: Final = "application/pdf"
_LOCAL_FILE_LINK_MODES: tuple[Literal[0, 1, 2], ...] = (0, 1, 2)

# The attachment-filter predicate, rendered from the constants above. `_ITEMS_SQL` interpolates it on the `WHERE` line.
_LINK_MODE_LIST = ", ".join(str(mode) for mode in _LOCAL_FILE_LINK_MODES)
_PDF_ATTACHMENT_FILTER = f"attachmentData.contentType = '{_PDF_CONTENT_TYPE}'\n  AND attachmentData.linkMode IN ({_LINK_MODE_LIST})"

# Parents that (a) are not trashed and (b) own at least one matching PDF
# attachment (see `_PDF_ATTACHMENT_FILTER`). Scalar metadata is pulled with
# correlated subqueries keyed on Zotero's stable field names so one row == one
# item (no creator/collection fan-out to de-duplicate here). `attachment_key`
# is the attachment item's own key (its `storage/<key>/` folder holds imported
# files), `attachment_path` carries the kind-prefixed path resolved downstream.
_ITEMS_SQL_TEMPLATE = """
SELECT
    parent.key                                                   AS key,
    parent.version                                               AS version,
    itemTypes.typeName                                           AS item_type,
    parent.dateAdded                                             AS date_added,
    parent.dateModified                                          AS date_modified,
    attachment.key                                               AS attachment_key,
    attachmentData.path                                          AS attachment_path,
    (SELECT v.value FROM itemData d JOIN itemDataValues v ON v.valueID = d.valueID
       JOIN fields f ON f.fieldID = d.fieldID
       WHERE d.itemID = parent.itemID AND f.fieldName = 'title')        AS title,
    (SELECT v.value FROM itemData d JOIN itemDataValues v ON v.valueID = d.valueID
       JOIN fields f ON f.fieldID = d.fieldID
       WHERE d.itemID = parent.itemID AND f.fieldName = 'DOI')          AS doi,
    (SELECT v.value FROM itemData d JOIN itemDataValues v ON v.valueID = d.valueID
       JOIN fields f ON f.fieldID = d.fieldID
       WHERE d.itemID = parent.itemID AND f.fieldName = 'url')          AS url,
    (SELECT v.value FROM itemData d JOIN itemDataValues v ON v.valueID = d.valueID
       JOIN fields f ON f.fieldID = d.fieldID
       WHERE d.itemID = parent.itemID AND f.fieldName = 'date')         AS date,
    (SELECT v.value FROM itemData d JOIN itemDataValues v ON v.valueID = d.valueID
       JOIN fields f ON f.fieldID = d.fieldID
       WHERE d.itemID = parent.itemID AND f.fieldName = 'abstractNote') AS abstract
FROM itemAttachments AS attachmentData
JOIN items AS attachment ON attachment.itemID = attachmentData.itemID
JOIN items AS parent ON parent.itemID = attachmentData.parentItemID
JOIN itemTypes ON itemTypes.itemTypeID = parent.itemTypeID
WHERE __PDF_ATTACHMENT_FILTER__
  AND attachmentData.parentItemID IS NOT NULL
  AND attachmentData.itemID NOT IN (SELECT itemID FROM deletedItems)
  AND parent.itemID NOT IN (SELECT itemID FROM deletedItems)
"""

# Internal constants only (no user input), so substituting the predicate is safe.
_ITEMS_SQL = _ITEMS_SQL_TEMPLATE.replace("__PDF_ATTACHMENT_FILTER__", _PDF_ATTACHMENT_FILTER)

_CREATORS_SQL = """
SELECT creators.firstName AS first_name, creators.lastName AS last_name, creatorTypes.creatorType AS creator_type
FROM itemCreators
JOIN items ON items.itemID = itemCreators.itemID
JOIN creators ON creators.creatorID = itemCreators.creatorID
JOIN creatorTypes ON creatorTypes.creatorTypeID = itemCreators.creatorTypeID
WHERE items.key = :key
ORDER BY itemCreators.orderIndex
"""

_COLLECTIONS_SQL = """
SELECT collections.collectionName AS name
FROM collectionItems
JOIN items ON items.itemID = collectionItems.itemID
JOIN collections ON collections.collectionID = collectionItems.collectionID
WHERE items.key = :key
ORDER BY collections.collectionName
"""

_TAGS_SQL = """
SELECT tags.name AS name
FROM itemTags
JOIN items ON items.itemID = itemTags.itemID
JOIN tags ON tags.tagID = itemTags.tagID
WHERE items.key = :key
ORDER BY tags.name
"""

# Optional `collection` filter, appended to `_ITEMS_SQL` only when a non-empty
# collection name is configured (sqlalchemy `text()` rejects a bound `:collection`
# that the SQL never mentions, so the clause and its param are added together).
_COLLECTION_CLAUSE = (
    "\n  AND parent.itemID IN ("
    "SELECT ci.itemID FROM collectionItems ci"
    " JOIN collections c ON c.collectionID = ci.collectionID"
    " WHERE c.collectionName = :collection)"
)

_SIDECAR_SUFFIXES = ("-wal", "-shm")
_NO_PATH_FILE = "(no path recorded)"
_YEAR_DIGITS = 4

_ARTICLE_TYPES = frozenset({"article", "blogpost", "webpage", "newspaperarticle", "magazinearticle"})
"""Zotero item types that read as a web article. Every other type (`journalArticle`, `preprint`,
`conferencePaper`, `book` and the rest) folds to a paper."""


def _text_or_empty(value: str | float | None) -> str:
    """A driver value as text, with a missing value as the empty string."""
    return "" if value is None else str(value)


def _text_or_none(value: str | float | None) -> str | None:
    """A driver value as text, with a missing value and the empty string as `None`."""
    return None if value is None else (str(value) or None)


_Text = Annotated[str, BeforeValidator(_text_or_empty)]
_OptionalText = Annotated[str | None, BeforeValidator(_text_or_none)]


class _ItemRow(BaseModel):
    """One row of the items query, validated once where the driver hands it over."""

    model_config = ConfigDict(frozen=True)

    key: str
    version: int
    item_type: str
    date_added: datetime
    date_modified: datetime
    attachment_key: str
    attachment_path: str | None
    title: _Text
    doi: _OptionalText
    url: _OptionalText
    date: _OptionalText
    abstract: _OptionalText

    @field_validator("date_added", "date_modified")
    @classmethod
    def _utc(cls, value: datetime) -> datetime:
        """A Zotero `TIMESTAMP` is stored in UTC, so a value with no offset reads as UTC."""
        return value if value.tzinfo is not None else value.replace(tzinfo=UTC)

    @property
    def year(self) -> int | None:
        """The leading four-digit year of Zotero's `date` field, or `None` when it has none."""
        head = (self.date or "")[:_YEAR_DIGITS]
        return int(head) if len(head) == _YEAR_DIGITS and head.isdigit() else None


class _CreatorRow(BaseModel):
    """One row of the creators query."""

    model_config = ConfigDict(frozen=True)

    first_name: _Text
    last_name: _Text
    creator_type: _Text


class _NameRow(BaseModel):
    """One row of the collections or tags query."""

    model_config = ConfigDict(frozen=True)

    name: _Text


class SqliteZoteroConnector:
    """The Zotero connector, which reads a Zotero library from a read-only copy of `zotero.sqlite`."""

    name = "zotero"

    def __init__(self, settings: LibrarySettings) -> None:
        """Keep the library settings.

        Args:
            settings: Where the Zotero database and its `storage/` tree live, and which collection to read.
        """
        self._settings = settings

    @staticmethod
    def claims(url: str) -> SourceType | None:
        """Claim no URL, since Zotero mirrors its local library by item.

        Args:
            url: The URL to classify, which is ignored.

        Returns:
            `None`, for every URL.
        """
        del url
        return None

    @staticmethod
    def key_for(url: str) -> str:
        """The generic key for a URL. It is never called, because `claims` answers `None` for every URL.

        Args:
            url: The URL to key.

        Returns:
            The generic hash key of the URL.
        """
        return url_key(url)

    @staticmethod
    def source_type_of(item_type: str) -> SourceType:
        """Fold a Zotero item type into its source type.

        Args:
            item_type: The Zotero item type, compared case-insensitively after stripping whitespace.

        Returns:
            `SourceType.ARTICLE` for a web article type, and `SourceType.PAPER` for every other type.
        """
        return SourceType.ARTICLE if item_type.strip().lower() in _ARTICLE_TYPES else SourceType.PAPER

    @classmethod
    def build(cls, resources: SourceResources) -> Connector:
        """Construct the connector over the library the `zotero` settings block names.

        Args:
            resources: The `SourceResources` object whose `settings` may hold a `zotero` block.

        Returns:
            The connector, or the connector over the all-default library when there is no block.

        Raises:
            ValidationError: The `zotero` block is present but does not validate.
        """
        return cls(library_settings(resources))

    async def list_items_with_pdf(self) -> list[SourceItem]:
        """List every non-trashed top-level item that owns a non-trashed PDF attachment.

        When the `collection` setting is set, only items in that Zotero collection are listed.

        Returns:
            One source item per matching Zotero item.

        Raises:
            FileNotFoundError: `zotero.sqlite` does not exist at the configured path.
        """
        sql, params = self._items_query()
        async with self._connect() as connection:
            rows = [_ItemRow.model_validate(dict(row)) for row in (await connection.execute(text(sql), params)).mappings().all()]
            return [(await self._row_to_item(connection, row)).to_source_item() for row in rows]

    async def get(self, provider_key: str) -> SourceItem | None:
        """Read one item by its Zotero key.

        The `collection` setting applies here too, so a key outside that collection reads as `None`.

        Args:
            provider_key: The Zotero item's own `key`.

        Returns:
            The source item, or `None` when the item is absent, trashed, has no PDF or is outside the collection.

        Raises:
            FileNotFoundError: `zotero.sqlite` does not exist at the configured path.
        """
        base_sql, params = self._items_query()
        sql = text(f"{base_sql}\n  AND parent.key = :key")
        async with self._connect() as connection:
            found = (await connection.execute(sql, {**params, "key": provider_key})).mappings().one_or_none()
            if found is None:
                return None
            return (await self._row_to_item(connection, _ItemRow.model_validate(dict(found)))).to_source_item()

    def _items_query(self) -> tuple[str, dict[str, str]]:
        """Build the item list SQL and its bound values, with the collection filter when one is set."""
        collection = self._settings.collection
        # The clause and its bound value are added together, because sqlalchemy's text() refuses an unused bind.
        if collection:
            return f"{_ITEMS_SQL}{_COLLECTION_CLAUSE}", {"collection": collection}
        return _ITEMS_SQL, {}

    @asynccontextmanager
    async def _connect(self) -> AsyncGenerator[AsyncConnection]:
        """Yield a read-only connection to a throwaway copy of the live database."""
        with tempfile.TemporaryDirectory(prefix="zotero-ro-") as temp_dir:
            copy = self._copy_db(Path(temp_dir))
            engine = create_async_engine(f"sqlite+aiosqlite:///{copy}?mode=ro", connect_args={"uri": True})
            try:
                async with engine.connect() as connection:
                    yield connection
            finally:
                await engine.dispose()

    def _copy_db(self, destination_directory: Path) -> Path:
        """Copy `zotero.sqlite` (and any `-wal` / `-shm` sidecars) into `destination_directory`."""
        source = self._settings.sqlite_path
        copy = destination_directory / source.name
        shutil.copyfile(source, copy)
        for suffix in _SIDECAR_SUFFIXES:
            sidecar = source.with_name(source.name + suffix)
            if sidecar.exists():
                shutil.copyfile(sidecar, copy.with_name(copy.name + suffix))
        return copy

    async def _row_to_item(self, connection: AsyncConnection, row: _ItemRow) -> ZoteroItem:
        """Map one parent row + its related creators / collections / tags to a `ZoteroItem`."""
        creators = [
            Creator(first_name=creator.first_name, last_name=creator.last_name, creator_type=creator.creator_type)
            for creator in await self._related(connection, _CREATORS_SQL, row.key, _CreatorRow)
        ]
        collections = [collection.name for collection in await self._related(connection, _COLLECTIONS_SQL, row.key, _NameRow)]
        tags = [tag.name for tag in await self._related(connection, _TAGS_SQL, row.key, _NameRow)]
        return ZoteroItem(
            key=row.key,
            version=row.version,
            item_type=row.item_type,
            title=row.title,
            creators=creators,
            year=row.year,
            doi=row.doi,
            url=row.url,
            abstract=row.abstract,
            collections=collections,
            tags=tags,
            date_added=row.date_added,
            date_modified=row.date_modified,
            pdf_path=self._pdf_path(row.attachment_key, row.attachment_path),
        )

    def _pdf_path(self, attachment_key: str, path: str | None) -> Path:
        """Resolve a Zotero attachment path by its `storage:`, `attachments:` or absolute form. A missing path resolves to a file that does not exist."""
        if path is None:
            return self._settings.storage_dir / attachment_key / _NO_PATH_FILE
        # Never raises. An `attachments:` path with no base directory comes back bare, so the missing file is reported per item.
        if path.startswith("storage:"):
            return self._settings.storage_dir / attachment_key / path.removeprefix("storage:")
        if path.startswith("attachments:"):
            relative_path = path.removeprefix("attachments:")
            base = self._settings.base_attachment_dir
            return base / relative_path if base is not None else Path(relative_path)
        return Path(path)

    @staticmethod
    async def _related[RowT: BaseModel](connection: AsyncConnection, sql: str, key: str, row_type: type[RowT]) -> list[RowT]:
        """Run a per-item child query (creators / collections / tags) bound to `key`."""
        result = await connection.execute(text(sql), {"key": key})
        return [row_type.model_validate(dict(row)) for row in result.mappings().all()]
