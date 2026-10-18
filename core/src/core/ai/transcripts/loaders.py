"""The transcript loaders, by the scheme of a source string.

A source reads `<scheme>:<locator>`. The scheme names the loader, the locator is what that loader opens. Every transcript
keeps its source, so `load(transcript.source)` reopens it. A new place to load from is one loader registered under one
scheme.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from core.ai.registry import Registry
from core.ai.transcripts.exceptions import SchemeUnknownError
from core.ai.transcripts.internal_contracts import TranscriptLoader

if TYPE_CHECKING:
    from core.ai.transcripts.models import Transcript


def scheme_and_locator(source: str) -> tuple[str, str]:
    """Split a source at its first colon into its scheme and its locator.

    Args:
        source: The source, `<scheme>:<locator>`.

    Returns:
        The scheme and the locator. The locator keeps every later colon and may be empty.

    Raises:
        ValueError: when the source has no colon or its scheme is empty.
    """
    scheme, separator, locator = source.partition(":")
    if not separator or not scheme:
        msg = f"a source reads <scheme>:<locator>, not {source!r}"
        raise ValueError(msg)
    return scheme, locator


class TranscriptLoaders(Registry[str, TranscriptLoader]):
    """The transcript loaders by scheme."""

    async def load(self, source: str) -> Transcript:
        """Open the transcript at `source` with the loader registered for its scheme.

        The loader's own errors pass through unchanged.

        Args:
            source: The source, `<scheme>:<locator>`.

        Returns:
            The transcript the loader answered.

        Raises:
            ValueError: when the source has no scheme.
            SchemeUnknownError: when no loader is registered for the scheme.
        """
        scheme, locator = scheme_and_locator(source)
        try:
            loader = self.select(scheme)
        except KeyError as error:
            msg = f"no loader is registered for the scheme {scheme!r} of {source!r}"
            raise SchemeUnknownError(msg) from error
        return await loader(locator)
