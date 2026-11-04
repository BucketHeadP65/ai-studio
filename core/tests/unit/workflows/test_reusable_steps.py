"""Copying a library's reusable steps into a workflow's inline steps: what is copied, and what is refused in which words."""

from __future__ import annotations

import pytest

from core.ai.workflows.contracts import (
    Briefing,
    FromNode,
    FromParameter,
    InlineStep,
    LibraryError,
    ParameterSpec,
    ReusableStepsRefusedError,
    RunNode,
    Shape,
    Workflow,
    copy_reusable_steps,
)


def _step(name: str) -> Briefing:
    return Briefing(
        name=name,
        instructions=f"Read the source and write one {name} note about it, quoting what it claims.",
        inputs={"source": Shape(json_schema={"type": "string"})},
        output=Shape(json_schema={"type": "string"}),
    )


def _workflow(*inline: InlineStep) -> Workflow:
    """A workflow running `digest` from one node, carrying `inline`."""
    return Workflow(
        name="digest-one",
        version=1,
        description="Digest one source.",
        parameters={"source": ParameterSpec(description="a source id", json_schema={"type": "string"})},
        inline=inline,
        nodes=(RunNode(id="d", work="digest", inputs={"source": FromParameter(parameter="source")}),),
        output=FromNode(node="d"),
    )


class FakeShelf:
    """Reusable steps by id. Any other id raises `LibraryError`, and `fetched` records every id asked for."""

    def __init__(self, steps: dict[str, InlineStep]) -> None:
        self.steps = steps
        self.fetched: list[str] = []

    async def fetch_step(self, step_id: str) -> InlineStep:
        self.fetched.append(step_id)
        if step_id not in self.steps:
            msg = f"no reusable step has the id {step_id!r}"
            raise LibraryError(msg)
        return self.steps[step_id]


async def test_each_used_step_is_appended_to_inline_in_the_order_uses_names_it() -> None:
    shelf = FakeShelf({"st.digest.v1": _step("digest"), "st.tag.v2": _step("tag")})

    copied = await copy_reusable_steps(_workflow(_step("summary")), ["st.tag.v2", "st.digest.v1"], shelf.fetch_step)

    assert [step.name for step in copied.inline] == ["summary", "tag", "digest"]
    assert shelf.fetched == ["st.tag.v2", "st.digest.v1"]


async def test_an_id_named_twice_is_refused_before_it_is_fetched_again() -> None:
    shelf = FakeShelf({"st.digest.v1": _step("digest")})

    with pytest.raises(ReusableStepsRefusedError, match=r"^'st.digest.v1' is named more than once in `uses`. Name each reusable step once.$"):
        await copy_reusable_steps(_workflow(), ["st.digest.v1", "st.digest.v1"], shelf.fetch_step)

    assert shelf.fetched == ["st.digest.v1"]


async def test_an_id_no_step_has_is_refused_as_a_step_that_cannot_be_copied() -> None:
    shelf = FakeShelf({})

    with pytest.raises(ReusableStepsRefusedError, match=r"^'st.missing.v1' did not yield a valid reusable step \(.*\). It cannot be copied into this spec.$"):
        await copy_reusable_steps(_workflow(), ["st.missing.v1"], shelf.fetch_step)


async def test_an_answer_that_is_not_a_step_is_refused_the_same_way() -> None:
    async def unreadable(step_id: str) -> InlineStep:
        msg = f"{step_id} answered no step document"
        raise TypeError(msg)

    with pytest.raises(ReusableStepsRefusedError, match="did not yield a valid reusable step"):
        await copy_reusable_steps(_workflow(), ["st.digest.v1"], unreadable)


async def test_a_step_whose_name_is_already_under_inline_is_refused() -> None:
    shelf = FakeShelf({"st.digest.v1": _step("digest")})

    with pytest.raises(
        ReusableStepsRefusedError,
        match=r"^st.digest.v1 is step 'digest', which this workflow already carries under inline. Drop one of the two$",
    ):
        await copy_reusable_steps(_workflow(_step("digest")), ["st.digest.v1"], shelf.fetch_step)


async def test_two_ids_that_hold_steps_of_one_name_are_refused() -> None:
    shelf = FakeShelf({"st.digest.v1": _step("digest"), "st.digest.v2": _step("digest")})

    with pytest.raises(ReusableStepsRefusedError, match=r"st.digest.v2 is step 'digest'"):
        await copy_reusable_steps(_workflow(), ["st.digest.v1", "st.digest.v2"], shelf.fetch_step)
