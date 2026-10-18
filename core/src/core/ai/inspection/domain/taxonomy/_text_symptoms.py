"""The symptoms of what the agent says.

In reading order: claiming a fact, planning, reporting what was done, and stopping or asking. All show in text, except the
one about thinking, which shows in the thinking part.
"""

from __future__ import annotations

from typing import Final

from ._models import Symptom
from ._root_causes import AGENT_REASONING, INSTRUCTIONS_SAY_NOTHING, INSTRUCTIONS_SAY_WRONG, MODEL_TIER, TOOL_RESULT_SHAPE, USER_PROMPT

UNSUPPORTED_CLAIM = Symptom(
    name="unsupported_claim",
    shows_in="text",
    what_you_see=(
        "A fact stated with no source in context: not in a tool result, not in the user's message, not in a skill. A tool, the "
        "context or the user could have supplied it (an invented page id, a path, a number)."
    ),
    caught_by="model",
    horizon="run",
    possible_root_causes=(AGENT_REASONING, INSTRUCTIONS_SAY_NOTHING, INSTRUCTIONS_SAY_WRONG, MODEL_TIER),
)
CLAIM_CONTRADICTS_EVIDENCE = Symptom(
    name="claim_contradicts_evidence",
    shows_in="text",
    what_you_see="A statement that a result already in context contradicts.",
    caught_by="model",
    horizon="run",
    possible_root_causes=(AGENT_REASONING, TOOL_RESULT_SHAPE, MODEL_TIER, INSTRUCTIONS_SAY_NOTHING, INSTRUCTIONS_SAY_WRONG),
)
MISQUOTED_SOURCE = Symptom(
    name="misquoted_source",
    shows_in="text",
    what_you_see="A quote or a citation that does not match the source text it points at.",
    caught_by="rule_then_model",
    horizon="moment",
    possible_root_causes=(AGENT_REASONING, INSTRUCTIONS_SAY_NOTHING, INSTRUCTIONS_SAY_WRONG),
)
ACTED_ON_UNCHECKED_ASSUMPTION = Symptom(
    name="acted_on_unchecked_assumption",
    shows_in="text",
    what_you_see="The agent went ahead on an assumption a cheap check in context would have refuted.",
    caught_by="model",
    horizon="run",
    possible_root_causes=(AGENT_REASONING, INSTRUCTIONS_SAY_NOTHING, INSTRUCTIONS_SAY_WRONG),
)

DID_NOT_DO_WHAT_IT_WAS_TOLD_TO = Symptom(
    name="did_not_do_what_it_was_told_to",
    shows_in="text",
    what_you_see="The agent was told to do something and did not: check before writing, read the schema first, cite the source.",
    caught_by="rule_then_model",
    horizon="run",
    possible_root_causes=(AGENT_REASONING, INSTRUCTIONS_SAY_NOTHING, INSTRUCTIONS_SAY_WRONG),
)
DID_WHAT_IT_WAS_TOLD_NOT_TO = Symptom(
    name="did_what_it_was_told_not_to",
    shows_in="text",
    what_you_see=(
        "The agent was told not to do something and did it: wrote where it was told not to write, touched a file outside its "
        "scope, spent past its budget."
    ),
    caught_by="model",
    horizon="run",
    possible_root_causes=(AGENT_REASONING, INSTRUCTIONS_SAY_NOTHING, INSTRUCTIONS_SAY_WRONG, USER_PROMPT),
)
IGNORED_USER_CORRECTION = Symptom(
    name="ignored_user_correction",
    shows_in="text",
    what_you_see="The user corrected something and later messages repeat the corrected behavior.",
    caught_by="model",
    horizon="conversation",
    possible_root_causes=(AGENT_REASONING, INSTRUCTIONS_SAY_NOTHING, INSTRUCTIONS_SAY_WRONG),
)

