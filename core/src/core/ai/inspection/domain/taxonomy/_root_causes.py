"""The closed list of root causes. A blame names one of these and nothing else."""

from __future__ import annotations

from typing import Final

from ._models import RootCause, SurfaceKind

AGENT_REASONING = RootCause(
    name="agent_reasoning",
    meaning=(
        "The information was there, the instructions covered it, and the agent still did the wrong thing with it. A verdict, "
        "reached once every other root cause is cleared."
    ),
    fix_lands_on=(SurfaceKind.MODEL_TIER,),
)
TOOL_ERROR_MESSAGE = RootCause(
    name="tool_error_message",
    meaning="The tool refused or failed and its text did not say what to change.",
    fix_lands_on=(SurfaceKind.TOOL,),
)
TOOL_DESCRIPTION = RootCause(
    name="tool_description",
    meaning="The docstring the model reads did not say when to use the tool, what an argument looks like, or what must come before.",
    fix_lands_on=(SurfaceKind.TOOL,),
)
TOOL_SCHEMA = RootCause(
    name="tool_schema",
    meaning=(
        "The signature the model sees demanded a shape stricter or harder than the tool needs: a literal narrower than the "
        "tool accepts, a required field the tool defaults, nesting or formats models get wrong, a name that promises another type."
    ),
    fix_lands_on=(SurfaceKind.TOOL,),
)
TOOL_RESULT_SHAPE = RootCause(
    name="tool_result_shape",
    meaning="The answer came back in a form the model could not use well: a blob, no ids, no next step, too big.",
    fix_lands_on=(SurfaceKind.TOOL,),
)
TOOL_DESIGN = RootCause(
    name="tool_design",
    meaning="The tool is shaped so that misuse is easy: not idempotent, no batching, does not check its own precondition.",
    fix_lands_on=(SurfaceKind.TOOL,),
)
MISSING_TOOL = RootCause(name="missing_tool", meaning="No tool exists for the job.", fix_lands_on=(SurfaceKind.TOOL,))
OVERLAPPING_TOOLS = RootCause(
    name="overlapping_tools",
    meaning="Two tools claim the same job so that the choice between them is arbitrary.",
    fix_lands_on=(SurfaceKind.TOOL,),
)
INSTRUCTIONS_SAY_NOTHING = RootCause(
    name="instructions_say_nothing",
    meaning="The skill or the system prompt the agent was given does not cover this situation.",
    fix_lands_on=(SurfaceKind.SKILL, SurfaceKind.SYSTEM_PROMPT),
)
INSTRUCTIONS_SAY_WRONG = RootCause(
    name="instructions_say_wrong",
    meaning="The skill or the system prompt covers this situation and tells the agent the wrong thing.",
    fix_lands_on=(SurfaceKind.SKILL, SurfaceKind.SYSTEM_PROMPT),
)
MODEL_TIER = RootCause(
    name="model_tier", meaning="The model on that tier cannot do this reliably however it is told.", fix_lands_on=(SurfaceKind.MODEL_TIER,)
)
USER_PROMPT = RootCause(
    name="user_prompt",
    meaning="The request was ambiguous, contradictory, or missing a fact only the user had.",
    fix_lands_on=(SurfaceKind.SKILL, SurfaceKind.SYSTEM_PROMPT),
)

ROOT_CAUSES: Final = (
    AGENT_REASONING,
    TOOL_ERROR_MESSAGE,
    TOOL_DESCRIPTION,
    TOOL_SCHEMA,
    TOOL_RESULT_SHAPE,
    TOOL_DESIGN,
    MISSING_TOOL,
    OVERLAPPING_TOOLS,
    INSTRUCTIONS_SAY_NOTHING,
    INSTRUCTIONS_SAY_WRONG,
    MODEL_TIER,
    USER_PROMPT,
)
