"""What a knowledge base watches, what a scanner finds, how far one scan covered, what a scan reports, and the scan command."""

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


class EntryScan(BaseModel):
    """What one scan of a watch entry found, and how far it covered the entry's window.

    A scan that covered its window only up to a time answers that time. One that stopped on a failure also answers why.
    """

    model_config = ConfigDict(frozen=True)

    sources: list[FoundSource] = []
    """The sources found, newest first."""
    covered_until: datetime | None = None
    """Every source published after the entry's last scan and up to this time was asked for. `None` means the scan's whole window up to its own time was."""
    failure: str = ""
    """Why the scan stopped on a failure before covering its window, or empty when it did not."""
    notes: list[str] = []
    """What the scan saw that is not a failure, one line each, such as a day whose search may have held more."""


REASON_LIMIT = 300
"""The most characters a scan failure's reason keeps. A longer one is cut and ends in `...`."""

SECRET_NAMES = (
    "token",
    "key",
    "auth",
    "password",
    "passwd",
    "pwd",
    "secret",
    "sig",
    "signature",
    "session",
    "sessionid",
    "code",
    "credential",
)
"""The words that make a URL parameter secret when its name holds one of them, in any case."""

_SCHEME_USER = re.compile(r"(?P<scheme>[A-Za-z][A-Za-z0-9+.-]*:(?:\\?/){2})[^\s/\\?#'\"<>]*@")
"""A URL's scheme and its `//`, JSON-escaped or not, then its user part, which runs to the last `@` before the host."""
_HOST = r"[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+(?::\d+)?"
_BARE_USER = re.compile(
    rf"(?<![^\s'\"(<\[=:])(?P<user>[^\s/\\?#'\":@]+(?P<password>:[^\s/\\?#'\":]*)?@)"
    rf"(?={_HOST}(?P<path>[/\\;]|[?#][^\s'\"])?)"
)
"""A user part in front of a dotted host with no scheme, with the password and the path or query after the host, if any."""
_PARAMETER_NAME = r"[^=&;#?\s'\"()<>\[\]/\\]"
_SECRET_PARAMETER = re.compile(
    rf"(?P<name>[?&;#]{_PARAMETER_NAME}*?(?:{'|'.join(SECRET_NAMES)}){_PARAMETER_NAME}*=)[^&;#?\s'\"()<>\[\]]+", re.IGNORECASE
)
"""A query, fragment or `;` path parameter whose name holds a secret word, and its value."""


def _bare_user_kept(found: re.Match[str]) -> str:
    """Nothing for a user part that carries a password or stands before a path or a query, else the user part as it was."""
    return "" if found["password"] is not None or found["path"] is not None else found[0]


def without_url_secrets(text: str) -> str:
    """`text` with the user part of every URL in it dropped and the value of every secret-named parameter masked as `***`.

    A parameter is secret when its name holds a word of `SECRET_NAMES`, in a query, a fragment or a `;` path parameter.
    The rest of a URL stays as written, so two URLs that differ in an ordinary parameter stay apart. A URL with no scheme
    loses its user part when the part carries a password or a path or a query follows the host, so an email address is
    left as it is. A slash escaped for JSON, with a backslash before it, is read as a slash.

    Args:
        text: Any text, such as a failure's message or a watch entry's label.

    Returns:
        The text with those parts dropped or masked.
    """
    text = _SCHEME_USER.sub(lambda found: found["scheme"], text)
    text = _BARE_USER.sub(_bare_user_kept, text)
    return _SECRET_PARAMETER.sub(lambda found: found["name"] + "***", text)


def scan_failure_reason(message: str) -> str:
    """One readable line of why a watch entry's scan failed, built from the failure's message.

    It keeps the message's first line that is not blank, passes it through `without_url_secrets`, and cuts it to
    `REASON_LIMIT` characters.

    Args:
        message: The failure's message as the scanner raised it.

    Returns:
        The reason, or an empty string when the message holds no text.
    """
    line = next((line.strip() for line in message.splitlines() if line.strip()), "")
    line = without_url_secrets(line)
    return line if len(line) <= REASON_LIMIT else line[: REASON_LIMIT - 3] + "..."


class ScanReport(BaseModel):
    """What a scan did: sources seen, rows added in all and by watch entry, the watch entries that failed with their reasons, and notes.

    `failed` pairs each failed entry's label with its reason, one line from `scan_failure_reason`.
    """

    model_config = ConfigDict(frozen=True)

    found: int = 0
    requested: int = 0
    failed: tuple[tuple[str, str], ...] = ()
    requested_by_entry: dict[str, int] = Field(default_factory=dict)
    """The rows each watch entry added to the source queue, by the entry's label. An entry that added none is absent."""
    notes: tuple[tuple[str, str], ...] = ()
    """Each note a scan made that is not a failure, paired with its entry's label."""


class ScanWatchList(Command):
    """Scan every entry on the knowledge base's watch list and queue what is new."""
