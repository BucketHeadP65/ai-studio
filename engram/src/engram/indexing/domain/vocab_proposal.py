"""A pending facet-value proposal, one row of a knowledge base's `_meta/vocabulary/pending.yaml`."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict


class VocabProposal(BaseModel):
    """One pending `_meta/vocabulary/pending.yaml` row: a facet value seen while authoring, not yet registered."""

    model_config = ConfigDict(frozen=True)

    facet: str
    value: str
    page_id: str
    proposed_at: datetime
