"""A join hands its node every item once, after every branch that feeds it has finished.

Each test builds a plain pydantic-graph graph in a shape where a join used to fire early. The node behind that join then
ran on part of its items and ran again later on the rest. Both tests check that the node runs once with all of them.
"""

from __future__ import annotations

import asyncio

from dataclasses import dataclass, field

from pydantic_graph import GraphBuilder, StepContext
from pydantic_graph.id_types import NodeID


@dataclass
class Trace:
    """Each node behind a join, with the items it was handed, in the order the nodes ran."""

    handed: list[tuple[str, list[str]]] = field(default_factory=list)

    def handed_to(self, node: str) -> list[list[str]]:
        return [items for name, items in self.handed if name == node]


def _collect(current: list[str], item: str) -> list[str]:
    return [*current, item]


def _collect_numbers(current: list[int], item: int) -> list[int]:
    return [*current, item]


async def test_a_join_finalized_with_another_waits_for_the_task_that_join_starts() -> None:
    # When `b` finishes, `mid_join` and `tail_join` are finalized in the same pass. `mid_join` starts `mid`, which feeds
    # `tail_join`, so `tail_join` must keep waiting for it.
    builder = GraphBuilder(state_type=Trace, output_type=list[str])

    @builder.step
    async def src(ctx: StepContext[Trace, None, None]) -> str:
        return "src"

    @builder.step
    async def b(ctx: StepContext[Trace, None, str]) -> str:
        return "b"

    @builder.step
    async def mid(ctx: StepContext[Trace, None, list[str]]) -> str:
        ctx.state.handed.append(("mid", sorted(ctx.inputs)))
        return "mid"

    @builder.step
    async def tail(ctx: StepContext[Trace, None, list[str]]) -> str:
        ctx.state.handed.append(("tail", sorted(ctx.inputs)))
        return "tail"

    @builder.step
    async def last(ctx: StepContext[Trace, None, list[str]]) -> list[str]:
        ctx.state.handed.append(("last", sorted(ctx.inputs)))
        return sorted(ctx.inputs)

    mid_join = builder.join(_collect, initial_factory=list[str], node_id="mid_join")
    tail_join = builder.join(_collect, initial_factory=list[str], node_id="tail_join")
    last_join = builder.join(_collect, initial_factory=list[str], node_id="last_join")
    builder.add(
        builder.edge_from(builder.start_node).to(src),
        builder.edge_from(src).to(b, mid_join, tail_join),
        builder.edge_from(b).to(mid_join, last_join),
        builder.edge_from(mid_join).to(mid),
        builder.edge_from(mid).to(tail_join),
        builder.edge_from(tail_join).to(tail),
        builder.edge_from(tail).to(last_join),
        builder.edge_from(last_join).to(last),
        builder.edge_from(last).to(builder.end_node),
    )
    trace = Trace()

    output = await builder.build().run(state=trace)

    assert trace.handed_to("tail") == [["mid", "src"]]
    assert output == ["b", "tail"]


async def test_a_join_behind_a_map_over_an_empty_list_waits_for_that_map() -> None:
    # `over` maps an empty list, so the map's collect join `map_collect` opens at once and feeds `gather`, which feeds
    # `tail_join`. When the slow `side` branch finishes, `tail_join` must keep waiting for `gather`.
    builder = GraphBuilder(state_type=Trace, output_type=list[str])

    @builder.step
    async def src(ctx: StepContext[Trace, None, None]) -> str:
        return "src"

    @builder.step
    async def over(ctx: StepContext[Trace, None, str]) -> list[int]:
        return []

    @builder.step
    async def each(ctx: StepContext[Trace, None, int]) -> int:
        return ctx.inputs

    @builder.step
    async def gather(ctx: StepContext[Trace, None, list[int]]) -> str:
        return "gather"

    @builder.step
    async def side(ctx: StepContext[Trace, None, str]) -> str:
        await asyncio.sleep(0.05)
        return "side"

    @builder.step
    async def tail(ctx: StepContext[Trace, None, list[str]]) -> str:
        ctx.state.handed.append(("tail", sorted(ctx.inputs)))
        return "tail"

    @builder.step
    async def last(ctx: StepContext[Trace, None, list[str]]) -> list[str]:
        ctx.state.handed.append(("last", sorted(ctx.inputs)))
        return sorted(ctx.inputs)

    map_collect = builder.join(_collect_numbers, initial_factory=list[int], node_id="map_collect", parent_fork_id="map_fork")
    tail_join = builder.join(_collect, initial_factory=list[str], node_id="tail_join")
    last_join = builder.join(_collect, initial_factory=list[str], node_id="last_join")
    builder.add(builder.edge_from(builder.start_node).to(src), builder.edge_from(src).to(over, side, tail_join))
    builder.add_mapping_edge(over, each, fork_id=NodeID("map_fork"), downstream_join_id=NodeID("map_collect"))
    builder.add(
        builder.edge_from(each).to(map_collect),
        builder.edge_from(map_collect).to(gather),
        builder.edge_from(gather).to(tail_join),
        builder.edge_from(tail_join).to(tail),
        builder.edge_from(tail).to(last_join),
        builder.edge_from(side).to(last_join),
        builder.edge_from(last_join).to(last),
        builder.edge_from(last).to(builder.end_node),
    )
    trace = Trace()

    output = await builder.build().run(state=trace)

    assert trace.handed_to("tail") == [["gather", "src"]]
    assert output == ["side", "tail"]
