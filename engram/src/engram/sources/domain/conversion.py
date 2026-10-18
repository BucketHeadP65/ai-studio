"""What a document converter hands back for one item."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class ConversionResult(BaseModel):
    """Markdown produced from one item, plus its page count (for cost/telemetry) and the converter that made it.

    `assets` carries any binary files the Markdown references (figures), keyed by the
    Sources-relative path the body links to (e.g. `_assets/<source-key>/x1.png`). The mirror
    store persists them next to the note. Converters without figure support leave it empty.
    `title` carries the source's own title when the converter can name one from outside the
    Markdown body (e.g. a fetched page's `<title>` or `og:title`). A converter with no such signal
    leaves it `None` and a caller falls back to the Markdown itself. The value is frozen.
    """

    model_config = ConfigDict(frozen=True)

    markdown: str
    page_count: int
    converter: str
    assets: dict[str, bytes] = {}
    title: str | None = None
