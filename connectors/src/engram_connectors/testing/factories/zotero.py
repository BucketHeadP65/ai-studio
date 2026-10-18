"""Polyfactory factory for the Zotero item model."""

from __future__ import annotations

from polyfactory.factories.pydantic_factory import ModelFactory

from engram_connectors.zotero.contracts import ZoteroItem


class ZoteroItemFactory(ModelFactory[ZoteroItem]):
    """Builds a random valid `ZoteroItem`.

    A test overrides any field per call, such as `key`, `version` or `pdf_path`.
    """
