"""The auto-retrieval switch decision: knowledge base signals in, retrieval switches out.

Each switch (`embeddings`, `sql`, `graph`) has an on-condition and a separate, looser
off-condition. `decide_switches` turns a switch on when its on-condition holds, turns it off
only when its off-condition holds, and leaves it exactly as `current` had it in the band between
the two. `floor` then forces named switches on regardless, and `ceiling`, when given, forces every
switch not named in it off, overriding both the thresholds and the floor.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict

type SwitchName = Literal["embeddings", "sql", "graph"]

SWITCH_NAMES: tuple[SwitchName, ...] = ("embeddings", "sql", "graph")

# Seed thresholds. Each switch has a wide band between its on-condition and its looser off threshold,
# and `current` carries through that band unchanged.
EMBEDDINGS_ON_NOTES = 200
EMBEDDINGS_ON_TOKEN_ESTIMATE = 150_000
EMBEDDINGS_OFF_NOTES = 100
EMBEDDINGS_OFF_TOKEN_ESTIMATE = 75_000

SQL_ON_NOTES = 1000
SQL_ON_KINDS = 5
SQL_OFF_NOTES = 500
SQL_OFF_KINDS = 2

GRAPH_ON_EDGES_PER_NOTE = 1.5
GRAPH_ON_GIANT_COMPONENT = 0.6
GRAPH_ON_NOTES = 100
GRAPH_OFF_EDGES_PER_NOTE = 0.75
GRAPH_OFF_NOTES = 50


class RetrievalSwitches(BaseModel):
    """Which of the three retrieval capabilities are active, decided or pinned, plus the privacy flag.

    `embeddings`, `sql` and `graph` are the decided switches `decide_switches` answers. `private`
    is not decided from signals at all: it is the knowledge base's own `retrieval.private` declaration,
    carried here so the indexed fingerprint records it. `True` means this collection stays out
    of any host's library over several knowledge bases, and the api surface serves its rows only to a
    session whose own collection it is.
    """

    model_config = ConfigDict(frozen=True)

    embeddings: bool = False
    sql: bool = False
    graph: bool = False
    private: bool = False


@dataclass(frozen=True)
class Signals:
    """The knowledge base measurements `decide_switches` reads. None of them is a switch state.

    `notes` counts every `kb.pages` row, including the orientation pages such as `api.about`, which carry
    no edges. `edges_per_note` and `giant_component` count only resolved relations (`kb.edges`), never
    dangling links (`kb.link_refs`).
    """

    notes: int
    token_estimate: int
    page_types: int
    edges_per_note: float
    giant_component: float


def _decide_embeddings(signals: Signals, *, current: bool) -> bool:
    if signals.notes > EMBEDDINGS_ON_NOTES or signals.token_estimate > EMBEDDINGS_ON_TOKEN_ESTIMATE:
        return True
    if signals.notes <= EMBEDDINGS_OFF_NOTES and signals.token_estimate <= EMBEDDINGS_OFF_TOKEN_ESTIMATE:
        return False
    return current


def _decide_sql(signals: Signals, *, current: bool) -> bool:
    if signals.notes > SQL_ON_NOTES or signals.page_types > SQL_ON_KINDS:
        return True
    if signals.notes <= SQL_OFF_NOTES and signals.page_types <= SQL_OFF_KINDS:
        return False
    return current


def _decide_graph(signals: Signals, *, current: bool) -> bool:
    if (
        signals.edges_per_note >= GRAPH_ON_EDGES_PER_NOTE
        and signals.giant_component >= GRAPH_ON_GIANT_COMPONENT
        and signals.notes > GRAPH_ON_NOTES
    ):
        return True
    if signals.edges_per_note < GRAPH_OFF_EDGES_PER_NOTE or signals.notes <= GRAPH_OFF_NOTES:
        return False
    return current


def decide_switches(
    signals: Signals,
    current: RetrievalSwitches,
    *,
    floor: frozenset[str] = frozenset(),
    ceiling: frozenset[str] | None = None,
) -> RetrievalSwitches:
    """Decide the next `RetrievalSwitches` from knowledge base signals, the current state, and pins.

    Args:
        signals: The knowledge base measurements to compare with the thresholds.
        current: The switch state to carry through each switch's wide band.
        floor: Switch names forced on regardless of signals.
        ceiling: When given, the switch names allowed to be on. Every name not in it is forced off,
            overriding both the thresholds and `floor`.

    Returns:
        The decided switches: thresholds and the band first, then `floor`, then `ceiling`.
    """
    decided = {
        "embeddings": _decide_embeddings(signals, current=current.embeddings),
        "sql": _decide_sql(signals, current=current.sql),
        "graph": _decide_graph(signals, current=current.graph),
    }
    for name in floor:
        decided[name] = True
    if ceiling is not None:
        for name in decided:
            if name not in ceiling:
                decided[name] = False
    return RetrievalSwitches(**decided)
