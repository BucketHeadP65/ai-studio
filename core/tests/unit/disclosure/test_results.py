"""Unit tests for the consumer-side payload-disclosure capability (`core.ai.disclosure.result_disclosure`).

`ResultDisclosure` collapses tool results carrying disclosable instances to ref+summary stubs
rendered as one `DisclosureEnvelope`, holds the full records run-scoped, and serves them back
through the framework-managed `load_result` tool. These specs drive the per-run wrapper directly
(`get_tools`/`call_tool`), mirroring `test_capability.py`'s style rather than a full agent run.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from pydantic import BaseModel
from pydantic_ai import FunctionToolset, ToolReturn
from pydantic_ai.exceptions import ModelRetry, UserError
from pydantic_ai.models.test import TestModel
from pydantic_ai.tools import RunContext
from pydantic_ai.usage import RunUsage

from core.ai.disclosure.contracts import Binding, ChildSummary, DisclosureEnvelope, ResultDisclosure

if TYPE_CHECKING:
    from pydantic_ai.toolsets import AbstractToolset


class Animal(BaseModel):
    """Structural convention via a property: `disclosure_summary` computed from the record."""

    name: str
    race: str
    age: int
    description: str

    @property
    def disclosure_summary(self) -> str:
        return f"{self.name}: {self.race}, {self.age}y — {self.description}"


class Note(BaseModel):
    """Structural convention via plain fields, including a bound title."""

    disclosure_title: str
    disclosure_summary: str
    body: str


class Recipe(BaseModel):
    """No structural marker; disclosable only through an explicit `Binding`."""

    label: str
    blurb: str
    steps: list[str]


class SpicyRecipe(Recipe):
    """A subclass, for binding-inheritance checks."""


def _animals() -> list[Animal]:
    return [
        Animal(name="Rex", race="dog", age=4, description="chases the mail"),
        Animal(name="Mia", race="cat", age=2, description="sleeps on keyboards"),
    ]


def _ctx() -> RunContext[None]:
    return RunContext(deps=None, model=TestModel(), usage=RunUsage())


_RESULTS: dict[str, object] = {
    "animals": _animals(),
    "one_animal": _animals()[0],
    "recipes": [Recipe(label="pie", blurb="a savoury pie", steps=["mix", "bake"])],
    "spicy": [SpicyRecipe(label="hot pie", blurb="the same, hotter", steps=["mix", "chili", "bake"])],
    "notes": [Note(disclosure_title="daily", disclosure_summary="what happened today", body="long body " * 50)],
    "mixed": [_animals()[0], "just a string"],
    "plain": "nothing disclosable",
    "empty": [],
    "wrapped_animals": ToolReturn(return_value=_animals(), metadata={"source": "test"}),
}


def _toolset() -> FunctionToolset[None]:
    """One scripted tool per `_RESULTS` entry, each returning its canned value."""
    toolset: FunctionToolset[None] = FunctionToolset()
    for name, value in _RESULTS.items():

        async def tool(canned: object = value) -> object:
            return canned

        toolset.add_function(tool, takes_ctx=False, name=name, description=name)
    return toolset


async def _call(toolset: AbstractToolset[None], name: str, args: dict[str, object]) -> object:
    ctx = _ctx()
    tools = await toolset.get_tools(ctx)
    return await toolset.call_tool(name, args, ctx, tools[name])


def _wrapper(capability: ResultDisclosure[None]) -> AbstractToolset[None]:
    wrapper = capability.get_wrapper_toolset(_toolset())
    assert wrapper is not None
    return wrapper


class TestCollapse:
    async def test_a_homogeneous_marked_list_collapses_to_one_envelope_of_stubs(self):
        wrapper = _wrapper(ResultDisclosure[None]())

        result = await _call(wrapper, "animals", {})

        assert isinstance(result, DisclosureEnvelope)
        assert result.kind == "results"
        assert result.mode == "strict"
        assert len(result.children) == 2
        rex = result.children[0]
        assert isinstance(rex, ChildSummary)
        assert rex.ref.startswith("result:")
        assert rex.kind == "Animal"
        assert rex.title == ""  # no bound title, no fallback — identity lives in the summary
        assert rex.summary == "Rex: dog, 4y — chases the mail"

    async def test_a_single_marked_instance_collapses_the_same_way(self):
        wrapper = _wrapper(ResultDisclosure[None]())

        result = await _call(wrapper, "one_animal", {})

        assert isinstance(result, DisclosureEnvelope)
        assert len(result.children) == 1

    async def test_a_structurally_bound_title_appears_on_the_stub(self):
        wrapper = _wrapper(ResultDisclosure[None]())

        result = await _call(wrapper, "notes", {})

        assert isinstance(result, DisclosureEnvelope)
        assert result.children[0].title == "daily"
        assert result.children[0].summary == "what happened today"

    async def test_unmarked_results_pass_through_untouched(self):
        wrapper = _wrapper(ResultDisclosure[None]())

        assert await _call(wrapper, "plain", {}) == "nothing disclosable"
        assert await _call(wrapper, "recipes", {}) == [Recipe(label="pie", blurb="a savoury pie", steps=["mix", "bake"])]

    async def test_a_mixed_sequence_passes_through_untouched(self):
        wrapper = _wrapper(ResultDisclosure[None]())

        result = await _call(wrapper, "mixed", {})

        assert isinstance(result, list)
        assert result[1] == "just a string"

    async def test_an_empty_sequence_passes_through_untouched(self):
        wrapper = _wrapper(ResultDisclosure[None]())

        assert await _call(wrapper, "empty", {}) == []

    async def test_a_tool_return_is_collapsed_in_place_keeping_content_and_metadata(self):
        wrapper = _wrapper(ResultDisclosure[None]())

        result = await _call(wrapper, "wrapped_animals", {})

        assert isinstance(result, ToolReturn)
        assert isinstance(result.return_value, DisclosureEnvelope)
        assert result.metadata == {"source": "test"}


class TestBindings:
    async def test_an_explicit_binding_makes_a_foreign_vocabulary_disclosable(self):
        capability = ResultDisclosure[None](bindings={Recipe: Binding(summary="blurb", title="label")})
        wrapper = _wrapper(capability)

        result = await _call(wrapper, "recipes", {})

        assert isinstance(result, DisclosureEnvelope)
        assert result.children[0].title == "pie"
        assert result.children[0].summary == "a savoury pie"
        assert result.children[0].kind == "Recipe"

    async def test_a_binding_on_a_base_class_covers_subclasses(self):
        capability = ResultDisclosure[None](bindings={Recipe: Binding(summary="blurb", title="label")})
        wrapper = _wrapper(capability)

        result = await _call(wrapper, "spicy", {})

        assert isinstance(result, DisclosureEnvelope)
        assert result.children[0].kind == "SpicyRecipe"
        assert result.children[0].title == "hot pie"

    async def test_an_explicit_binding_beats_the_structural_convention(self):
        capability = ResultDisclosure[None](bindings={Animal: Binding(summary="description", title="name")})
        wrapper = _wrapper(capability)

        result = await _call(wrapper, "animals", {})

        assert isinstance(result, DisclosureEnvelope)
        assert result.children[0].title == "Rex"
        assert result.children[0].summary == "chases the mail"  # the bound field, not the structural property


class TestLoadResult:
    async def test_load_result_returns_the_waiting_full_record(self):
        wrapper = _wrapper(ResultDisclosure[None]())

        collapsed = await _call(wrapper, "animals", {})
        assert isinstance(collapsed, DisclosureEnvelope)
        loaded = await _call(wrapper, "load_result", {"ref": collapsed.children[0].ref})

        assert loaded == _animals()[0]

    async def test_an_unknown_ref_raises_model_retry(self):
        wrapper = _wrapper(ResultDisclosure[None]())

        with pytest.raises(ModelRetry, match="valid only within this run"):
            await _call(wrapper, "load_result", {"ref": "result:deadbeef"})

    async def test_loading_the_same_ref_twice_raises_model_retry(self):
        wrapper = _wrapper(ResultDisclosure[None]())

        collapsed = await _call(wrapper, "animals", {})
        assert isinstance(collapsed, DisclosureEnvelope)
        ref = collapsed.children[0].ref
        await _call(wrapper, "load_result", {"ref": ref})

        with pytest.raises(ModelRetry, match="already loaded"):
            await _call(wrapper, "load_result", {"ref": ref})

    async def test_refs_are_isolated_between_runs(self):
        capability = ResultDisclosure[None]()
        first_run = _wrapper(capability)
        collapsed = await _call(first_run, "animals", {})
        assert isinstance(collapsed, DisclosureEnvelope)
        ref = collapsed.children[0].ref

        second_run = capability.get_wrapper_toolset(_toolset())
        assert second_run is not None

        with pytest.raises(ModelRetry, match="valid only within this run"):
            await _call(second_run, "load_result", {"ref": ref})

    async def test_a_user_tool_named_load_result_is_a_hard_error(self):
        toolset: FunctionToolset[None] = FunctionToolset()

        async def load_result(ref: str) -> str:
            return ref

        toolset.add_function(load_result, takes_ctx=False, name="load_result", description="collides")
        wrapper = ResultDisclosure[None]().get_wrapper_toolset(toolset)
        assert wrapper is not None

        with pytest.raises(UserError, match="reserved"):
            await wrapper.get_tools(_ctx())


class TestInstructions:
    def test_the_protocol_text_names_the_loader(self):
        capability = ResultDisclosure[None]()

        compose = capability.get_instructions()
        text = compose(_ctx())

        assert "load_result" in text
        assert "ref" in text
