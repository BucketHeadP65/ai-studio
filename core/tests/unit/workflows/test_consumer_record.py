"""The consumer record: which saved version of each reusable step a workflow's copy matches, found by content address."""

from __future__ import annotations

import pytest

from pydantic import ValidationError

from core.ai.workflows.contracts import (
    Briefing,
    ConsumerEntry,
    FromNode,
    FromParameter,
    InlineStep,
    ParameterSpec,
    RunNode,
    Shape,
    StepVersion,
    Workflow,
    consumer_record,
    step_content_address,
)

TEXT = Shape(json_schema={"type": "string"})

DIGEST_V1 = "Read the source and write one digest note about it.\nQuote what it claims.\nName the source first."
DIGEST_V2 = "Read the source and write one digest note about it.\nQuote what it claims.\nName the author first."
DIGEST_V3 = "Read the source closely and write one digest note.\nQuote every claim you keep.\nName the author and the year."
FINISH_V1 = "Read the digest note and report what changed in the knowledge base, page by page."


def _briefing(name: str, instructions: str) -> Briefing:
    return Briefing(name=name, instructions=instructions, inputs={"source": TEXT}, output=TEXT)


def _version(step: InlineStep, version: int) -> StepVersion:
    return StepVersion(
        name=step.name, version=version, step_id=f"st.{step.name}.v{version}", step_hash=step_content_address(step), step=step
    )


def _workflow(*inline: InlineStep) -> Workflow:
    """A workflow with one node per inline step, each reading the `source` parameter."""
    return Workflow(
        name="digest-one",
        version=2,
        description="Digest one source and report it.",
        parameters={"source": ParameterSpec(description="a source id", json_schema={"type": "string"})},
        inline=inline,
        nodes=tuple(RunNode(id=step.name, work=step.name, inputs={"source": FromParameter(parameter="source")}) for step in inline),
        output=FromNode(node=inline[-1].name),
    )


DIGESTS = (
    _version(_briefing("digest", DIGEST_V1), 1),
    _version(_briefing("digest", DIGEST_V2), 2),
    _version(_briefing("digest", DIGEST_V3), 3),
)


def test_a_copy_equal_to_a_saved_version_names_that_version() -> None:
    copy = _briefing("digest", DIGEST_V3)

    record = consumer_record(_workflow(copy), {"digest": DIGESTS})

    assert record == (ConsumerEntry(step="digest", version=3, step_hash=step_content_address(copy)),)


def test_a_copy_equal_to_two_saved_versions_names_the_higher() -> None:
    copy = _briefing("digest", DIGEST_V2)
    versions = (*DIGESTS, _version(copy, 4))

    record = consumer_record(_workflow(copy), {"digest": versions})

    assert record[0].version == 4


def test_an_inline_step_whose_name_no_reusable_step_has_gets_no_entry() -> None:
    summary = _briefing("summary", FINISH_V1)
    tag = _briefing("tag", FINISH_V1)

    record = consumer_record(_workflow(summary, tag, _briefing("digest", DIGEST_V1)), {"digest": DIGESTS, "tag": ()})

    assert [entry.step for entry in record] == ["digest"]


def test_a_diverged_copy_names_the_version_its_parent_record_named() -> None:
    copy = _briefing("digest", DIGEST_V3.replace("closely", "carefully"))
    parent = (ConsumerEntry(step="digest", version=2, step_hash=DIGESTS[1].step_hash),)

    record = consumer_record(_workflow(copy), {"digest": DIGESTS}, parent=parent)

    assert record == (
        ConsumerEntry(step="digest", version=None, step_hash=step_content_address(copy), diverged_from=2, diverged_basis="parent"),
    )


