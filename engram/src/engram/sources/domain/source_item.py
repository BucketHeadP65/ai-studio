"""The connector-neutral item shape every connector maps its own item type onto.

`provider_key` is the host's own item key, the field name the mirror note and `kb.sources_raw`
persist. `source_id` is engram's own identifier, a connector's proposal, which may be the
`provider_key` verbatim.
"""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

from pydantic import BaseModel, ConfigDict


class Creator(BaseModel):
    """One author/editor of a mirrored source item."""

    model_config = ConfigDict(frozen=True)

    last_name: str
    first_name: str = ""
    creator_type: str = "author"


class SourceItem(BaseModel):
    """A top-level source-library item with a PDF attachment, the unit a connector mirrors."""

    model_config = ConfigDict(frozen=True)

    provider_key: str
    source_id: str
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
    channel: str | None = None  # the publishing channel or author, when the connector has one
    published: date | None = None  # the original publish date, when the connector carries one distinct from date_added
    date_added: datetime
    date_modified: datetime
    pdf_path: Path  # absolute path to the attachment PDF on disk
    source_link: str = ""  # a connector-owned deep link back to the item, in that host's own URI scheme, empty when none
    meta: dict[str, str] = {}  # the connector's own text metadata by key, empty when the connector carries none
