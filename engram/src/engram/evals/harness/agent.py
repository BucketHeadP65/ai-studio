"""Build the agent under test and wrap it as the task a pydantic-evals dataset runs.

The agent returns a structured `RetrievalAnswer` over the same retrieval toolset the MCP server exposes.
It takes no run dependencies, because the retrieval handlers are closed over inside the toolset.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic_ai import Agent
from pydantic_ai.capabilities import Instrumentation

from engram.evals.shared.contracts import RetrievalAnswer

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Sequence

    from pydantic_ai.capabilities import AbstractCapability
    from pydantic_ai.models import KnownModelName, Model
    from pydantic_ai.toolsets import AbstractToolset
    from pydantic_ai.usage import UsageLimits

    from engram.evals.shared.contracts import QaInput

type AgentUnderTest = Agent[None, RetrievalAnswer]
"""The agent under test: no run dependencies, and a structured `RetrievalAnswer` as its output."""


def build_agent(
    model: Model | KnownModelName | str,
    *,
    instructions: str,
    toolset: AbstractToolset[object],
    code_mode: bool = False,
    extra_capabilities: Sequence[AbstractCapability] | None = None,
) -> AgentUnderTest:
    """Build the agent under test from `model`, `instructions` and a retrieval `toolset`.

    The agent is instrumented, so every tool call, token count and latency lands in the span tree
    the evaluators score. With `code_mode` set, the tools collapse into one `run_code` tool the model
    drives by writing sandboxed Python, and the run's tool-call count holds each `run_code` call once,
    never the calls its program makes. `extra_capabilities` are added after the built-in ones.

    Args:
        model: The model to evaluate, a concrete `Model` such as `TestModel`, or a model name
            pydantic-ai resolves.
        instructions: The agent's system instructions.
        toolset: The retrieval toolset from `build_retrieval_toolset`, its handlers already closed
            over, or a wrapper around it such as the guarded tracing toolset.
        code_mode: Whether to add the `ModelCallsCodeMode` capability.
        extra_capabilities: More capabilities to add, such as `ProgressiveDisclosure`. `None` adds none.

    Returns:
        The configured agent under test.
    """
    capabilities: list[AbstractCapability] = [Instrumentation()]
    if code_mode:
        from engram.evals.harness.code_mode import ModelCallsCodeMode  # noqa: PLC0415 (lazy: it loads the Monty sandbox)

        capabilities.append(ModelCallsCodeMode())
    if extra_capabilities:
        capabilities.extend(extra_capabilities)
    return Agent(
        model, instructions=instructions, toolsets=[toolset], output_type=RetrievalAnswer, capabilities=capabilities, deps_type=type(None)
    )


def make_task(agent: AgentUnderTest, *, usage_limits: UsageLimits | None = None) -> Callable[[QaInput], Awaitable[RetrievalAnswer]]:
    """Wrap a built `agent` as the async task a pydantic-evals dataset runs once per case.

    The task runs the agent on the case's `question` alone and returns its structured `RetrievalAnswer`.
    A run that crosses `usage_limits` raises `UsageLimitExceeded`, which fails the case.

    Args:
        agent: The agent under test from `build_agent`.
        usage_limits: The limits every run of the agent is held to, or `None` for pydantic-ai's defaults.

    Returns:
        An async function from a `QaInput` to the agent's `RetrievalAnswer`.
    """

    async def run_case(inp: QaInput) -> RetrievalAnswer:
        res = await agent.run(inp.question, usage_limits=usage_limits)
        return res.output

    return run_case