def test_a_diverged_copy_keeps_the_version_a_diverged_parent_traced_to_its_own_parent() -> None:
    copy = _briefing("digest", DIGEST_V3.replace("closely", "carefully"))
    parent = (ConsumerEntry(step="digest", version=None, step_hash="0" * 64, diverged_from=1, diverged_basis="parent"),)

    record = consumer_record(_workflow(copy), {"digest": DIGESTS}, parent=parent)

    assert (record[0].diverged_from, record[0].diverged_basis) == (1, "parent")


def test_a_diverged_copy_whose_parent_only_guessed_is_matched_again_by_text() -> None:
    copy = _briefing("digest", DIGEST_V3.replace("closely", "carefully"))
    parent = (ConsumerEntry(step="digest", version=None, step_hash="0" * 64, diverged_from=1, diverged_basis="closest"),)

    record = consumer_record(_workflow(copy), {"digest": DIGESTS}, parent=parent)

    assert (record[0].diverged_from, record[0].diverged_basis) == (3, "closest")


def test_a_diverged_copy_with_no_parent_entry_names_the_closest_saved_version() -> None:
    copy = _briefing("digest", DIGEST_V1.replace("one digest", "a digest"))

    record = consumer_record(_workflow(copy), {"digest": tuple(reversed(DIGESTS))})

    assert record == (
        ConsumerEntry(step="digest", version=None, step_hash=step_content_address(copy), diverged_from=1, diverged_basis="closest"),
    )


def test_a_tie_for_the_closest_saved_version_goes_to_the_higher_version() -> None:
    copy = _briefing("digest", DIGEST_V1.replace("one digest", "a digest"))
    versions = (_version(_briefing("digest", DIGEST_V1), 2), _version(_briefing("digest", DIGEST_V1), 5), DIGESTS[2])

    record = consumer_record(_workflow(copy), {"digest": versions})

    assert (record[0].diverged_from, record[0].diverged_basis) == (5, "closest")


def test_a_decline_carries_over_while_the_copy_is_unchanged() -> None:
    copy = _briefing("digest", DIGEST_V2)
    parent = (
        ConsumerEntry(
            step="digest", version=2, step_hash=step_content_address(copy), declined=DIGESTS[2].step_hash, declined_because="reverted"
        ),
    )

    record = consumer_record(_workflow(copy), {"digest": DIGESTS}, parent=parent)

    assert (record[0].declined, record[0].declined_because) == (DIGESTS[2].step_hash, "reverted")


def test_a_decline_is_dropped_once_the_copy_changed() -> None:
    parent = (
        ConsumerEntry(step="digest", version=2, step_hash=DIGESTS[1].step_hash, declined=DIGESTS[2].step_hash, declined_because="kept"),
    )

    record = consumer_record(_workflow(_briefing("digest", DIGEST_V1)), {"digest": DIGESTS}, parent=parent)

    assert record == (ConsumerEntry(step="digest", version=1, step_hash=DIGESTS[0].step_hash),)


def test_entries_come_back_sorted_by_step_name() -> None:
    finish = _briefing("finish", FINISH_V1)

    record = consumer_record(_workflow(finish, _briefing("digest", DIGEST_V1)), {"digest": DIGESTS, "finish": (_version(finish, 1),)})

    assert [entry.step for entry in record] == ["digest", "finish"]


@pytest.mark.parametrize(
    ("fields", "message"),
    [
        ({"version": None}, "a copy that matches no saved version names the version it diverged from"),
        ({"version": 2, "diverged_from": 1, "diverged_basis": "parent"}, "a copy that matches version 2 did not diverge"),
        ({"version": None, "diverged_from": 1}, "diverged_from and diverged_basis come together"),
        ({"version": 2, "declined": "a" * 64}, "declined and declined_because come together"),
        ({"version": 2, "declined_because": "kept"}, "declined and declined_because come together"),
    ],
)
def test_an_entry_that_contradicts_itself_is_refused(fields: dict[str, int | str | None], message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        ConsumerEntry.model_validate({"step": "digest", "step_hash": "f" * 64, **fields})
