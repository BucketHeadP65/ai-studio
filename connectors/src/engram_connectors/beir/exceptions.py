"""The errors a caller of the BEIR connector handles."""

from __future__ import annotations


class BeirSettingsError(Exception):
    """The knowledge base's `_meta/beir.yaml` is missing, unreadable or names no dataset, in words the owner can act on."""


class BeirDatasetError(Exception):
    """A downloaded BEIR dataset is missing a file or holds a line that does not parse."""