REPORTED_DONE_WHEN_NOT_DONE = Symptom(
    name="reported_done_when_not_done",
    shows_in="text",
    what_you_see="A completion claim while a required output is missing or a check in the log failed.",
    caught_by="rule_then_model",
    horizon="run",
    possible_root_causes=(AGENT_REASONING, INSTRUCTIONS_SAY_NOTHING, INSTRUCTIONS_SAY_WRONG),
)
FAILURE_LEFT_OUT_OF_REPORT = Symptom(
    name="failure_left_out_of_report",
    shows_in="text",
    what_you_see="A tool error, a skipped step or a partial result in the log that the report does not mention.",
    caught_by="rule_then_model",
    horizon="run",
    possible_root_causes=(AGENT_REASONING, INSTRUCTIONS_SAY_NOTHING, INSTRUCTIONS_SAY_WRONG),
)
ANSWERED_A_DIFFERENT_QUESTION = Symptom(
    name="answered_a_different_question",
    shows_in="text",
    what_you_see="The answer addresses something other than what was asked.",
    caught_by="model",
    horizon="run",
    possible_root_causes=(AGENT_REASONING, USER_PROMPT, MODEL_TIER),
)
OUTPUT_SHAPE_NOT_FOLLOWED = Symptom(
    name="output_shape_not_followed",
    shows_in="text",
    what_you_see=(
        "The output does not match the required shape: prose where JSON was asked, a page missing required fields, the wrong section order."
    ),
    caught_by="rule_then_model",
    horizon="moment",
    possible_root_causes=(INSTRUCTIONS_SAY_NOTHING, INSTRUCTIONS_SAY_WRONG, MODEL_TIER),
)
OUTPUT_FAR_LONGER_THAN_NEEDED = Symptom(
    name="output_far_longer_than_needed",
    shows_in="text",
    what_you_see="The message restates context, repeats itself, or runs far beyond the norm for that kind of turn.",
    caught_by="rule_then_model",
    horizon="moment",
    possible_root_causes=(INSTRUCTIONS_SAY_NOTHING, INSTRUCTIONS_SAY_WRONG, MODEL_TIER),
)
THINKING_FAR_BEYOND_NORM = Symptom(
    name="thinking_far_beyond_norm",
    shows_in="thinking",
    what_you_see="Reasoning tokens far beyond the norm for a turn of that difficulty.",
    caught_by="rule",
    horizon="moment",
    possible_root_causes=(MODEL_TIER, INSTRUCTIONS_SAY_NOTHING, INSTRUCTIONS_SAY_WRONG),
)

STOPPED_WITH_WORK_LEFT = Symptom(
    name="stopped_with_work_left",
    shows_in="text",
    what_you_see="The turn ended with work left that nothing blocked.",
    caught_by="model",
    horizon="run",
    possible_root_causes=(AGENT_REASONING, INSTRUCTIONS_SAY_NOTHING, INSTRUCTIONS_SAY_WRONG, MODEL_TIER),
)
ASKED_WHAT_IT_COULD_FIND = Symptom(
    name="asked_what_it_could_find",
    shows_in="text",
    what_you_see="A question to the user that a tool or the context already answered.",
    caught_by="model",
    horizon="conversation",
    possible_root_causes=(AGENT_REASONING, INSTRUCTIONS_SAY_NOTHING, INSTRUCTIONS_SAY_WRONG),
)
PROCEEDED_ON_AMBIGUOUS_REQUEST = Symptom(
    name="proceeded_on_ambiguous_request",
    shows_in="text",
    what_you_see="The request had two readings that lead to materially different work and the agent picked one without asking.",
    caught_by="model",
    horizon="run",
    possible_root_causes=(AGENT_REASONING, INSTRUCTIONS_SAY_NOTHING, INSTRUCTIONS_SAY_WRONG, USER_PROMPT),
)

TEXT_SYMPTOMS: Final = (
    UNSUPPORTED_CLAIM,
    CLAIM_CONTRADICTS_EVIDENCE,
    MISQUOTED_SOURCE,
    ACTED_ON_UNCHECKED_ASSUMPTION,
    DID_NOT_DO_WHAT_IT_WAS_TOLD_TO,
    DID_WHAT_IT_WAS_TOLD_NOT_TO,
    IGNORED_USER_CORRECTION,
    REPORTED_DONE_WHEN_NOT_DONE,
    FAILURE_LEFT_OUT_OF_REPORT,
    ANSWERED_A_DIFFERENT_QUESTION,
    OUTPUT_SHAPE_NOT_FOLLOWED,
    OUTPUT_FAR_LONGER_THAN_NEEDED,
    THINKING_FAR_BEYOND_NORM,
    STOPPED_WITH_WORK_LEFT,
    ASKED_WHAT_IT_COULD_FIND,
    PROCEEDED_ON_AMBIGUOUS_REQUEST,
)
