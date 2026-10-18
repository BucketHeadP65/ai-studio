"""One embed strategy's output: an id, a granularity level, and the embed text.

Pure data, no behavior.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class EmbedStrategyUnit:
    """One embeddable unit an `EmbedStrategy` yields for an indexed note or source mirror.

    `id` is the row's primary key (matches the `kb.pages` / `sections` / `claims` /
    `raw_chunks` id scheme), `level` is the granularity tier the unit belongs to (`page`,
    `section`, `claim`, `chunk`), `text` is the exact string a caller embeds and hashes for
    the re-embed gate.
    """

    id: str
    level: str
    text: str
