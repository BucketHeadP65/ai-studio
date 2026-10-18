"""A workflow document's map nodes, moved from the `work` and `then` fields to `turns`.

It reads and returns plain dicts, so it takes a raw document the current workflow model refuses to load.
Nothing here may import that model.
"""

from __future__ import annotations

from collections.abc import Mapping


class MigrationConflict(ValueError):
    """A map's `inputs` and `then_inputs` wire one name differently, so the two cannot be merged."""


_OLD_WORK = "work"
_OLD_THEN = "then"
_OLD_THEN_EACH = "then_each"
_OLD_THEN_INPUTS = "then_inputs"


def _merged_inputs(node_id: object, inputs: Mapping[str, object], then_inputs: Mapping[str, object]) -> dict[str, object]:
    """`inputs` with `then_inputs` folded in, raising `MigrationConflict` when one key is wired two ways."""
    merged = dict(inputs)
    for key, wire in then_inputs.items():
        if key in merged and merged[key] != wire:
            msg = f"map {node_id!r} wires {key!r} differently in inputs ({merged[key]!r}) and then_inputs ({wire!r})"
            raise MigrationConflict(msg)
        merged[key] = wire
    return merged


def _migrate_node(node: Mapping[str, object]) -> tuple[dict[str, object], bool]:
    """One node, migrated when it is a map with a `work` field and copied untouched otherwise, and whether it changed."""
    if node.get("kind") != "map" or _OLD_WORK not in node:
        return dict(node), False
    migrated = dict(node)
    work = migrated.pop(_OLD_WORK)
    then = migrated.pop(_OLD_THEN, None)
    migrated.pop(_OLD_THEN_EACH, None)
    then_inputs = migrated.pop(_OLD_THEN_INPUTS, None)
    migrated["turns"] = [work] if then is None else [work, then]
    if isinstance(then_inputs, Mapping):
        inputs = migrated.get("inputs")
        migrated["inputs"] = _merged_inputs(node.get("id"), inputs if isinstance(inputs, Mapping) else {}, then_inputs)
    return migrated, True


def migrate_spec(document: Mapping[str, object]) -> tuple[dict[str, object], bool]:
    """The workflow document with every map node on `turns`, and whether anything changed.

    `work: X` becomes `turns: [X]`, and `work: X, then: Y` becomes `turns: [X, Y]`.
    `then_inputs` is merged into `inputs`, and `then_each` is dropped because the turn order carries it.
    A document already on `turns` passes through unchanged.

    Args:
        document: The workflow document, read raw.

    Returns:
        A copy of the document, and whether any node changed.

    Raises:
        MigrationConflict: If a map wires one key differently in `inputs` and `then_inputs`. The message
            names the node and the key.
    """
    nodes = document.get("nodes")
    if not isinstance(nodes, list | tuple):
        return dict(document), False
    migrated_nodes: list[object] = []
    changed = False
    for node in nodes:
        if not isinstance(node, Mapping):
            migrated_nodes.append(node)
            continue
        migrated_node, node_changed = _migrate_node(node)
        migrated_nodes.append(migrated_node)
        changed = changed or node_changed
    if not changed:
        return dict(document), False
    return {**document, "nodes": migrated_nodes}, True
