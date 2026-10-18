"""The taxonomy: closed lists, cross-checked, resolved by name."""

import pytest

from pydantic import ValidationError

from core.ai.inspection.domain.taxonomy import (
    ROOT_CAUSES,
    TAXONOMY,
    Blame,
    Finding,
    Horizon,
    RootCause,
    Surface,
    SurfaceKind,
    Symptom,
    Taxonomy,
)
from core.ai.transcripts.contracts import Span

A_SPAN = Span(step=0, message=1, part=0)


def test_the_lists_are_the_documented_size() -> None:
    assert len(TAXONOMY.symptoms) == 38
    assert len(TAXONOMY.root_causes) == 12


def test_every_root_cause_is_behind_some_symptom() -> None:
    used = {cause.name for symptom in TAXONOMY.symptoms for cause in symptom.possible_root_causes}

    assert used == {cause.name for cause in ROOT_CAUSES}


def test_a_symptom_is_found_by_name_and_an_unknown_name_is_refused() -> None:
    assert TAXONOMY.symptom("duplicate_call").shows_in == "tool-call"
    with pytest.raises(KeyError):
        TAXONOMY.symptom("no_such_symptom")


def test_agent_reasoning_lands_only_on_the_model_tier() -> None:
    assert TAXONOMY.root_cause("agent_reasoning").fix_lands_on == (SurfaceKind.MODEL_TIER,)


def test_a_blame_refuses_a_surface_the_root_cause_never_lands_on() -> None:
    with pytest.raises(ValidationError, match="never fixed on a skill: it lands on tool"):
        Blame(what=TAXONOMY.root_cause("tool_error_message"), where=Surface(kind=SurfaceKind.SKILL, name="digesting"), why="x")


def test_a_finding_refuses_a_blame_the_symptom_does_not_list() -> None:
    symptom = TAXONOMY.symptom("no_tool_for_the_job")
    blame = Blame(what=TAXONOMY.root_cause("tool_error_message"), where=Surface(kind=SurfaceKind.TOOL, name="search"), why="x")

    with pytest.raises(ValidationError, match="cannot be behind no_tool_for_the_job"):
        Finding(symptom=symptom, evidence=(A_SPAN,), blames=(blame,), found_by="inspector")


def test_a_taxonomy_refuses_a_root_cause_named_twice() -> None:
    cause = RootCause(name="twice", meaning="x", fix_lands_on=(SurfaceKind.TOOL,))
    symptom = Symptom(name="s", shows_in="text", what_you_see="x", caught_by="rule", horizon="moment", possible_root_causes=(cause,))

    with pytest.raises(ValidationError, match="defined twice"):
        Taxonomy(root_causes=(cause, cause), symptoms=(symptom,))


def test_the_health_of_a_tool_is_no_longer_a_symptom_of_this_taxonomy() -> None:
    gone = {"tool_too_slow", "tool_raised_or_hung", "flaky_result", "auth_or_quota_refused", "upstream_host_changed", "silent_wrong_result"}

    assert gone & {symptom.name for symptom in TAXONOMY.symptoms} == set()
    assert {"tool_implementation", "infrastructure"} & {cause.name for cause in TAXONOMY.root_causes} == set()
    assert {kind.value for kind in SurfaceKind} == {"tool", "skill", "system_prompt", "model_tier"}


MOMENT = (
    "schema_rejected_arguments",
    "retried_after_vague_error",
    "warning_ignored",
    "uncertain_result_taken_as_fact",
    "result_misread",
    "docstring_mismatch",
    "misquoted_source",
    "output_shape_not_followed",
    "output_far_longer_than_needed",
    "thinking_far_beyond_norm",
    "valid_but_wrong_arguments",
    "expensive_tool_for_cheap_answer",
    "wrong_tool_chosen",
    "no_tool_for_the_job",
    "called_before_precondition",
    "scope_too_big_or_too_small",
)
RUN = (
    "duplicate_call",
    "denied_action_repeated",
    "retried_after_clear_error",
    "unbatched_calls",
    "serial_calls_instead_of_parallel",
    "result_overflowed_context",
    "budget_exhausted_unfinished",
    "gave_up_on_recoverable_error",
    "unsupported_claim",
    "claim_contradicts_evidence",
    "reported_done_when_not_done",
    "failure_left_out_of_report",
    "stopped_with_work_left",
    "answered_a_different_question",
    "did_not_do_what_it_was_told_to",
    "did_what_it_was_told_not_to",
    "acted_on_unchecked_assumption",
    "proceeded_on_ambiguous_request",
)
CONVERSATION = ("call_for_known_fact", "stale_reference_used", "ignored_user_correction", "asked_what_it_could_find")


def test_every_symptom_carries_the_horizon_the_design_gives_it() -> None:
    by_horizon: dict[Horizon, tuple[str, ...]] = {"moment": MOMENT, "run": RUN, "conversation": CONVERSATION}
    expected = {name: horizon for horizon, names in by_horizon.items() for name in names}

    assert {symptom.name: symptom.horizon for symptom in TAXONOMY.symptoms} == expected
