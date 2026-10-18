"""Synthetic Zotero `sqlite` builder for the connector tests.

Holds the synthetic Zotero `sqlite` builder used by `test_connector`. The
fixture DB is created with stdlib `sqlite3` (synchronous) because it is test
*setup*, not the async read path the production adapter exercises.

Scoped to the zotero unit directory: only `SqliteZoteroConnector`'s tests need it, so it
rides as a per-directory `conftest` rather than polluting the shared root fixtures.
"""

from __future__ import annotations

import sqlite3

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

# Minimal subset of Zotero's schema — only the tables/columns the repository
# JOINs touch. Column names mirror the real `zotero.sqlite` (verified against
# ~/Zotero) so the production query and this fixture agree.
_SCHEMA = """
CREATE TABLE itemTypes (itemTypeID INTEGER PRIMARY KEY, typeName TEXT);
CREATE TABLE items (
    itemID INTEGER PRIMARY KEY,
    itemTypeID INT,
    dateAdded TIMESTAMP,
    dateModified TIMESTAMP,
    key TEXT,
    version INT
);
CREATE TABLE itemAttachments (
    itemID INTEGER PRIMARY KEY,
    parentItemID INT,
    contentType TEXT,
    linkMode INT,
    path TEXT
);
CREATE TABLE fields (fieldID INTEGER PRIMARY KEY, fieldName TEXT);
CREATE TABLE itemDataValues (valueID INTEGER PRIMARY KEY, value);
CREATE TABLE itemData (itemID INT, fieldID INT, valueID INT);
CREATE TABLE creatorTypes (creatorTypeID INTEGER PRIMARY KEY, creatorType TEXT);
CREATE TABLE creators (creatorID INTEGER PRIMARY KEY, firstName TEXT, lastName TEXT, fieldMode INT);
CREATE TABLE itemCreators (itemID INT, creatorID INT, creatorTypeID INT, orderIndex INT);
CREATE TABLE collections (collectionID INTEGER PRIMARY KEY, collectionName TEXT, key TEXT);
CREATE TABLE collectionItems (collectionID INT, itemID INT, orderIndex INT);
CREATE TABLE tags (tagID INTEGER PRIMARY KEY, name TEXT);
CREATE TABLE itemTags (itemID INT, tagID INT, type INT);
CREATE TABLE deletedItems (itemID INTEGER PRIMARY KEY, dateDeleted TIMESTAMP);
"""

_FIELD_IDS = {"title": 1, "abstractNote": 2, "date": 6, "url": 13, "DOI": 59}


@dataclass
class SeededZotero:
    """Handles + the asserted values for the synthetic Zotero library.

    `data_dir` points `ZoteroSettings(data_dir=...)` at the fixture: it holds
    `zotero.sqlite` and the `storage/<attachment-key>/<file>` tree.
    """

    data_dir: Path
    article_key: str
    pdf_path: Path
    title: str
    doi: str
    url: str
    year: int
    creator_names: list[tuple[str, str]] = field(default_factory=list)
    collection_name: str = ""
    tag_name: str = ""
    trashed_key: str = ""
    no_pdf_key: str = ""
    # --- linked-file attachments (rev-1 scope expansion) ------------------
    # `_build` always populates these; `| None` defaults only satisfy the
    # dataclass rule that defaulted fields cannot precede non-defaulted ones.
    base_attachment_dir: Path | None = None
    linked_rel_key: str = ""
    linked_rel_pdf_path: Path | None = None
    linked_abs_key: str = ""
    linked_abs_pdf_path: Path | None = None
    linked_url_key: str = ""
    # --- collection-filter handles ---------------------------------------
    other_collection_name: str = ""
    other_collection_key: str = ""


def _insert_value(cursor: sqlite3.Cursor, value_id: int, value: str) -> None:
    cursor.execute("INSERT INTO itemDataValues (valueID, value) VALUES (?, ?)", (value_id, value))


def _set_field(cursor: sqlite3.Cursor, *, item_id: int, field_name: str, value_id: int, value: str) -> None:
    _insert_value(cursor, value_id, value)
    cursor.execute("INSERT INTO itemData (itemID, fieldID, valueID) VALUES (?, ?, ?)", (item_id, _FIELD_IDS[field_name], value_id))


