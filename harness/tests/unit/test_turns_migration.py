"""`migrate_spec` on plain dicts, the exact shape a page written before this migration carries.

No vocabulary import here either -- a fixture below is precisely what the current grammar refuses
to load, and this suite exists to prove the transform reads it anyway.
"""

from __future__ import annotations

from collections.abc import Mapping

import pytest

from harness.features.authoring.turns_migration import MigrationConflict, migrate_spec


def _node_at(document: Mapping[str, object], index: int) -> Mapping[str, object]:
    """The node at `index` in `document["nodes"]`, narrowed for indexing -- both fixtures and results are plain dicts."""
    nodes = document["nodes"]
    assert isinstance(nodes, list)
    node = nodes[index]
    assert isinstance(node, Mapping)
    return node


def _map_node(**overrides: object) -> dict[str, object]:
    node: dict[str, object] = {
        "kind": "map",
        "id": "sources",
        "work": "digest_source",
        "over": {"node": "undigested"},
        "each": "source",
        "inputs": {"collection": {"parameter": "collection"}},
        "max_parallel": 4,
        "on_error": "skip",
    }
    node.update(overrides)
    return node


def test_single_work_becomes_a_one_entry_turn_list() -> None:
    document = {"nodes": [_map_node()], "output": {"node": "sources"}}

    migrated, changed = migrate_spec(document)

    assert changed is True
    node = _node_at(migrated, 0)
    assert node["turns"] == ["digest_source"]
    assert "work" not in node
    assert "then" not in node
    assert node["inputs"] == {"collection": {"parameter": "collection"}}


def test_work_plus_then_becomes_two_turns_with_then_inputs_merged_in() -> None:
    node = _map_node(then="relate", then_each="digest", then_inputs={"reviewer": {"parameter": "reviewer"}})
    document = {"nodes": [node], "output": {"node": "sources"}}

    migrated, changed = migrate_spec(document)

    assert changed is True
    migrated_node = _node_at(migrated, 0)
    assert migrated_node["turns"] == ["digest_source", "relate"]
    assert migrated_node["inputs"] == {
        "collection": {"parameter": "collection"},
        "reviewer": {"parameter": "reviewer"},
    }
    assert "then" not in migrated_node
    assert "then_each" not in migrated_node
    assert "then_inputs" not in migrated_node


def test_a_wire_named_the_same_way_in_both_merges_silently() -> None:
    node = _map_node(then="relate", then_inputs={"collection": {"parameter": "collection"}})
    document = {"nodes": [node], "output": {"node": "sources"}}

    migrated, changed = migrate_spec(document)

    assert changed is True
    assert _node_at(migrated, 0)["inputs"] == {"collection": {"parameter": "collection"}}


def test_a_wire_named_differently_in_inputs_and_then_inputs_is_refused() -> None:
    node = _map_node(then="relate", then_inputs={"collection": {"parameter": "other_collection"}})
    document = {"nodes": [node], "output": {"node": "sources"}}

    with pytest.raises(MigrationConflict) as excinfo:
        migrate_spec(document)

    message = str(excinfo.value)
    assert "sources" in message
    assert "collection" in message


def test_migrating_twice_leaves_the_second_pass_with_nothing_to_do() -> None:
    node = _map_node(then="relate", then_inputs={"reviewer": {"parameter": "reviewer"}})
    document = {"nodes": [node], "output": {"node": "sources"}}

    once, first_changed = migrate_spec(document)
    twice, second_changed = migrate_spec(once)

    assert first_changed is True
    assert second_changed is False
    assert twice == once


def test_non_map_nodes_are_untouched() -> None:
    run_node = {"kind": "run", "id": "undigested", "work": "find_undigested", "inputs": {"collection": {"parameter": "collection"}}}
    gate_node = {"kind": "gate", "id": "check", "reviews": {"node": "undigested"}, "rejected_to": "undigested"}
    document = {"nodes": [run_node, gate_node], "output": {"node": "undigested"}}

    migrated, changed = migrate_spec(document)

    assert changed is False
    assert migrated["nodes"] == [run_node, gate_node]


def test_a_document_with_no_map_nodes_at_all_passes_through_unchanged() -> None:
    document = {
        "nodes": [{"kind": "run", "id": "a", "work": "step", "inputs": {}}],
        "output": {"node": "a"},
    }

    migrated, changed = migrate_spec(document)

    assert changed is False
    assert migrated == document
