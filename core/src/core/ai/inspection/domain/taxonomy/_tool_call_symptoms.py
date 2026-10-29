"""The symptoms of tool use.

In reading order: choosing whether and which tool, calling it, reacting to what came back, doing all of that economically,
and the tool's own faults. Most show in the tool call or in its return, a retry included: a call rejected before any tool
ran and a call a tool refused both answer the call with a retried return. One shows in text (giving up on an error the
agent could have recovered from). The health of a tool or its host is not here: that is a number in a span, not a
relationship between what the agent was told and what it did.
"""

from __future__ import annotations

from typing import Final

from ._models import Symptom
from ._root_causes import (
    AGENT_REASONING,
    INSTRUCTIONS_SAY_NOTHING,
    INSTRUCTIONS_SAY_WRONG,
    MISSING_TOOL,
    MODEL_TIER,
    OVERLAPPING_TOOLS,
    TOOL_DESCRIPTION,
    TOOL_DESIGN,
    TOOL_ERROR_MESSAGE,
    TOOL_RESULT_SHAPE,
    TOOL_SCHEMA,
)

WRONG_TOOL_CHOSEN = Symptom(
    name="wrong_tool_chosen",
    shows_in="tool-call",
    what_you_see="The right tool existed and the agent used another one (grep where search exists, run_code for one plain call).",
    caught_by="model",
    horizon="moment",
    possible_root_causes=(TOOL_DESCRIPTION, OVERLAPPING_TOOLS, INSTRUCTIONS_SAY_NOTHING, INSTRUCTIONS_SAY_WRONG, AGENT_REASONING),
)
NO_TOOL_FOR_THE_JOB = Symptom(
    name="no_tool_for_the_job",
    shows_in="tool-call",
    what_you_see="No tool fit the job and the agent improvised (hand-wrote a page's markdown, shelled out, guessed a fact).",
    caught_by="model",
    horizon="moment",
    possible_root_causes=(MISSING_TOOL,),
)
DUPLICATE_CALL = Symptom(
    name="duplicate_call",
    shows_in="tool-call",
    what_you_see=(
        "The same tool with the same arguments twice, the first call succeeded, no write call and no context compaction in "
        "between. Waste on a tool with no world effect, damage on one that changes the world."
    ),
    caught_by="rule",
    horizon="run",
    possible_root_causes=(AGENT_REASONING, INSTRUCTIONS_SAY_NOTHING, INSTRUCTIONS_SAY_WRONG, TOOL_DESIGN),
)
CALL_FOR_KNOWN_FACT = Symptom(
    name="call_for_known_fact",
    shows_in="tool-call",
    what_you_see=(
        "The fact was already in context (in the user's message, a skill, an earlier result under other arguments) and the "
        "agent called a tool for it anyway."
    ),
    caught_by="model",
    horizon="conversation",
    possible_root_causes=(AGENT_REASONING, INSTRUCTIONS_SAY_NOTHING, INSTRUCTIONS_SAY_WRONG),
)
DENIED_ACTION_REPEATED = Symptom(
    name="denied_action_repeated",
    shows_in="tool-call",
    what_you_see="A permission or policy refusal, then the same ask.",
    caught_by="rule",
    horizon="run",
    possible_root_causes=(AGENT_REASONING, INSTRUCTIONS_SAY_NOTHING, INSTRUCTIONS_SAY_WRONG),
)

