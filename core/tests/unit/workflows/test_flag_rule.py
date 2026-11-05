"""Every writer of a maintenance flag follows one rule, read from the page under the flag's id before it writes.

No page writes the flag open. An open page takes the new evidence. A resolved page keeps a person's decision for the
same step version and opens again for another one. A page an automatic resolver resolved opens again on any new flag.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from pydantic import ValidationError

from core.ai.workflows.contracts import AUTOMATIC_RESOLVERS, MaintenanceFlag, flag_id, flag_to_write

RAISED = datetime(2026, 11, 5, 9, 0, tzinfo=UTC)


def _flag(**changes: object) -> MaintenanceFlag:
    fields: dict[str, object] = {
        "kind": "diverged_copy",
        "step": "digest",
        "step_id": "st.digest.v3",
        "step_hash": "a" * 64,
        "workflow": "wf.digest-sources.v2",
        "title": "Diverged copy of digest in digest-sources",
        "summary": "Decide whether the copy stays.",
        "evidence": "It diverged from st.digest.v3.",
        "raised_by": "library",
        "raised_at": RAISED,
    }
    fields.update(changes)
    return MaintenanceFlag.model_validate(fields)


def _resolved(flag: MaintenanceFlag, by: str = "owner") -> MaintenanceFlag:
    return flag.model_copy(update={"status": "resolved", "resolved_by": by, "decision_reason": "The copy is right for this workflow."})


def test_no_stored_page_writes_the_new_flag_open() -> None:
    new = _flag()
    assert flag_to_write(None, new) == new
    assert new.status == "open"


def test_an_open_page_takes_the_new_evidence() -> None:
    stored = _flag()
    new = _flag(evidence="It diverged from st.digest.v3, and now differs in two lines.", raised_at=RAISED + timedelta(days=1))
    assert flag_to_write(stored, new) == new


def test_an_open_page_holding_the_same_evidence_is_left_alone() -> None:
    stored = _flag()
    assert flag_to_write(stored, _flag(raised_at=RAISED + timedelta(days=1))) is None, "no write keeps the first raised_at"


def test_a_resolved_page_with_the_same_step_hash_is_left_resolved() -> None:
    stored = _resolved(_flag())
    new = _flag(evidence="The same divergence, written again by the next save.", raised_at=RAISED + timedelta(days=1))
    assert flag_to_write(stored, new) is None, "the owner already decided this step version"


def test_the_automatic_resolvers_are_the_library_and_step_maintenance() -> None:
    assert frozenset({"library", "agent:step-maintenance"}) == AUTOMATIC_RESOLVERS


@pytest.mark.parametrize("by", ["library", "agent:step-maintenance"])
def test_a_page_an_automatic_resolver_resolved_opens_again_for_the_same_step_version(by: str) -> None:
    stored = _resolved(_flag(), by=by)
    new = _flag(evidence="The copy diverged from st.digest.v3 again.", raised_at=RAISED + timedelta(days=1))
    assert flag_to_write(stored, new) == new, "a condition that cleared on its own is no decision"


def test_a_resolved_page_with_another_step_hash_opens_again_with_the_new_evidence() -> None:
    stored = _resolved(_flag())
    new = _flag(step_id="st.digest.v4", step_hash="b" * 64, evidence="It diverged from st.digest.v4.")
    written = flag_to_write(stored, new)
    assert written == new
    assert written is not None
    assert (written.status, written.resolved_by, written.decision_reason) == ("open", "", "")


def test_a_writer_never_writes_a_resolution_of_its_own() -> None:
    written = flag_to_write(None, _resolved(_flag()))
    assert written is not None
    assert (written.status, written.resolved_by, written.decision_reason) == ("open", "", "")


@pytest.mark.parametrize(
    ("kind", "step", "workflow_name", "expected"),
    [
        ("diverged_copy", "digest", "digest-sources", "mf.diverged.digest-sources.digest"),
        ("consumer_flag", "extract_references", "research-digest", "mf.consumer.research-digest.extract_references"),
        ("live_trial_reverted", "digest", "digest-sources", "mf.reverted.digest-sources.digest"),
        ("plan_rejected", "digest", "", "mf.rejected.digest"),
        ("repository_change", "digest_source", "", "mf.change.digest_source"),
    ],
)
def test_a_flag_id_names_the_kind_the_workflow_and_the_step(kind: str, step: str, workflow_name: str, expected: str) -> None:
    flag = _flag(kind=kind)
    assert flag_id(flag.kind, step, workflow_name) == expected


def test_a_flag_id_about_a_step_takes_no_workflow_name() -> None:
    assert flag_id("plan_rejected", "digest") == "mf.rejected.digest"


def test_a_flag_refuses_a_raised_at_without_its_offset() -> None:
    with pytest.raises(ValidationError, match="timezone"):
        _flag(raised_at=datetime(2026, 11, 5, 9, 0))  # noqa: DTZ001 (the naive time the flag refuses)


def test_a_flag_refuses_a_field_it_does_not_hold() -> None:
    with pytest.raises(ValidationError, match="extra"):
        _flag(owner_note="no such field")


@pytest.mark.parametrize("field", ["evidence", "proposed_change"])
@pytest.mark.parametrize(
    "body",
    [
        "Two consumers split.\n\n## Drafted step\n\nname: digest",
        "Two consumers split.\n\n### Drafted step",
        "name: digest\nversion: 3\n---\n\nThe drafted step's text.",
        "Two consumers split.\n\n```yaml\nname: digest\n",
    ],
    ids=["level 2 heading", "level 3 heading", "setext level 2 heading, a step file's frontmatter close", "fence left open"],
)
def test_a_flag_refuses_text_its_page_would_cut_into_another_section(field: str, body: str) -> None:
    with pytest.raises(ValidationError, match=r"fenced block") as refused:
        _flag(**{field: body})
    assert "## " in str(refused.value)
    assert "`---` line under text" in str(refused.value)


@pytest.mark.parametrize("field", ["evidence", "proposed_change"])
@pytest.mark.parametrize(
    "body",
    ["Coverage is 50%% of sources and 100%% when done.", "```text\n%% a note %%\n```"],
    ids=["a pair in prose", "a pair inside a fence"],
)
def test_a_flag_refuses_a_pair_of_double_percent_signs_its_page_would_drop(field: str, body: str) -> None:
    with pytest.raises(ValidationError, match="%%") as refused:
        _flag(**{field: body})
    assert "single %" in str(refused.value)


@pytest.mark.parametrize(
    "body",
    [
        "```markdown\n## Drafted step\n\nname: digest\n---\n```",
        "#### A level 4 heading stays in the text",
        "# A level 1 heading stays in the text",
        "A setext level 1 heading\n===\n\nstays in the text.",
        "Coverage is 50%% of sources.",
        "- research-digest adopted\n- source-remove kept",
        "",
    ],
    ids=["heading inside a fence", "level 4 heading", "level 1 heading", "setext level 1 heading", "one double percent", "list", "empty"],
)
def test_a_flag_keeps_text_that_stays_one_section(body: str) -> None:
    assert _flag(proposed_change=body).proposed_change == body


def test_a_flag_keeps_its_sections_without_the_whitespace_at_their_edges_as_its_page_does() -> None:
    flag = _flag(evidence="\n\n  It diverged from st.digest.v3.\n", proposed_change="  Retype the output.   \n")
    assert (flag.evidence, flag.proposed_change) == ("It diverged from st.digest.v3.", "Retype the output.")
    again = _flag(evidence="It diverged from st.digest.v3.\n", proposed_change="Retype the output.", raised_at=RAISED + timedelta(days=1))
    assert flag_to_write(flag, again) is None, "the same text read back from the page is the same flag"
