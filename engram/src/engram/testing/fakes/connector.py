"""An in-memory `Connector` for reconcile tests that also serves as a `ConnectorClass`.

Serves a seeded list of `SourceItem` from `list_items_with_pdf` and `preview_items`, and looks items up by
`provider_key` in `get`. `preview_items` also names the seeded `would_resolve` rows, and `listings` counts the
real listings. Seed it through the constructor. `claims`, `key_for` and `source_type_of`
answer `claims_result`, `key_for_result` and `source_type_result`, public attributes a test sets
before use. For many distinct URLs or item types, set `claims_from`, `key_for_from` or
`source_type_from` instead, a plain function consulted ahead of the constant. Pass a constructed
instance, never the bare class, wherever a `ConnectorClass` is expected, for example as a
`claimants_for` or `engram.sources.registry` `source` entry.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from engram.sources.contracts import ItemPreview, SourceType, url_key

if TYPE_CHECKING:
    from collections.abc import Callable

    from engram.config import SourceResources
    from engram.sources.contracts import SourceItem, UnresolvedItem


@dataclass
class FakeConnector:
    """Returns the seeded items. `get` looks a `provider_key` up in the same list."""

    items: list[SourceItem] = field(default_factory=list)
    # A test that seeds a `MirrorFile` or writes a mirror naming a connector passes that same name
    # here, because the sync run's orphan pass narrows by `mirror.provider == repository.name`.
    name: str = "fake"
    # Scriptable outcomes for `claims`, `key_for` and `source_type_of`, read by the matching method
    # below. A test sets one of these before calling `classify_url` or `claimants_for` through this fake.
    claims_result: SourceType | None = None
    key_for_result: str | None = None
    source_type_result: SourceType = SourceType.PAPER
    # Per-input versions of the three constants above: a plain function of the same argument,
    # consulted first when set.
    claims_from: Callable[[str], SourceType | None] | None = None
    key_for_from: Callable[[str], str] | None = None
    source_type_from: Callable[[str], SourceType] | None = None
    would_resolve: list[UnresolvedItem] = field(default_factory=list)
    """The rows `preview_items` names as awaiting resolution."""
    listings: int = 0
    """How many times `list_items_with_pdf` ran."""

    async def list_items_with_pdf(self) -> list[SourceItem]:
        """List every seeded item and count the listing. The fake treats every item as carrying a PDF.

        Returns:
            A copy of `items`.
        """
        self.listings += 1
        return list(self.items)

    async def preview_items(self) -> ItemPreview:
        """Preview the seeded items and the seeded rows awaiting resolution, counting no listing.

        Returns:
            `items` and `would_resolve`.
        """
        return ItemPreview(items=tuple(self.items), would_resolve=tuple(self.would_resolve))

    async def get(self, provider_key: str) -> SourceItem | None:
        """Look a seeded item up by its provider key.

        Args:
            provider_key: The key to look up.

        Returns:
            The seeded item with that key, or None when none was seeded.
        """
        return next((item for item in self.items if item.provider_key == provider_key), None)

    def claims(self, url: str) -> SourceType | None:
        """Answer the scripted claim for a URL.

        Args:
            url: The URL to classify.

        Returns:
            `claims_from(url)` when set, else `claims_result`, which is None by default.
        """
        if self.claims_from is not None:
            return self.claims_from(url)
        return self.claims_result

    def key_for(self, url: str) -> str:
        """Answer the scripted key for a URL.

        Args:
            url: The URL to key.

        Returns:
            `key_for_from(url)` when set, else `key_for_result`, else the generic hash key of the URL.
        """
        if self.key_for_from is not None:
            return self.key_for_from(url)
        return self.key_for_result if self.key_for_result is not None else url_key(url)

    def source_type_of(self, item_type: str) -> SourceType:
        """Answer the scripted source type for an item type.

        Args:
            item_type: The item type to fold.

        Returns:
            `source_type_from(item_type)` when set, else `source_type_result`, which is `PAPER` by default.
        """
        if self.source_type_from is not None:
            return self.source_type_from(item_type)
        return self.source_type_result

    @classmethod
    def build(cls, resources: SourceResources) -> FakeConnector:
        """Build a fresh, empty instance.

        Args:
            resources: Ignored.

        Returns:
            A new fake with no items.
        """
        del resources
        return cls()
