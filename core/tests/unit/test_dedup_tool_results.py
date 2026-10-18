"""`DedupToolResults(tools=[...])`: a later result is set-differenced against what the run has BEEN SHOWN.

The helpers simulate the one thing the capability depends on: a run's message history. Each call
gets its own `tool_call_id`, and its return lands in the simulated history exactly as pydantic-ai
records it — so the tamper specs can then remove a return and watch the block release.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from pydantic_ai import FunctionToolset
from pydantic_ai.messages import ModelRequest, ToolReturnPart

from core.ai.dedup import DedupToolResults

if TYPE_CHECKING:
    from pydantic_ai.tools import RunContext
    from pydantic_ai.toolsets import AbstractToolset


def _toolset(rounds: list[object]) -> FunctionToolset[None]:
    toolset: FunctionToolset[None] = FunctionToolset()
    feed = iter(rounds)

    async def search(query: str) -> object:
        del query
        return next(feed)

    async def status() -> object:
        return next(feed)

    toolset.add_function(search, takes_ctx=False, name="search")
    toolset.add_function(status, takes_ctx=False, name="status")
    return toolset


@dataclass
class _Session:
    """One simulated run: numbered calls, every return recorded into the history the ctx carries."""

    wrapper: AbstractToolset[None]
    history: list[ModelRequest] = field(default_factory=list)
    calls: int = 0

    def _ctx(self, tool_call_id: str) -> RunContext[None]:
        from pydantic_ai.models.test import TestModel
        from pydantic_ai.tools import RunContext
        from pydantic_ai.usage import RunUsage

        return RunContext(deps=None, model=TestModel(), usage=RunUsage(), tool_call_id=tool_call_id, messages=list(self.history))

    async def call(self, name: str, args: dict[str, object]) -> object:
        self.calls += 1
        call_id = f"c{self.calls}"
        ctx = self._ctx(call_id)
        tools = await self.wrapper.get_tools(ctx)
        result = await self.wrapper.call_tool(name, args, ctx, tools[name])
        self.history.append(ModelRequest(parts=[ToolReturnPart(tool_name=name, content=result, tool_call_id=call_id)]))
        return result

    def tamper(self, call_id: str) -> None:
        """Remove one call's return from the history, as an editor or a compactor would."""
        self.history = [message for message in self.history if not any(getattr(p, "tool_call_id", None) == call_id for p in message.parts)]


def _session(rounds: list[object], **kwargs: object) -> _Session:
    capability = DedupToolResults[None](tools=["search"], **kwargs)  # type: ignore[arg-type]
    return _Session(wrapper=capability.get_wrapper_toolset(_toolset(rounds)))


async def test_call_two_is_the_set_difference_against_call_one():
    session = _session([[1, 2, 3], [1, 4, 5]])

    assert await session.call("search", {"query": "a"}) == [1, 2, 3]
    assert await session.call("search", {"query": "b"}) == [4, 5]


async def test_identity_is_the_content_itself_so_a_changed_item_still_shows():
    session = _session([[{"id": "s2", "score": 0.9}], [{"id": "s2", "score": 0.9}, {"id": "s2", "score": 0.4}]])

    await session.call("search", {"query": "a"})
    # The byte-identical copy is gone; the same unit under a NEW score is new information and stays.
    assert await session.call("search", {"query": "b"}) == [{"id": "s2", "score": 0.4}]


async def test_a_repeated_single_result_answers_with_a_note():
    session = _session([{"id": "mu.x", "body": "the text"}, {"id": "mu.x", "body": "the text"}])

    await session.call("search", {"query": "a"})
    assert await session.call("search", {"query": "b"}) == "Already shown this run. Reuse it from the earlier result above."


async def test_a_listing_that_repeats_entirely_answers_with_a_note():
    session = _session([[1, 2, 3], [1, 2, 3]])

    await session.call("search", {"query": "a"})
    # Never an empty list: an empty listing would teach "no results", which is false.
    assert await session.call("search", {"query": "b"}) == "Already shown this run. Reuse it from the earlier result above."