SCHEMA_REJECTED_ARGUMENTS = Symptom(
    name="schema_rejected_arguments",
    shows_in="tool-return",
    what_you_see="No tool ran: the call named no tool on offer, or its arguments failed the tool's schema.",
    caught_by="rule",
    horizon="moment",
    possible_root_causes=(TOOL_SCHEMA, TOOL_DESCRIPTION, MODEL_TIER),
)
TOOL_REFUSED_CALL = Symptom(
    name="tool_refused_call",
    shows_in="tool-return",
    what_you_see=(
        "The call passed the schema, then the tool, a guard around it or its own check asked the model to call again with "
        "something changed (an id that does not resolve, a path a guard denies, a program that raised)."
    ),
    caught_by="rule",
    horizon="moment",
    possible_root_causes=(TOOL_SCHEMA, TOOL_DESCRIPTION, INSTRUCTIONS_SAY_NOTHING, INSTRUCTIONS_SAY_WRONG, AGENT_REASONING, MODEL_TIER),
)
VALID_BUT_WRONG_ARGUMENTS = Symptom(
    name="valid_but_wrong_arguments",
    shows_in="tool-call",
    what_you_see="A valid call with the wrong meaning: wrong knowledge base name, wrong id shape, wrong path.",
    caught_by="rule_then_model",
    horizon="moment",
    possible_root_causes=(TOOL_DESCRIPTION, INSTRUCTIONS_SAY_NOTHING, INSTRUCTIONS_SAY_WRONG, AGENT_REASONING),
)
CALLED_BEFORE_PRECONDITION = Symptom(
    name="called_before_precondition",
    shows_in="tool-call",
    what_you_see="The right tool with a missing precondition: write before check, fetch before search, decide before read.",
    caught_by="rule_then_model",
    horizon="moment",
    possible_root_causes=(TOOL_DESCRIPTION, TOOL_DESIGN, INSTRUCTIONS_SAY_NOTHING, INSTRUCTIONS_SAY_WRONG),
)
STALE_REFERENCE_USED = Symptom(
    name="stale_reference_used",
    shows_in="tool-call",
    what_you_see="An id or path from an earlier result that no longer resolves, used as if it did.",
    caught_by="rule",
    horizon="conversation",
    possible_root_causes=(AGENT_REASONING, TOOL_RESULT_SHAPE, INSTRUCTIONS_SAY_NOTHING, INSTRUCTIONS_SAY_WRONG),
)
SCOPE_TOO_BIG_OR_TOO_SMALL = Symptom(
    name="scope_too_big_or_too_small",
    shows_in="tool-call",
    what_you_see="Asked for everything where one item was needed, or one where everything was needed.",
    caught_by="rule_then_model",
    horizon="moment",
    possible_root_causes=(TOOL_DESCRIPTION, INSTRUCTIONS_SAY_NOTHING, INSTRUCTIONS_SAY_WRONG, AGENT_REASONING),
)

RETRIED_AFTER_CLEAR_ERROR = Symptom(
    name="retried_after_clear_error",
    shows_in="tool-call",
    what_you_see="Same tool, same arguments, right after an error whose text named the fix.",
    caught_by="rule",
    horizon="run",
    possible_root_causes=(AGENT_REASONING, INSTRUCTIONS_SAY_NOTHING, INSTRUCTIONS_SAY_WRONG),
)
RETRIED_AFTER_VAGUE_ERROR = Symptom(
    name="retried_after_vague_error",
    shows_in="tool-return",
    what_you_see="Same tool, same arguments, after an error whose text did not say what to change.",
    caught_by="rule_then_model",
    horizon="moment",
    possible_root_causes=(TOOL_ERROR_MESSAGE,),
)
GAVE_UP_ON_RECOVERABLE_ERROR = Symptom(
    name="gave_up_on_recoverable_error",
    shows_in="text",
    what_you_see=(
        "An error that a retry, a different argument, or another tool would have fixed, and the agent stopped or reported failure."
    ),
    caught_by="model",
    horizon="run",
    possible_root_causes=(AGENT_REASONING, TOOL_ERROR_MESSAGE, INSTRUCTIONS_SAY_NOTHING, INSTRUCTIONS_SAY_WRONG),
)
WARNING_IGNORED = Symptom(
    name="warning_ignored",
    shows_in="tool-return",
    what_you_see="A success answer carried a warning (an unresolvable citation, a truncation note) and the next step did not act on it.",
    caught_by="rule_then_model",
    horizon="moment",
    possible_root_causes=(AGENT_REASONING, TOOL_RESULT_SHAPE, INSTRUCTIONS_SAY_NOTHING, INSTRUCTIONS_SAY_WRONG),
)
UNCERTAIN_RESULT_TAKEN_AS_FACT = Symptom(
    name="uncertain_result_taken_as_fact",
    shows_in="tool-return",
    what_you_see="The result marked itself uncertain (a low score, a partial match) and the agent acted on it as fact.",
    caught_by="model",
    horizon="moment",
    possible_root_causes=(AGENT_REASONING, INSTRUCTIONS_SAY_NOTHING, INSTRUCTIONS_SAY_WRONG),
)
RESULT_MISREAD = Symptom(
    name="result_misread",
    shows_in="tool-return",
    what_you_see="The agent took one field for another, or parsed prose it should have read as data.",
    caught_by="model",
    horizon="moment",
    possible_root_causes=(TOOL_RESULT_SHAPE, AGENT_REASONING, INSTRUCTIONS_SAY_NOTHING, INSTRUCTIONS_SAY_WRONG),
)

