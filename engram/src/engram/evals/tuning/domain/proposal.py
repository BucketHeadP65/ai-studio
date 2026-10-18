"""The proposer's answer: one full revised retrieval policy and the rationale for its change."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from engram.evals.tuning.policy import RetrievalPolicy


class ProposedPolicy(BaseModel):
    """The proposer agent's structured output: a full revised policy plus why it should help.

    The model carries every unchanged knob over verbatim.
    """

    model_config = ConfigDict(frozen=True)

    policy: RetrievalPolicy
    """The full revised policy. Its strategy is validated against the evaluation strategies."""
    rationale: str
    """The one knob changed and the diagnostic evidence for it."""
