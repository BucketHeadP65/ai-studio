"""The sources slice's ports: connectors, document converters, the mirror store and scanners.

Every implementation of a connector, a converter or a registered scanner lives outside engram, in
the `connectors` project, and reaches a host through `engram.sources.registry`, never through a
direct import. A connector owns its host: it is the one place its URL shapes, key derivation and
item-type vocabulary are recognized, and engram matches none of them.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from collections.abc import Mapping
    from datetime import datetime
    from pathlib import Path

    from core.cqrs.contracts import Event

    from engram.config import SourceResources
    from engram.sources.domain.conversion import ConversionResult
    from engram.sources.domain.mirror import MirrorFile, MirrorFrontmatter
    from engram.sources.domain.source_item import SourceItem
    from engram.sources.domain.source_queue import SourceType
    from engram.sources.domain.sync import SyncMirrors, SyncReport
    from engram.sources.domain.watch_list import FoundSource, WatchEntry


class Connector(Protocol):
    """One external source backend: lists items to mirror, resolves one by key, and owns its host."""

    name: str
    """The name the knowledge base declared this connector under, which every mirror it writes carries as `provider`."""

    def claims(self, url: str) -> SourceType | None:
        """The source type this connector would yield for `url`, or `None` when the URL is not its host.

        Args:
            url: The URL to recognize.

        Returns:
            The source type, or `None` for a URL on another host.
        """
        ...

    def key_for(self, url: str) -> str:
        """The stable row and mirror key for `url`, once `claims` has already returned this connector's source type for it.

        Args:
            url: A URL this connector claims.

        Returns:
            The key the URL's row and mirror share.
        """
        ...

    def source_type_of(self, item_type: str) -> SourceType:
        """Fold this connector's own `item_type` vocabulary into the source type it yields.

        Args:
            item_type: One of this connector's own item types.

        Returns:
            The source type it yields.
        """
        ...

    async def list_items_with_pdf(self) -> list[SourceItem]:
        """Every non-trashed top-level item that has a PDF attachment.

        Returns:
            The items.
        """
        ...

    async def get(self, provider_key: str) -> SourceItem | None:
        """Single item by its host-native key.

        Args:
            provider_key: The host's own item key.

        Returns:
            The item, or `None` if it is absent, trashed, or carries no PDF.
        """
        ...


class ConnectorClass(Protocol):
    """A `Connector` implementation's own class: what `engram.sources.registry` resolves a declared name to.

    `claims`, `key_for` and `source_type_of` are stateless static methods, so the class itself,
    never instantiated, satisfies `UrlClaimant`: a caller with no settings in hand gets a
    connector's real recognition and key derivation. `build` constructs a live instance once a
    host has the resources it needs. There is no `name` member, because nothing reads it off the
    bare class, only off a live instance.
    """

    @staticmethod
    def claims(url: str) -> SourceType | None:
        """The source type this connector would yield for `url`, or `None` when the URL is not its host.

        Args:
            url: The URL to recognize.

        Returns:
            The source type, or `None` for a URL on another host.
        """
        ...

    @staticmethod
    def key_for(url: str) -> str:
        """The stable row and mirror key for `url`, once `claims` has already returned this connector's source type for it.

        Args:
            url: A URL this connector claims.

        Returns:
            The key the URL's row and mirror share.
        """
        ...

    @staticmethod
    def source_type_of(item_type: str) -> SourceType:
        """Fold this connector's own `item_type` vocabulary into the source type it yields.

        Args:
            item_type: One of this connector's own item types.

        Returns:
            The source type it yields.
        """
        ...

    @classmethod
    def build(cls, resources: SourceResources) -> Connector:
        """Construct a live instance from the resources every connector might need, ignoring the ones it does not.

        Args:
            resources: The one resources value every connector and scanner is built from, so a
                connector never depends on a host's own settings.

        Returns:
            The live connector.
        """
        ...


class DocumentConverter(Protocol):
    """Turns a PDF path (or a web URL) into Markdown. Implemented by a connector, faked in tests."""

    async def to_markdown(self, pdf_path: Path, *, source_url: str | None = None) -> ConversionResult:
        """Convert the item at `pdf_path` to Markdown.

        Args:
            pdf_path: The item's PDF on disk.
            source_url: The item's web URL, a hint for converters that can prefer a web source.
                Converters that only read the PDF ignore it.

        Returns:
            The Markdown, its page count and the converter's label.

        Raises:
            ConversionError: The converter cannot produce Markdown for the item.
        """
        ...


class MirrorStore(Protocol):
    """The knowledge base's `Sources/` folder of mirror notes."""

    async def load_index(self) -> dict[str, MirrorFile]:
        """Every mirror note under `Sources/`, `_archive/` included, by its `source_key`.

        A note whose frontmatter is not a valid mirror is skipped, never guessed at.

        Returns:
            The current mirror state by key.
        """
        ...

    async def read_body(self, key: str) -> str | None:
        """The Markdown body (below frontmatter) of an existing active mirror.

        Args:
            key: The mirror's `source_key`.

        Returns:
            The body, or `None` when no active mirror carries `key`.
        """
        ...

    async def write(self, *, frontmatter: MirrorFrontmatter, body: str, assets: Mapping[str, bytes] | None = None) -> None:
        """Create or overwrite the mirror note for `frontmatter.source_key` with the given frontmatter and body, atomically.

        Args:
            frontmatter: The note's frontmatter, whose `source_key` names the mirror.
            body: The Markdown body, written verbatim.
            assets: Binary files the body references (figures), keyed by Sources-relative path.
                Each is persisted under `Sources/` alongside the note. Empty or `None` writes none.
        """
        ...

    async def soft_delete(self, key: str) -> bool:
        """Move the active note for `key` to `Sources/_archive/` and set its status to removed.

        Args:
            key: The mirror's `source_key`.

        Returns:
            `True` when a note was moved.
        """
        ...


class Scanner(Protocol):
    """One scanner's way of listing what is new in the thing it reads."""

    async def scan(self, entry: WatchEntry, *, since: datetime | None) -> list[FoundSource]:
        """Sources newer than `since`, in publication order (by convention newest first).

        A scanner caps at `entry.max_items`, keeping the head of that order.

        Args:
            entry: The watch entry to read.
            since: The entry's last scan, or `None` to answer every source.

        Returns:
            The sources found.

        Raises:
            ScanError: The entry could not be scanned.
        """
        ...


class ScannerClass(Protocol):
    """A `Scanner` implementation's own class: what `engram.sources.registry` resolves a watch entry's `scanner` to.

    `build` takes the one `SourceResources` value every connector's own `build` takes, so a scanner
    never depends on a host's settings tree. A scanner needing a resource the value carries as
    `None` refuses in `build`, with a sentence naming the resource, rather than building against
    one nobody configured.
    """

    @classmethod
    def build(cls, resources: SourceResources) -> Scanner:
        """Construct a live scanner from the resources every scanner might need, ignoring the ones it does not.

        Args:
            resources: The one resources value every connector and scanner is built from.

        Returns:
            The live scanner.
        """
        ...


class SyncHandler(Protocol):
    """The slice of `SyncMirrorsHandler` that `MultiConnectorSync` drives."""

    events: list[Event]
    """What the handler raised while it ran, for whoever dispatched it to drain."""

    async def handle(self, command: SyncMirrors) -> SyncReport:
        """Run one sync and report it.

        Args:
            command: The sync command.

        Returns:
            The run's report.
        """
        ...