async def test_an_entirely_repeated_listing_with_a_marker_keeps_its_shape():
    session = _session(
        [[{"id": "s1", "body": "x"}], [{"id": "s1", "body": "x"}]],
        identity="id",
        marker=lambda item: {"id": item["id"], "note": "(shown above)"},  # type: ignore[index]
    )

    await session.call("search", {"query": "a"})
    assert await session.call("search", {"query": "b"}) == [{"id": "s1", "note": "(shown above)"}]


async def test_a_genuinely_empty_listing_stays_empty():
    session = _session([[]])

    assert await session.call("search", {"query": "a"}) == []


async def test_a_tampered_history_releases_the_block():
    session = _session([[1, 2], [1, 3], [1, 9]])

    assert await session.call("search", {"query": "a"}) == [1, 2]
    assert await session.call("search", {"query": "b"}) == [3]  # 1 is above, in call c1 — blocked
    session.tamper("c1")  # the copy of 1 is edited out of history
    assert await session.call("search", {"query": "c"}) == [1, 9]  # no visible copy → 1 passes whole again


async def test_a_released_item_blocks_again_under_its_new_carrier():
    session = _session([{"id": "x"}, {"id": "x"}, {"id": "x"}])

    await session.call("search", {"query": "a"})
    session.tamper("c1")
    assert await session.call("search", {"query": "b"}) == {"id": "x"}  # re-shown, now carried by c2
    assert isinstance(await session.call("search", {"query": "c"}), str)  # c2 is visible → blocked again


async def test_unlisted_tools_are_untouched():
    session = _session(["same", "same"])

    assert await session.call("status", {}) == "same"
    assert await session.call("status", {}) == "same"


async def test_each_run_starts_fresh():
    capability = DedupToolResults[None](tools=["search"])
    toolset = _toolset([[1], [1]])

    first_run = _Session(wrapper=capability.get_wrapper_toolset(toolset))
    second_run = _Session(wrapper=capability.get_wrapper_toolset(toolset))

    assert await first_run.call("search", {"query": "a"}) == [1]
    assert await second_run.call("search", {"query": "a"}) == [1]  # new run, nothing carried over


async def test_identity_by_field_dedups_a_reranked_item():
    session = _session([[{"id": "s2", "score": 0.9}], [{"id": "s2", "score": 0.4}, {"id": "s5", "score": 0.3}]], identity="id")

    await session.call("search", {"query": "a"})
    assert await session.call("search", {"query": "b"}) == [{"id": "s5", "score": 0.3}]


async def test_a_marker_keeps_the_repeats_place_instead_of_dropping_it():
    session = _session(
        [[{"id": "s2", "body": "x"}], [{"id": "s2", "body": "x"}, {"id": "s5", "body": "y"}]],
        identity="id",
        marker=lambda item: {"id": item["id"], "note": "(shown above)"},  # type: ignore[index]
    )

    await session.call("search", {"query": "a"})
    assert await session.call("search", {"query": "b"}) == [{"id": "s2", "note": "(shown above)"}, {"id": "s5", "body": "y"}]


async def test_the_note_is_customizable_and_receives_the_matched_key():
    session = _session([{"id": "mu.x", "body": "text"}, {"id": "mu.x", "body": "other"}], identity="id", note="You already read {key}; reuse it.")

    await session.call("search", {"query": "a"})
    assert await session.call("search", {"query": "b"}) == "You already read mu.x; reuse it."


async def test_a_note_with_literal_braces_survives_substitution():
    note = 'Already read {key}; see {"kind": "dup"} above.'
    session = _session([{"id": "mu.x", "body": "text"}, {"id": "mu.x", "body": "other"}], identity="id", note=note)

    await session.call("search", {"query": "a"})
    assert await session.call("search", {"query": "b"}) == 'Already read mu.x; see {"kind": "dup"} above.'