UNBATCHED_CALLS = Symptom(
    name="unbatched_calls",
    shows_in="tool-call",
    what_you_see="One call per phrasing where one batched call would do, a whole file where a section would do.",
    caught_by="rule_then_model",
    horizon="run",
    possible_root_causes=(INSTRUCTIONS_SAY_NOTHING, INSTRUCTIONS_SAY_WRONG, TOOL_DESCRIPTION, TOOL_DESIGN),
)
SERIAL_CALLS_INSTEAD_OF_PARALLEL = Symptom(
    name="serial_calls_instead_of_parallel",
    shows_in="tool-call",
    what_you_see=(
        "Several things that did not depend on each other were asked for one per turn, waiting for each answer, where one turn "
        "could have asked for all of them. Every extra turn is one more model request with the whole context sent again."
    ),
    caught_by="rule",
    horizon="run",
    possible_root_causes=(INSTRUCTIONS_SAY_NOTHING, INSTRUCTIONS_SAY_WRONG, TOOL_DESCRIPTION),
)
EXPENSIVE_TOOL_FOR_CHEAP_ANSWER = Symptom(
    name="expensive_tool_for_cheap_answer",
    shows_in="tool-call",
    what_you_see="A model-backed or quota-bound tool used where a plain read answers the same.",
    caught_by="model",
    horizon="moment",
    possible_root_causes=(INSTRUCTIONS_SAY_NOTHING, INSTRUCTIONS_SAY_WRONG, TOOL_DESCRIPTION),
)
RESULT_OVERFLOWED_CONTEXT = Symptom(
    name="result_overflowed_context",
    shows_in="tool-return",
    what_you_see="A result so large that later turns drifted or the conversation compacted.",
    caught_by="rule",
    horizon="run",
    possible_root_causes=(TOOL_RESULT_SHAPE, AGENT_REASONING, INSTRUCTIONS_SAY_NOTHING, INSTRUCTIONS_SAY_WRONG),
)
BUDGET_EXHAUSTED_UNFINISHED = Symptom(
    name="budget_exhausted_unfinished",
    shows_in="tool-call",
    what_you_see=(
        "The request or tool-call budget was spent without finishing. Often the effect of another symptom in the same transcript."
    ),
    caught_by="rule",
    horizon="run",
    possible_root_causes=(AGENT_REASONING, INSTRUCTIONS_SAY_NOTHING, INSTRUCTIONS_SAY_WRONG, MODEL_TIER),
)

DOCSTRING_MISMATCH = Symptom(
    name="docstring_mismatch",
    shows_in="tool-return",
    what_you_see="The docstring says one thing and the tool does another, or the docstring is stale after a change.",
    caught_by="model",
    horizon="moment",
    possible_root_causes=(TOOL_DESCRIPTION,),
)

TOOL_CALL_SYMPTOMS: Final = (
    WRONG_TOOL_CHOSEN,
    NO_TOOL_FOR_THE_JOB,
    DUPLICATE_CALL,
    CALL_FOR_KNOWN_FACT,
    DENIED_ACTION_REPEATED,
    SCHEMA_REJECTED_ARGUMENTS,
    TOOL_REFUSED_CALL,
    VALID_BUT_WRONG_ARGUMENTS,
    CALLED_BEFORE_PRECONDITION,
    STALE_REFERENCE_USED,
    SCOPE_TOO_BIG_OR_TOO_SMALL,
    RETRIED_AFTER_CLEAR_ERROR,
    RETRIED_AFTER_VAGUE_ERROR,
    GAVE_UP_ON_RECOVERABLE_ERROR,
    WARNING_IGNORED,
    UNCERTAIN_RESULT_TAKEN_AS_FACT,
    RESULT_MISREAD,
    UNBATCHED_CALLS,
    SERIAL_CALLS_INSTEAD_OF_PARALLEL,
    EXPENSIVE_TOOL_FOR_CHEAP_ANSWER,
    RESULT_OVERFLOWED_CONTEXT,
    BUDGET_EXHAUSTED_UNFINISHED,
    DOCSTRING_MISMATCH,
)
