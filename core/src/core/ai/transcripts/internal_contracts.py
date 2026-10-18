"""The ports transcripts are read through: the loader of one source scheme, and the reads of a conversation's runs."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from collections.abc import Sequence

    from core.ai.transcripts.models import Transcript
    from core.ai.transcripts.read_models import RunSummary


class TranscriptLoader(Protocol):
    """Opens the transcript a locator names. The scheme it is registered under says what the locator is."""

    async def __call__(self, locator: str) -> Transcript:
        """Open the transcript at `locator`.

        A loader that cannot answer raises only the errors below, which `UNREADABLE` holds.

        Args:
            locator: The part of the source after its scheme.

        Returns:
            The transcript, whose source is the scheme and `locator`.

        Raises:
            RunEmptyError: when the source holds the run but the run recorded no step.
            LookupError: when the source holds no such run.
            FileNotFoundError: when the file `locator` names does not exist.
            ValueError: when what `locator` names does not read as a transcript.
        """
        ...


class RunsOfConversation(Protocol):
    """The runs of the conversation a run belongs to."""

    async def before(self, source: str) -> Sequence[RunSummary]:
        """The conversation's runs started before the one at `source`, newest first.

        Args:
            source: The run as a transcript source, `<scheme>:<locator>`.

        Returns:
            One summary per earlier run. Empty when the source names no run this port can read.
        """
        ...


class RunFacts(Protocol):
    """One run's own summary line, for a reader that has its source."""

    async def summary_of(self, source: str) -> RunSummary | None:
        """The summary line of the run at `source`.

        Args:
            source: The run as a transcript source, `<scheme>:<locator>`.

        Returns:
            The run's summary, or None when this port cannot read it.
        """
        ...
