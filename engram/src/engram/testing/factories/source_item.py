"""Polyfactory fixtures for the vendor-neutral `SourceItem` port shape."""

from __future__ import annotations

from polyfactory.factories.pydantic_factory import ModelFactory

from engram.sources.contracts import SourceItem


class SourceItemFactory(ModelFactory[SourceItem]):
    """A random valid `SourceItem`. Override per test, for example `provider_key=`, `version=` or `pdf_path=`.

    A test seeding a single provider key passes the same value for both `provider_key` and
    `source_id`, for example `SourceItemFactory.build(provider_key="ITEM0001", source_id="ITEM0001")`.
    """

    @classmethod
    def meta(cls) -> dict[str, str]:
        """Build the item's metadata.

        Returns:
            An empty mapping, so a built item carries no metadata unless a test passes `meta=`.
        """
        return {}
