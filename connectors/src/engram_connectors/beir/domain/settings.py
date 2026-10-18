"""What a knowledge base's `_meta/beir.yaml` says: which BEIR dataset it holds and which split's judgments to write."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class BeirSettings(BaseModel):
    """The dataset a knowledge base holds and the split whose relevance judgments the connector writes."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    dataset: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]*$")
    """The BEIR dataset name, such as `scifact` or `nfcorpus`."""
    split: str = Field(default="test", pattern=r"^[a-z0-9_-]+$")
    """The split whose `qrels/<split>.tsv` the judgments file is written from."""
