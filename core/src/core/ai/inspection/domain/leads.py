"""A lead: one symptom seen at some spans in one transcript, no cause yet. What a rule produces."""

from __future__ import annotations

from hashlib import sha256
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from core.ai.transcripts.contracts import Span

if TYPE_CHECKING:
    from core.ai.transcripts.contracts import Transcript

_SPANS: TypeAdapter[tuple[Span, ...]] = TypeAdapter(tuple[Span, ...])


class Lead(BaseModel):
    """One symptom seen at some spans. A lead becomes a finding when the root causes are named."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    source: str = Field(description="The transcript it was seen in, as `<scheme>:<locator>`.")
    symptom: str = Field(pattern=r"^[a-z][a-z0-9_]*$", description="The symptom's name in the taxonomy.")
    spans: tuple[Span, ...] = Field(min_length=1, description="Where it shows, in the order the rule found them.")
    found_by: str = Field(min_length=1, description="The rule that saw it.")

    @property
    def spans_digest(self) -> str:
        """The content address of the spans: the same spans give the same digest, whatever the source or the rule."""
        return sha256(_SPANS.dump_json(self.spans)).hexdigest()

    def is_new_in(self, transcript: Transcript) -> bool:
        """Whether the lead cites a part its own run added, not only parts an earlier run of the conversation recorded first.

        A lead that cites only an earlier run's parts belongs to that run's inspection.

        Args:
            transcript: The run the lead was seen in.

        Returns:
            True when at least one span points at a part the run added.
        """
        return any(not transcript.is_from_earlier_run(span) for span in self.spans)
