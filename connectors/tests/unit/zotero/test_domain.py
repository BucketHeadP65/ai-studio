"""Unit tests for the zotero slice domain — only our own logic.

The one thing we own here is the `zotero_link` deep-link derivation. We also pin
that the two value objects are frozen. Field storage, declared defaults, and that
polyfactory can build the model are third-party behaviour and are not tested.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pydantic
import pytest

from engram.sources.contracts import Creator

from engram_connectors.zotero.contracts import ZoteroItem

_EPOCH = datetime(2024, 1, 1, tzinfo=UTC)


def _item() -> ZoteroItem:
    return ZoteroItem(
        key="ABCD1234", version=1, item_type="journalArticle", title="T", date_added=_EPOCH, date_modified=_EPOCH, pdf_path=Path("/x.pdf")
    )


def test_zotero_link_is_a_select_deep_link() -> None:
    assert _item().zotero_link == "zotero://select/library/items/ABCD1234"


def test_zotero_item_is_frozen() -> None:
    item = _item()

    with pytest.raises(pydantic.ValidationError):
        item.title = "mutated"  # type: ignore[misc]


def test_creator_is_frozen() -> None:
    creator = Creator(last_name="Doe", first_name="Jane")

    with pytest.raises(pydantic.ValidationError):
        creator.last_name = "mutated"  # type: ignore[misc]
