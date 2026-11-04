"""The nodes a run has work in flight on, counted per report.

A map reports once per item under its own node id, so several reports of one node overlap. A
node stays running until each report it started has finished.
"""

from __future__ import annotations

from core.ai.workflows.contracts import NodesInFlight


def test_a_node_runs_until_every_report_it_started_has_finished() -> None:
    nodes = NodesInFlight()
    for _ in range(4):
        nodes.start("place_pages")

    assert [nodes.finish("place_pages") for _ in range(3)] == [False, False, False]
    assert nodes.running == ["place_pages"], "three items finished, one is still running"
    assert nodes.finish("place_pages") is True
    assert nodes.running == []


def test_running_lists_nodes_in_the_order_they_first_started() -> None:
    nodes = NodesInFlight()
    nodes.start("refresh")
    nodes.start("back")
    nodes.start("refresh")

    assert nodes.running == ["refresh", "back"]


def test_a_node_started_again_after_it_settled_runs_again() -> None:
    nodes = NodesInFlight()
    nodes.start("draft")
    assert nodes.finish("draft") is True
    nodes.start("draft")

    assert nodes.running == ["draft"]


def test_a_finish_with_nothing_in_flight_leaves_the_node_settled() -> None:
    nodes = NodesInFlight()

    assert nodes.finish("draft") is True
    assert nodes.running == []


def test_clear_forgets_every_node() -> None:
    nodes = NodesInFlight()
    nodes.start("draft")
    nodes.clear()

    assert nodes.running == []