@dataclass(frozen=True)
class _LinkedSeed:
    """Handles for the linked-file / linked-url rows seeded by :func:`_seed_linked_attachments`."""

    base_attachment_dir: Path
    linked_rel_key: str
    linked_rel_pdf_path: Path
    linked_abs_key: str
    linked_abs_pdf_path: Path
    linked_url_key: str
    other_collection_name: str
    other_collection_key: str


def _seed_excluded_items(cursor: sqlite3.Cursor) -> tuple[str, str]:
    """Seed a trashed top-level PDF item and a PDF-less item, both of which the repository must drop.

    Returns `(trashed_key, no_pdf_key)` so the exclusion test can assert neither
    key appears in the listing.
    """
    trashed_key = "TRSH0001"
    cursor.execute(
        "INSERT INTO items (itemID, itemTypeID, dateAdded, dateModified, key, version) VALUES (3, 1, ?, ?, ?, 1)",
        ("2024-01-01 10:00:00", "2024-01-01 10:00:00", trashed_key),
    )
    _set_field(cursor, item_id=3, field_name="title", value_id=30, value="Trashed Paper")
    cursor.execute(
        "INSERT INTO items (itemID, itemTypeID, dateAdded, dateModified, key, version) VALUES (4, 2, ?, ?, 'ATT00002', 1)",
        ("2024-01-01 10:00:00", "2024-01-01 10:00:00"),
    )
    cursor.execute(
        "INSERT INTO itemAttachments (itemID, parentItemID, contentType, linkMode, path) "
        "VALUES (4, 3, 'application/pdf', 1, 'storage:trash.pdf')"
    )
    cursor.execute("INSERT INTO deletedItems (itemID, dateDeleted) VALUES (3, '2024-03-03 09:00:00')")

    no_pdf_key = "NOPDF001"
    cursor.execute(
        "INSERT INTO items (itemID, itemTypeID, dateAdded, dateModified, key, version) VALUES (5, 1, ?, ?, ?, 1)",
        ("2024-01-01 10:00:00", "2024-01-01 10:00:00", no_pdf_key),
    )
    _set_field(cursor, item_id=5, field_name="title", value_id=50, value="PDF-less Paper")
    return trashed_key, no_pdf_key


