"""What a knowledge base watches, what a scanner finds, what a scan reports, and the scan command."""

from __future__ import annotations

import re

from datetime import UTC, datetime

from core.cqrs.contracts import Command
from pydantic import BaseModel, ConfigDict, Field, field_validator


class WatchEntry(BaseModel):
    """One watched thing: which scanner reads it, its reference, its cap, and the last scan.

    `scanner` is a scanner's registered name (the built-in XML reader's own name, or a name an
    installed package registers under `engram.sources.scanners`), so no host is named here.
    Nothing validates it against what is actually installed at read time: a scan resolves each
    name against the scanners the host actually built and reports the ones it cannot serve, one
    entry at a time. `ref` is whatever that scanner reads, a document URL for the built-in one.
    """

    model_config = ConfigDict(frozen=True)

    scanner: str = Field(min_length=1)
    ref: str
    max_items: int = 15
    last_scanned: datetime | None = None

    @field_validator("last_scanned")
    @classmethod
    def _assume_utc(cls, value: datetime | None) -> datetime | None:
        """A naive `last_scanned` is taken as UTC, matching every stamp `stamp()` writes and `FoundSource.published`."""
        return value if value is None or value.tzinfo is not None else value.replace(tzinfo=UTC)

    @property
    def label(self) -> str:
        """`<scanner>:<ref>`, what a source-queue row records as `found_via`."""
        return f"{self.scanner}:{self.ref}"


class FoundSource(BaseModel):
    """One source a scanner found: where it is, what it is called, when it appeared.

    `published` is always UTC-aware when present, so it compares against a watch entry's own scan stamp.
    """

    model_config = ConfigDict(frozen=True)

    url: str
    title: str
    published: datetime | None = None


REASON_LIMIT = 300
"""The most characters a scan failure's reason keeps. A longer one is cut and ends in `...`."""

_URL = re.compile(r"(?P<scheme>[A-Za-z][A-Za-z0-9+.-]*://)(?:[^\s/@'\"]*@)?(?P<rest>[^\s?#'\"]*)(?:[?#][^\s'\"]*)?")
"""A URL in a message, with its user part, its query and its fragment as separate pieces to drop."""


def scan_failure_reason(message: str) -> str:
    """One readable line of why a watch entry's scan failed, built from the failure's message.

    It keeps the message's first line that is not blank, drops the user part, the query and the fragment of every URL
    in it, where a credential would sit, and cuts it to `REASON_LIMIT` characters.

    Args:
        message: The failure's message as the scanner raised it.

    Returns:
        The reason, or an empty string when the message holds no text.
    """
    line = next((line.strip() for line in message.splitlines() if line.strip()), "")
    line = _URL.sub(lambda found: found["scheme"] + found["rest"], line)
    return line if len(line) <= REASON_LIMIT else line[: REASON_LIMIT - 3] + "..."


class ScanReport(BaseModel):
    """What a scan did: sources seen, rows added in all and by watch entry, and the watch entries that failed with their reasons.

    `failed` pairs each failed entry's label with its reason, one line from `scan_failure_reason`.
    """

    model_config = ConfigDict(frozen=True)

    found: int = 0
    requested: int = 0
    failed: tuple[tuple[str, str], ...] = ()
    requested_by_entry: dict[str, int] = Field(default_factory=dict)
    """The rows each watch entry added to the source queue, by the entry's label. An entry that added none is absent."""


class ScanWatchList(Command):
    """Scan every entry on the knowledge base's watch list and queue what is new."""
