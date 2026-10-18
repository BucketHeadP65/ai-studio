"""A mirror note's status, its frontmatter, and the state read back from it."""

from __future__ import annotations

from datetime import date, datetime
from enum import StrEnum
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict

if TYPE_CHECKING:
    from pydantic import JsonValue


class MirrorStatus(StrEnum):
    """Lifecycle state of a mirror note: living in `Sources/` or soft-deleted to `Sources/_archive/`."""

    ACTIVE = "active"
    REMOVED = "removed"


class MirrorFrontmatter(BaseModel):
    """The YAML written atop a mirror note. Generic, it names no connector.

    A frozen value: once built it is a stable record of what was serialised. The note on disk is
    still rewritten, and renamed on a title change, on every sync. The never-overwritten record is
    the `kb.sources_raw` row. `source_id`, `provider` and `provider_key` are the connector-neutral
    identity an index reads back into `kb.sources_raw` (`provider` holds the connector's
    registered name, `provider_key` the host's own item key). `source_key` and `source_version`
    are the mirror store's join key across every connector, and `source_link` is the item's deep
    link, empty when its connector owns none. `channel`, `published` and `meta` are optional,
    connector-supplied fields.
    """

    model_config = ConfigDict(frozen=True)

    title: str
    authors: list[str] = []
    year: int | None = None
    item_type: str
    doi: str | None = None
    url: str | None = None
    channel: str | None = None  # the publishing channel or author, when the connector has one
    published: date | None = None  # the original publish date, when the connector carries one distinct from date_added
    meta: dict[str, str] = {}  # the connector's own text metadata by key, empty when the connector carries none
    source_id: str
    provider: str
    provider_key: str
    source_key: str
    source_version: int
    source_link: str
    collections: list[str] = []
    keywords: list[str] = []
    date_added: datetime
    date_modified: datetime
    pdf_path: str
    source_hash: str
    converter: str
    status: MirrorStatus = MirrorStatus.ACTIVE
    synced: datetime

    def to_disk_fields(self) -> dict[str, JsonValue]:
        """The on-disk YAML mapping, with `channel` and `published` omitted when unset and `meta` omitted when empty.

        Every other field renders exactly as `model_dump(mode="json")` does, a `None` included as
        YAML `null`. A connector that never sets these three fields gains no keys on disk.

        Returns:
            The mapping to serialise.
        """
        data: dict[str, JsonValue] = self.model_dump(mode="json")
        if self.channel is None:
            del data["channel"]
        if self.published is None:
            del data["published"]
        if not self.meta:
            del data["meta"]
        return data


class MirrorFile(BaseModel):
    """The reconcile-relevant state read back from an existing mirror note's frontmatter.

    A frozen snapshot. `converter` falls back to `unknown` so a note naming no provenance label
    still loads. `provider` has no default and is required: a sync run's orphan pass narrows the
    index by it, so a guessed name would archive another connector's mirrors. A note naming no
    connector is not a mirror the store can address.
    """

    model_config = ConfigDict(frozen=True)

    source_key: str
    source_version: int
    source_hash: str
    provider: str
    converter: str = "unknown"
    status: MirrorStatus
