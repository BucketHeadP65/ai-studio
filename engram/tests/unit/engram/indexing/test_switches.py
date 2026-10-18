"""The switch decision: on at the threshold, off below half, current wins between, pins override."""

import pytest

from engram.indexing.contracts import RetrievalSwitches, Signals
from engram.indexing.domain.switches import decide_switches
from engram.kb.domain.schema import RetrievalSpec

SMALL = Signals(notes=40, token_estimate=10_000, page_types=1, edges_per_note=0.1, giant_component=0.0)
BIG_LINKED = Signals(notes=5_000, token_estimate=2_000_000, page_types=8, edges_per_note=2.0, giant_component=0.9)
BIG_DUST = Signals(notes=5_000, token_estimate=2_000_000, page_types=2, edges_per_note=0.1, giant_component=0.05)
MID = Signals(notes=150, token_estimate=100_000, page_types=3, edges_per_note=1.0, giant_component=0.4)

OFF = RetrievalSwitches()


def test_small_knowledge_base_earns_nothing() -> None:
    assert decide_switches(SMALL, OFF) == OFF


def test_big_linked_knowledge_base_earns_everything() -> None:
    assert decide_switches(BIG_LINKED, OFF) == RetrievalSwitches(embeddings=True, sql=True, graph=True)


def test_big_dust_gets_vectors_but_no_graph() -> None:
    decided = decide_switches(BIG_DUST, OFF)
    assert decided.embeddings is True
    assert decided.graph is False


def test_the_band_holds_the_current_state() -> None:
    on = RetrievalSwitches(embeddings=True)
    assert decide_switches(MID, on).embeddings is True
    assert decide_switches(MID, OFF).embeddings is False


def test_shrinking_below_half_turns_off() -> None:
    on = RetrievalSwitches(embeddings=True)
    assert decide_switches(SMALL, on).embeddings is False


def test_floor_forces_on_and_ceiling_forces_off() -> None:
    assert decide_switches(SMALL, OFF, floor=frozenset({"graph"})).graph is True
    assert decide_switches(BIG_LINKED, OFF, ceiling=frozenset({"embeddings"})) == RetrievalSwitches(embeddings=True)


def test_retrieval_spec_defaults_to_fixed() -> None:
    assert RetrievalSpec().mode == "fixed"


def test_retrieval_spec_rejects_unknown_pin_names() -> None:
    with pytest.raises(ValueError, match="embeddings, sql, graph"):
        RetrievalSpec(mode="auto", floor=("teleport",))


# Boundary pins below: one pair per threshold constant, straddling the exact line the constant
# draws. Each pair is (AT the value, one step across it) so a regression flipping the comparison's
# strictness (`>` <-> `>=`, `<` <-> `<=`) or drifting the constant itself fails at least one side.

ON_EMBEDDINGS = RetrievalSwitches(embeddings=True)
ON_SQL = RetrievalSwitches(sql=True)
ON_GRAPH = RetrievalSwitches(graph=True)


def _band(
    *,
    notes: int = 150,
    token_estimate: int = 100_000,
    page_types: int = 3,
    edges_per_note: float = 1.0,
    giant_component: float = 0.4,
) -> Signals:
    """`MID`'s values, with only the field(s) a test names overridden off their in-band default.

    Every default sits inside every switch's band (neither its on- nor its off-condition holds), so
    overriding one field isolates that field's own threshold from the other switches' conditions.
    """
    return Signals(
        notes=notes,
        token_estimate=token_estimate,
        page_types=page_types,
        edges_per_note=edges_per_note,
        giant_component=giant_component,
    )


def test_embeddings_on_notes_threshold_is_exclusive() -> None:
    assert decide_switches(_band(notes=200), OFF).embeddings is False
    assert decide_switches(_band(notes=201), OFF).embeddings is True


def test_embeddings_on_token_estimate_threshold_is_exclusive() -> None:
    assert decide_switches(_band(token_estimate=150_000), OFF).embeddings is False
    assert decide_switches(_band(token_estimate=150_001), OFF).embeddings is True


def test_embeddings_off_notes_threshold_is_inclusive() -> None:
    assert decide_switches(_band(notes=100, token_estimate=50_000), ON_EMBEDDINGS).embeddings is False
    assert decide_switches(_band(notes=101, token_estimate=50_000), ON_EMBEDDINGS).embeddings is True


def test_embeddings_off_token_estimate_threshold_is_inclusive() -> None:
    assert decide_switches(_band(notes=50, token_estimate=75_000), ON_EMBEDDINGS).embeddings is False
    assert decide_switches(_band(notes=50, token_estimate=75_001), ON_EMBEDDINGS).embeddings is True


def test_sql_on_notes_threshold_is_exclusive() -> None:
    assert decide_switches(_band(notes=1000), OFF).sql is False
    assert decide_switches(_band(notes=1001), OFF).sql is True


def test_sql_on_kinds_threshold_is_exclusive() -> None:
    assert decide_switches(_band(notes=750, page_types=5), OFF).sql is False
    assert decide_switches(_band(notes=750, page_types=6), OFF).sql is True


def test_sql_off_notes_threshold_is_inclusive() -> None:
    assert decide_switches(_band(notes=500, page_types=2), ON_SQL).sql is False
    assert decide_switches(_band(notes=501, page_types=2), ON_SQL).sql is True


def test_sql_off_kinds_threshold_is_inclusive() -> None:
    assert decide_switches(_band(notes=300, page_types=2), ON_SQL).sql is False
    assert decide_switches(_band(notes=300, page_types=3), ON_SQL).sql is True


def test_graph_on_edges_per_note_threshold_is_inclusive() -> None:
    assert decide_switches(_band(notes=101, edges_per_note=1.5, giant_component=0.6), OFF).graph is True
    assert decide_switches(_band(notes=101, edges_per_note=1.49, giant_component=0.6), OFF).graph is False


def test_graph_on_giant_component_threshold_is_inclusive() -> None:
    assert decide_switches(_band(notes=101, edges_per_note=2.0, giant_component=0.6), OFF).graph is True
    assert decide_switches(_band(notes=101, edges_per_note=2.0, giant_component=0.59), OFF).graph is False


def test_graph_on_notes_threshold_is_exclusive() -> None:
    assert decide_switches(_band(notes=100, edges_per_note=2.0, giant_component=0.7), OFF).graph is False
    assert decide_switches(_band(notes=101, edges_per_note=2.0, giant_component=0.7), OFF).graph is True


def test_graph_off_edges_per_note_threshold_is_exclusive() -> None:
    assert decide_switches(_band(notes=60, edges_per_note=0.75, giant_component=0.4), ON_GRAPH).graph is True
    assert decide_switches(_band(notes=60, edges_per_note=0.74, giant_component=0.4), ON_GRAPH).graph is False


def test_graph_off_notes_threshold_is_inclusive() -> None:
    assert decide_switches(_band(notes=50, edges_per_note=1.0, giant_component=0.4), ON_GRAPH).graph is False
    assert decide_switches(_band(notes=51, edges_per_note=1.0, giant_component=0.4), ON_GRAPH).graph is True
