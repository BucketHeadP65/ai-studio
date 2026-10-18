"""The errors a caller of the MultiHop-RAG connector handles."""

from __future__ import annotations


class MultihopRagDatasetError(Exception):
    """A MultiHop-RAG dataset file is missing from the cache, does not match its pinned hash, or does not parse.

    It is also raised when two articles share a url or a title, or a question cites an article the dataset lacks.
    """