def _seed_linked_attachments(cursor: sqlite3.Cursor, data_dir: Path) -> _LinkedSeed:
    """Seed two linked-file parents (base-relative + absolute), a linked-url parent, and a 2nd collection.

    The linked-url parent (`linkMode=3`) has no local file and must be excluded
    by the repository; the second collection holds only the absolute-linked parent
    (itemID 8) so a collection filter can be asserted against it. Returns the keys
    and the on-disk paths each linked attachment should resolve to.
    """
    base_attachment_dir = data_dir / "base"
    linked_rel_key, linked_rel_relpath = "LNKR0001", "Sub/Foo.pdf"
    cursor.execute(
        "INSERT INTO items (itemID, itemTypeID, dateAdded, dateModified, key, version) VALUES (6, 1, ?, ?, ?, 1)",
        ("2024-01-01 10:00:00", "2024-01-01 10:00:00", linked_rel_key),
    )
    _set_field(cursor, item_id=6, field_name="title", value_id=60, value="Linked Relative Paper")
    cursor.execute(
        "INSERT INTO items (itemID, itemTypeID, dateAdded, dateModified, key, version) VALUES (7, 2, ?, ?, 'ATTR0001', 1)",
        ("2024-01-01 10:00:00", "2024-01-01 10:00:00"),
    )
    cursor.execute(
        "INSERT INTO itemAttachments (itemID, parentItemID, contentType, linkMode, path) VALUES (7, 6, 'application/pdf', 2, ?)",
        (f"attachments:{linked_rel_relpath}",),
    )

    linked_abs_key = "LNKA0001"
    linked_abs_pdf_path = data_dir / "abs" / "Bar.pdf"
    cursor.execute(
        "INSERT INTO items (itemID, itemTypeID, dateAdded, dateModified, key, version) VALUES (8, 1, ?, ?, ?, 1)",
        ("2024-01-01 10:00:00", "2024-01-01 10:00:00", linked_abs_key),
    )
    _set_field(cursor, item_id=8, field_name="title", value_id=80, value="Linked Absolute Paper")
    cursor.execute(
        "INSERT INTO items (itemID, itemTypeID, dateAdded, dateModified, key, version) VALUES (9, 2, ?, ?, 'ATTA0001', 1)",
        ("2024-01-01 10:00:00", "2024-01-01 10:00:00"),
    )
    cursor.execute(
        "INSERT INTO itemAttachments (itemID, parentItemID, contentType, linkMode, path) VALUES (9, 8, 'application/pdf', 2, ?)",
        (str(linked_abs_pdf_path),),
    )

    linked_url_key = "LNKU0001"
    cursor.execute(
        "INSERT INTO items (itemID, itemTypeID, dateAdded, dateModified, key, version) VALUES (10, 1, ?, ?, ?, 1)",
        ("2024-01-01 10:00:00", "2024-01-01 10:00:00", linked_url_key),
    )
    _set_field(cursor, item_id=10, field_name="title", value_id=100, value="Linked URL Paper")
    cursor.execute(
        "INSERT INTO items (itemID, itemTypeID, dateAdded, dateModified, key, version) VALUES (11, 2, ?, ?, 'ATTU0001', 1)",
        ("2024-01-01 10:00:00", "2024-01-01 10:00:00"),
    )
    cursor.execute(
        "INSERT INTO itemAttachments (itemID, parentItemID, contentType, linkMode, path) "
        "VALUES (11, 10, 'application/pdf', 3, 'https://example.org/paper.pdf')"
    )

    other_collection_name, other_collection_key = "Other Papers", "COLL0002"
    cursor.execute(
        "INSERT INTO collections (collectionID, collectionName, key) VALUES (2, ?, ?)", (other_collection_name, other_collection_key)
    )
    cursor.execute("INSERT INTO collectionItems (collectionID, itemID, orderIndex) VALUES (2, 8, 0)")

    return _LinkedSeed(
        base_attachment_dir=base_attachment_dir,
        linked_rel_key=linked_rel_key,
        linked_rel_pdf_path=base_attachment_dir / "Sub" / "Foo.pdf",
        linked_abs_key=linked_abs_key,
        linked_abs_pdf_path=linked_abs_pdf_path,
        linked_url_key=linked_url_key,
        other_collection_name=other_collection_name,
        other_collection_key=other_collection_key,
    )


