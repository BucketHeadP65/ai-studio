"""The posts the X scanner asks the model for, and the answer that carries them.

The names and fields of `XPost` and `XScan` form the JSON schema of the model's structured output.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from engram.sources.contracts import Resolved
from pydantic import BaseModel, ConfigDict, field_validator

if TYPE_CHECKING:
    from collections.abc import Sequence

POST_CONVERTER = "x-post"
"""The converter label a rendered X post carries."""


class XPost(BaseModel):
    """One post as the scanner reports it."""

    model_config = ConfigDict(frozen=True)

    url: str
    handle: str
    date: datetime | None = None
    text: str
    links: list[str] = []

    @field_validator("date")
    @classmethod
    def _utc_date(cls, value: datetime | None) -> datetime | None:
        """A post's date as UTC-aware, where a date with no offset reads as UTC."""
        if value is None:
            return None
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


class XScan(BaseModel):
    """The scan's structured answer."""

    model_config = ConfigDict(frozen=True)

    posts: list[XPost]


def render_post(*, url: str, handle: str, date: datetime | None, text: str, links: Sequence[str]) -> Resolved:
    """Render one X post as the markdown a mirror carries, with the facts its cache sidecar holds.

    Args:
        url: The post's URL.
        handle: The author's X handle, with or without its leading `@`.
        date: When the post was made, or `None` when unknown.
        text: The post's full text.
        links: Every link the post carries.

    Returns:
        The post's markdown, titled by the first 80 characters of its text and labelled `x-post`.
    """
    name = handle.removeprefix("@")
    when = date.isoformat() if date is not None else "unknown date"
    link_lines = "".join(f"\n- {link}" for link in links)
    markdown = f"# @{name}, {when}\n\n{text}\n" + (f"\nLinks:{link_lines}\n" if link_lines else "")
    return Resolved(
        title=text[:80], markdown=markdown, url=url, channel=name, published=date.date() if date else None, converter=POST_CONVERTER
    )
