"""Unit tests for the generic progressive-disclosure capability (`core.ai.disclosure.capability`).

`ProgressiveDisclosure` is a thin protocol handler: it contributes the descent-protocol
instructions and wraps the agent's assembled toolset in a per-run token budget over conformant
results — a bare `DisclosureEnvelope`, or a pydantic-ai `ToolReturn` wrapping one.
These specs drive it against a bare `FunctionToolset` exposing a fake `disclose` (returns a
scripted envelope, bare or `ToolReturn`-wrapped) and a fake `search` (returns a plain string,
never an envelope), mirroring `test_retrieval_budget.py`'s direct `call_tool`/`get_tools` style
rather than a full agent run.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from pydantic_ai import FunctionToolset, ToolReturn
from pydantic_ai.models.test import TestModel
from pydantic_ai.tools import RunContext
from pydantic_ai.toolsets import WrapperToolset
from pydantic_ai.usage import RunUsage

from core.ai.disclosure.capability import ProgressiveDisclosure
from core.ai.disclosure.contracts import DisclosureEnvelope, approx_tokens
from core.ai.retrieval.contracts import NextSteps

if TYPE_CHECKING:
    from pydantic_ai.toolsets import AbstractToolset


def _ctx() -> RunContext[None]:
    return RunContext(deps=None, model=TestModel(), usage=RunUsage())


def _envelope(ref: str) -> DisclosureEnvelope:
    return DisclosureEnvelope(
        ref=ref,
        kind="unit",
        title=f"Title of {ref}",
        summary=f"A summary of {ref}.",
        content="Body content. " * 20,
    )


def _wrapped() -> FunctionToolset[None]:
    """A bare `FunctionToolset` with a `disclose` (envelope) and a `search` (plain string) tool."""
    toolset: FunctionToolset[None] = FunctionToolset()

    async def disclose(ref: str = "root") -> DisclosureEnvelope:
        return _envelope(ref)

    async def search(query: str) -> str:
        return f"hits:{query}"

    toolset.add_function(disclose, takes_ctx=False, name="disclose", description="disclose a unit")
    toolset.add_function(search, takes_ctx=False, name="search", description="search the knowledge base")
    return toolset


def _wrapped_tool_return() -> FunctionToolset[None]:
    """A `FunctionToolset` whose `disclose` wraps its envelope in a `ToolReturn`."""
    toolset: FunctionToolset[None] = FunctionToolset()

    async def disclose(ref: str = "root") -> ToolReturn:
        return ToolReturn(return_value=_envelope(ref), metadata={"source": "test"})

    toolset.add_function(disclose, takes_ctx=False, name="disclose", description="disclose a unit")
    return toolset


async def _call(toolset: AbstractToolset[None], name: str, args: dict[str, object]) -> object:
    ctx = _ctx()
    tools = await toolset.get_tools(ctx)
    return await toolset.call_tool(name, args, ctx, tools[name])


@dataclass
class _CopiedPerStep(WrapperToolset[None]):
    """A toolset that returns a fresh copy of itself at every run step, as a stateful toolset does."""

    async def for_run_step(self, ctx: RunContext[None]) -> AbstractToolset[None]:
        return replace(self)


class _EnvelopeWithNextSteps(DisclosureEnvelope):
    """A `DisclosureEnvelope` subclass adding one field — mirrors the retrieval layer's real pattern.

    Mirrors `engram.retrieval.tools.FetchEnvelope` / `FetchUnit`: a genuine subclass, never a
    dict-merge or a separate wrapper — the whole point being that `isinstance(result,
    DisclosureEnvelope)` (the exact check `_EnvelopeBudget.call_tool` performs below) still holds
    through inheritance. Defined here, in `core`, rather than importing engram's real class: `core`
    has no dependency on `engram`.
    """

    next_steps: NextSteps


def _envelope_with_next_steps(ref: str, *, options: tuple[str, ...] = ()) -> _EnvelopeWithNextSteps:
    return _EnvelopeWithNextSteps(
        ref=ref,
        kind="unit",
        title=f"Title of {ref}",
        summary=f"A summary of {ref}.",
        content="Body content. " * 20,
        next_steps=NextSteps(summary="Do the next thing.", options=options),
    )


def _wrapped_next_steps(envelope: _EnvelopeWithNextSteps) -> FunctionToolset[None]:
    """A bare `FunctionToolset` whose `disclose` always returns the given subclassed envelope."""
    toolset: FunctionToolset[None] = FunctionToolset()

    async def disclose(ref: str = "root") -> _EnvelopeWithNextSteps:
        return envelope

    toolset.add_function(disclose, takes_ctx=False, name="disclose", description="disclose a unit")
    return toolset


class TestInstructions:
    def test_get_instructions_orients_on_leveled_reads_without_announcing_the_budget(self):
        capability = ProgressiveDisclosure(max_envelope_tokens=500)

        compose = capability.get_instructions()
        text = compose(_ctx())

        assert "ref" in text
        assert "children" in text.lower()
        # The budget speaks through the stub on the read that spends it, never in advance.
        assert "500" not in text
        assert "budget" not in text.lower()



class TestBudget:
    async def test_under_budget_results_pass_through_unchanged(self):
        capability = ProgressiveDisclosure[None](max_envelope_tokens=20_000)
        wrapper = capability.get_wrapper_toolset(_wrapped())
        assert wrapper is not None

        result = await _call(wrapper, "disclose", {"ref": "root"})

        assert result == _envelope("root")

    async def test_an_envelope_that_exactly_fits_the_budget_passes_through(self):
        tokens = approx_tokens(_envelope("root"))
        capability = ProgressiveDisclosure[None](max_envelope_tokens=tokens)
        wrapper = capability.get_wrapper_toolset(_wrapped())
        assert wrapper is not None

        result = await _call(wrapper, "disclose", {"ref": "root"})

        assert result == _envelope("root")

    async def test_a_single_envelope_larger_than_the_whole_budget_is_stubbed_immediately(self):
        tokens = approx_tokens(_envelope("root"))
        capability = ProgressiveDisclosure[None](max_envelope_tokens=tokens - 1)
        wrapper = capability.get_wrapper_toolset(_wrapped())
        assert wrapper is not None

        result = await _call(wrapper, "disclose", {"ref": "root"})  # a single call, no prior spend at all

        assert isinstance(result, DisclosureEnvelope)
        assert result.kind == "budget"
        assert result.ref == "root"  # the requested ref, recovered from the stubbed call's own args
        assert result.mode == "search_only"
        assert result.title == "Reading budget reached"
        assert result.summary == f"Reading budget of {tokens - 1} tokens spent. Narrow with search, then read only what you need."

    async def test_the_call_that_crosses_the_cumulative_budget_is_the_one_stubbed(self):
        root_tokens = approx_tokens(_envelope("root"))
        child_tokens = approx_tokens(_envelope("child"))
        capability = ProgressiveDisclosure[None](max_envelope_tokens=root_tokens + child_tokens - 1)
        wrapper = capability.get_wrapper_toolset(_wrapped())
        assert wrapper is not None

        first = await _call(wrapper, "disclose", {"ref": "root"})
        second = await _call(wrapper, "disclose", {"ref": "child"})

        assert first == _envelope("root")  # fits within the budget on its own
        assert isinstance(second, DisclosureEnvelope)
        assert second.kind == "budget"  # this call — the one that crosses the cumulative cap — is stubbed, not the next

    async def test_non_envelope_results_are_never_touched_or_counted(self):
        capability = ProgressiveDisclosure[None](max_envelope_tokens=1)
        wrapper = capability.get_wrapper_toolset(_wrapped())
        assert wrapper is not None

        await _call(wrapper, "disclose", {"ref": "root"})  # spends well past the tiny budget
        result = await _call(wrapper, "search", {"query": "graph rag"})  # untouched despite the exhausted budget

        assert result == "hits:graph rag"

    async def test_a_stub_recovers_the_tool_name_when_no_ref_arg_is_recoverable(self):
        capability = ProgressiveDisclosure[None](max_envelope_tokens=1)
        wrapper = capability.get_wrapper_toolset(_wrapped())
        assert wrapper is not None

        stub = await _call(wrapper, "disclose", {})  # no recoverable ref

        assert isinstance(stub, DisclosureEnvelope)
        assert stub.ref == "disclose"

    async def test_per_run_isolation_a_fresh_wrapper_starts_under_budget_again(self):
        tokens = approx_tokens(_envelope("root"))
        capability = ProgressiveDisclosure[None](max_envelope_tokens=tokens)
        wrapped = _wrapped()

        first_run = capability.get_wrapper_toolset(wrapped)
        assert first_run is not None
        await _call(first_run, "disclose", {"ref": "root"})  # fits exactly
        exhausted = await _call(first_run, "disclose", {"ref": "root"})  # crosses the cumulative cap
        assert isinstance(exhausted, DisclosureEnvelope)
        assert exhausted.kind == "budget"

        second_run = capability.get_wrapper_toolset(wrapped)  # a new run, same capability instance
        assert second_run is not None
        fresh = await _call(second_run, "disclose", {"ref": "root"})

        assert fresh == _envelope("root")  # the second run's budget did not inherit the first run's spend

    async def test_a_run_step_copy_of_the_wrapper_spends_the_same_budget(self):
        tokens = approx_tokens(_envelope("root"))
        capability = ProgressiveDisclosure[None](max_envelope_tokens=tokens)
        wrapper = capability.get_wrapper_toolset(_CopiedPerStep(wrapped=_wrapped()))
        assert wrapper is not None

        step_copy = await wrapper.for_run_step(_ctx())
        assert step_copy is not wrapper  # pydantic-ai copied the wrapper for the new step
        await _call(step_copy, "disclose", {"ref": "root"})  # fits exactly, spent through the copy
        crossed = await _call(wrapper, "disclose", {"ref": "root"})

        assert isinstance(crossed, DisclosureEnvelope)
        assert crossed.kind == "budget"  # the original saw what its copy spent


class TestSubclassedEnvelopeConformance:
    """A `DisclosureEnvelope` SUBCLASS carrying an extra field is still a conformant result.

    This is the isinstance contract the retrieval layer's `next_steps`-on-`fetch` design leans on:
    subclassing (not dict-merging) so the budget wrapper's own `isinstance(result, DisclosureEnvelope)`
    check — never a name or a tool allowlist — keeps recognizing it, `approx_tokens` keeps applying
    (now over the bigger payload), and the accounting keeps working exactly as it does for the bare
    shape above.
    """

    async def test_a_subclassed_envelope_is_recognized_as_conformant(self):
        capability = ProgressiveDisclosure[None](max_envelope_tokens=20_000)
        wrapper = capability.get_wrapper_toolset(_wrapped_next_steps(_envelope_with_next_steps("root")))
        assert wrapper is not None

        result = await _call(wrapper, "disclose", {"ref": "root"})

        assert isinstance(result, DisclosureEnvelope)  # the exact check `_EnvelopeBudget.call_tool` performs
        assert isinstance(result, _EnvelopeWithNextSteps)
        assert result == _envelope_with_next_steps("root")

    def test_approx_tokens_counts_the_next_steps_payload_too(self):
        bare = _envelope("root")
        with_next_steps = _envelope_with_next_steps("root")
        with_bigger_next_steps = _envelope_with_next_steps("root", options=("x" * 500,))

        # Extending the base shape costs at least the extra field's own (small) text.
        assert approx_tokens(with_next_steps) > approx_tokens(bare)
        # A bigger `next_steps` payload on the SAME base fields costs more still — `approx_tokens`
        # (`len(model_dump_json()) // 4`) is genuinely reading the subclass's full serialized form,
        # not just the base `DisclosureEnvelope` fields.
        assert approx_tokens(with_bigger_next_steps) > approx_tokens(with_next_steps)

    async def test_the_budget_stubs_a_subclassed_envelope_once_it_crosses_the_cap(self):
        tokens = approx_tokens(_envelope_with_next_steps("root"))
        capability = ProgressiveDisclosure[None](max_envelope_tokens=tokens - 1)
        wrapper = capability.get_wrapper_toolset(_wrapped_next_steps(_envelope_with_next_steps("root")))
        assert wrapper is not None

        result = await _call(wrapper, "disclose", {"ref": "root"})

        assert isinstance(result, DisclosureEnvelope)
        assert result.kind == "budget"  # the stub, not the scripted subclass instance
        assert result.ref == "root"

    async def test_the_budget_sees_a_bigger_next_steps_payload_as_a_bigger_spend(self):
        # Prove the accounting itself moves with the subclass's real size, not just the base fields:
        # a cap strictly between the small and the big variant's token cost lets the small one
        # through and stubs the big one, so the number the budget compares against genuinely reflects
        # `next_steps`, not a token count computed only over the inherited `DisclosureEnvelope` shape.
        small = _envelope_with_next_steps("root", options=())
        big = _envelope_with_next_steps("root", options=("z" * 5_000,))
        cap = (approx_tokens(small) + approx_tokens(big)) // 2
        assert approx_tokens(small) < cap < approx_tokens(big)

        small_wrapper = ProgressiveDisclosure[None](max_envelope_tokens=cap).get_wrapper_toolset(_wrapped_next_steps(small))
        big_wrapper = ProgressiveDisclosure[None](max_envelope_tokens=cap).get_wrapper_toolset(_wrapped_next_steps(big))
        assert small_wrapper is not None
        assert big_wrapper is not None

        small_result = await _call(small_wrapper, "disclose", {"ref": "root"})
        big_result = await _call(big_wrapper, "disclose", {"ref": "root"})

        assert small_result == small  # under the cap: passes through unchanged, next_steps included
        assert isinstance(big_result, DisclosureEnvelope)
        assert big_result.kind == "budget"  # over the cap: stubbed, exactly like an oversized bare envelope


class TestToolReturnWrapping:
    async def test_a_tool_return_wrapped_envelope_is_counted_and_passes_through_under_budget(self):
        tokens = approx_tokens(_envelope("root"))
        capability = ProgressiveDisclosure[None](max_envelope_tokens=tokens)
        wrapper = capability.get_wrapper_toolset(_wrapped_tool_return())
        assert wrapper is not None

        result = await _call(wrapper, "disclose", {"ref": "root"})

        assert isinstance(result, ToolReturn)
        assert result.return_value == _envelope("root")
        assert result.metadata == {"source": "test"}  # untouched: under budget, nothing was substituted

    async def test_a_tool_return_wrapped_envelope_is_stubbed_still_wrapped_when_over_budget(self):
        tokens = approx_tokens(_envelope("root"))
        capability = ProgressiveDisclosure[None](max_envelope_tokens=tokens - 1)
        wrapper = capability.get_wrapper_toolset(_wrapped_tool_return())
        assert wrapper is not None

        result = await _call(wrapper, "disclose", {"ref": "root"})

        assert isinstance(result, ToolReturn)  # still ToolReturn-wrapped, not unwrapped to a bare envelope
        assert isinstance(result.return_value, DisclosureEnvelope)
        assert result.return_value.kind == "budget"
        assert result.return_value.ref == "root"