def _build(data_dir: Path) -> SeededZotero:
    storage_dir = data_dir / "storage"
    storage_dir.mkdir(parents=True, exist_ok=True)

    connection = sqlite3.connect(data_dir / "zotero.sqlite")
    try:
        cursor = connection.cursor()
        cursor.executescript(_SCHEMA)

        cursor.execute("INSERT INTO itemTypes (itemTypeID, typeName) VALUES (1, 'journalArticle')")
        cursor.execute("INSERT INTO itemTypes (itemTypeID, typeName) VALUES (2, 'attachment')")
        cursor.execute("INSERT INTO creatorTypes (creatorTypeID, creatorType) VALUES (1, 'author')")
        cursor.executemany("INSERT INTO fields (fieldID, fieldName) VALUES (?, ?)", [(fid, name) for name, fid in _FIELD_IDS.items()])

        # --- the one valid article (parent) with a PDF attachment -------------
        article_key = "ART00001"
        cursor.execute(
            "INSERT INTO items (itemID, itemTypeID, dateAdded, dateModified, key, version) VALUES (?, 1, ?, ?, ?, ?)",
            (1, "2024-01-01 10:00:00", "2024-02-02 12:00:00", article_key, 7),
        )
        title = "Retrieval-Augmented Generation"
        doi = "10.1234/rag.2023"
        url = "https://example.org/rag"
        # Zotero stores `date` as "<sortable> <userinput>"; leading 4 chars = year.
        _set_field(cursor, item_id=1, field_name="title", value_id=10, value=title)
        _set_field(cursor, item_id=1, field_name="DOI", value_id=11, value=doi)
        _set_field(cursor, item_id=1, field_name="url", value_id=12, value=url)
        _set_field(cursor, item_id=1, field_name="date", value_id=13, value="2023-05-00 2023-05")
        _set_field(cursor, item_id=1, field_name="abstractNote", value_id=14, value="An abstract.")

        # attachment item: its own `items` row carries the storage-folder key.
        attachment_key = "ATT00001"
        pdf_file = "rag.pdf"
        cursor.execute(
            "INSERT INTO items (itemID, itemTypeID, dateAdded, dateModified, key, version) VALUES (?, 2, ?, ?, ?, ?)",
            (2, "2024-01-01 10:00:00", "2024-01-01 10:00:00", attachment_key, 7),
        )
        # linkMode 0 = imported_file (`IN (0,1,2)` excludes NULL, so set it explicitly).
        cursor.execute(
            "INSERT INTO itemAttachments (itemID, parentItemID, contentType, linkMode, path) VALUES (?, ?, 'application/pdf', 0, ?)",
            (2, 1, f"storage:{pdf_file}"),
        )
        pdf_dir = storage_dir / attachment_key
        pdf_dir.mkdir(parents=True, exist_ok=True)
        pdf_path = pdf_dir / pdf_file
        pdf_path.write_bytes(b"%PDF-1.7 dummy")

        # two creators, one collection, one tag for the valid article
        creator_names = [("Ada", "Lovelace"), ("Alan", "Turing")]
        for creator_id, (first, last) in enumerate(creator_names, start=1):
            cursor.execute(
                "INSERT INTO creators (creatorID, firstName, lastName, fieldMode) VALUES (?, ?, ?, 0)", (creator_id, first, last)
            )
            cursor.execute(
                "INSERT INTO itemCreators (itemID, creatorID, creatorTypeID, orderIndex) VALUES (1, ?, 1, ?)", (creator_id, creator_id - 1)
            )
        collection_name = "RAG Papers"
        cursor.execute("INSERT INTO collections (collectionID, collectionName, key) VALUES (1, ?, 'COLL0001')", (collection_name,))
        cursor.execute("INSERT INTO collectionItems (collectionID, itemID, orderIndex) VALUES (1, 1, 0)")
        tag_name = "rag"
        cursor.execute("INSERT INTO tags (tagID, name) VALUES (1, ?)", (tag_name,))
        cursor.execute("INSERT INTO itemTags (itemID, tagID, type) VALUES (1, 1, 0)")

        # a trashed top-level PDF item and a PDF-less item, both excluded by the repo.
        trashed_key, no_pdf_key = _seed_excluded_items(cursor)

        # linked-file (relative + absolute), linked-url (excluded), and a 2nd collection.
        linked = _seed_linked_attachments(cursor, data_dir)

        connection.commit()
    finally:
        connection.close()

    return SeededZotero(
        data_dir=data_dir,
        article_key=article_key,
        pdf_path=pdf_path,
        title=title,
        doi=doi,
        url=url,
        year=2023,
        creator_names=creator_names,
        collection_name=collection_name,
        tag_name=tag_name,
        trashed_key=trashed_key,
        no_pdf_key=no_pdf_key,
        base_attachment_dir=linked.base_attachment_dir,
        linked_rel_key=linked.linked_rel_key,
        linked_rel_pdf_path=linked.linked_rel_pdf_path,
        linked_abs_key=linked.linked_abs_key,
        linked_abs_pdf_path=linked.linked_abs_pdf_path,
        linked_url_key=linked.linked_url_key,
        other_collection_name=linked.other_collection_name,
        other_collection_key=linked.other_collection_key,
    )


@pytest.fixture
def zotero_sqlite_factory(tmp_path: Path) -> Callable[[], SeededZotero]:
    """Return a builder that materialises a synthetic Zotero data dir under `tmp_path`.

    The builder seeds one valid `journalArticle` (imported PDF attachment + 2
    creators + 1 collection + 1 tag), one trashed top-level item, one item with no
    PDF, two linked-file items (one `attachments:` base-relative, one absolute
    path), a linked-URL item (must be excluded), and a second collection holding
    only the absolute-linked parent. It writes the dummy PDF under
    `storage/<attachment-key>/` and returns the handles/values the tests assert on.
    """

    def build() -> SeededZotero:
        return _build(tmp_path)

    return build
