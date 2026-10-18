"""The Zotero item the connector reads, the library settings it reads from, and how those settings are read.

A `ZoteroItem` is the Zotero-native shape of one library item. `ZoteroItem.to_source_item` maps it onto the
`SourceItem` the `Connector` port serves.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Final

from engram.sources.contracts import Creator, SourceItem
from pydantic import BaseModel, ConfigDict

if TYPE_CHECKING:
    from engram.config import SourceResources

SETTINGS_KEY: Final = "zotero"
"""The `SourceResources.settings` block this connector's settings are read from."""


class ZoteroItem(BaseModel):
    """A top-level Zotero item that has a PDF attachment, the unit we mirror."""

    model_config = ConfigDict(frozen=True)

    key: str
    version: int
    item_type: str
    title: str
    creators: list[Creator] = []
    year: int | None = None
    doi: str | None = None
    url: str | None = None
    abstract: str | None = None
    collections: list[str] = []
    tags: list[str] = []
    date_added: datetime
    date_modified: datetime
    pdf_path: Path
    """The absolute path to the attachment PDF on disk."""

    @property
    def zotero_link(self) -> str:
        """The `zotero://select/...` deep link that opens the item in Zotero."""
        return f"zotero://select/library/items/{self.key}"

    def to_source_item(self) -> SourceItem:
        """Map the item onto the `SourceItem` the `Connector` port serves.

        `provider_key` and `source_id` are both the item's own `key`.

        Returns:
            The source item, with `source_link` set to the item's `zotero_link`.
        """
        return SourceItem(
            provider_key=self.key,
            source_id=self.key,
            version=self.version,
            item_type=self.item_type,
            title=self.title,
            creators=list(self.creators),
            year=self.year,
            doi=self.doi,
            url=self.url,
            abstract=self.abstract,
            collections=list(self.collections),
            tags=list(self.tags),
            date_added=self.date_added,
            date_modified=self.date_modified,
            pdf_path=self.pdf_path,
            source_link=self.zotero_link,
        )


class LibrarySettings(BaseModel):
    """The Zotero reference library the connector reads.

    `data_dir` is Zotero's data directory (default `~/Zotero`), which holds `zotero.sqlite` and the `storage/`
    attachment tree. `base_attachment_dir` is Zotero's "Linked Attachment Base Directory", used to resolve
    `attachments:` linked-file paths. `collection` names the one Zotero collection to mirror, and empty means the
    whole library. Every field has a default, so the settings construct with no configuration present.
    """

    data_dir: Path = Path.home() / "Zotero"
    base_attachment_dir: Path | None = None
    collection: str = ""

    @property
    def sqlite_path(self) -> Path:
        """The absolute path to `zotero.sqlite` inside the data directory."""
        return self.data_dir / "zotero.sqlite"

    @property
    def storage_dir(self) -> Path:
        """The absolute path to Zotero's `storage/` attachment tree."""
        return self.data_dir / "storage"


def library_settings(resources: SourceResources) -> LibrarySettings:
    """Read the library settings out of the connector's `zotero` settings block.

    An absent block gives the all-default library, since every field has a meaningful default.

    Args:
        resources: The `SourceResources` object whose `settings` may hold a `zotero` block.

    Returns:
        The library settings the block declares, or the all-default library when there is no block.

    Raises:
        ValidationError: The `zotero` block is present but does not validate.
    """
    return LibrarySettings.model_validate(resources.settings.get(SETTINGS_KEY, {}))
