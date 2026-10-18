"""The units the disclosure-summary stage queues and the rows it writes."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, TypedDict


@dataclass(frozen=True, slots=True)
class SummaryUnit:
    """One page or section queued for summarization: its id, title, body text, and content hash.

    `source_hash` is the unit's `body_hash`, and the written summary row carries it unchanged. A unit
    stops being queued once a summary with a matching hash exists.
    """

    unit_id: str
    unit_type: Literal["page", "section"]
    title: str
    body: str
    source_hash: str


class SummaryUpsertRow(TypedDict):
    """The `kb.unit_summaries` upsert row `SummaryStage` hands `upsert_summary`."""

    unit_id: str
    unit_kind: str
    collection: str
    summary: str
    status: str
    model: str
    source_hash: str
