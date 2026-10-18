"""The shipped templates' workflow files, read as the cards a person chooses a workflow by.

Every template ships its workflows as data, and a person meets one of them before they meet a run.
These are the rules that data keeps: the author's own four sentences, a label for every parameter,
a declared result whose primary work and artifact sources name nodes the graph really has, and node
ids that name the job a node does rather than what the node leaves behind.

The files are found through `templates_root`, the one resolver that knows where `templates/` is, so
this walk follows the directory wherever `ENGRAM_TEMPLATES` or the repo layout puts it. The rules a
whole set can break gather every offender before failing, so one run names all of them.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from core.ai.workflows.contracts import FromNode, MapNode, WorkflowPresentation, load_spec_document
from core.ai.workflows.domain.presentation import SaveNoteAction

from engram.init.infrastructure.templates import templates_root

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from core.ai.workflows.contracts import SpecDocument

JOB_VERBS = frozenset(
    {
        "check",
        "count",
        "digest",
        "draft",
        "find",
        "finish",
        "fold",
        "link",
        "list",
        "merge",
        "notify",
        "present",
        "propose",
        "relate",
        "repair",
        "report",
        "review",
        "write",
    }
)
"""The verbs the shipped jobs are named with. A node id opens with one of them, so the id says what the
node does rather than what is left over once it has done it. A new kind of job adds its verb here as a
deliberate edit, which is the point of keeping the set closed."""

SHIPPED = {
    "research/research-digest",
    "research/research-compound",
    "research/research-decision-brief",
    "repo-docs/docs-promote-initiative",
    "repo-docs/docs-notes-to-analysis",
    "benchmark-dataset/benchmark-digest",
    "news-benchmark/news-digest",
    "news-benchmark/news-link",
    "news-benchmark/news-relate",
}
"""Every workflow the templates ship, by template slug and file stem: the walk is asserted against this
so a glob that stopped matching cannot make the whole module pass by finding nothing."""


def _files() -> list[Path]:
    return sorted(templates_root({}).glob("*/workflows/workflows/*.md"))


def _documents() -> Iterator[tuple[str, SpecDocument]]:
    for path in _files():
        yield f"{path.parents[2].name}/{path.stem}", load_spec_document(path.read_text(encoding="utf-8"))


def _presentation(document: SpecDocument) -> WorkflowPresentation | None:
    declared = document.frontmatter.get("presentation")
    return None if declared is None else WorkflowPresentation.model_validate(declared)


def _node_ids(document: SpecDocument) -> set[str]:
    return {node.id for node in document.workflow.nodes}


def test_every_shipped_template_workflow_is_walked() -> None:
    assert {name for name, _ in _documents()} == SHIPPED


def test_every_template_workflow_carries_the_four_sentences_a_reader_needs() -> None:
    unsaid: list[str] = []
    for name, document in _documents():
        words = _presentation(document)
        if words is None:
            unsaid.append(f"{name}: declares no presentation at all, so only the spec's own description describes it")
            continue
        unsaid.extend(
            f"{name}: says nothing about {says}"
            for field, says in (
                (words.purpose, "what it is for"),
                (words.produces, "what a finished run leaves the person holding"),
                (words.owner_participation, "what the person does during a run"),
                (words.not_for, "what to reach for something else for"),
            )
            if not field.strip()
        )
    assert unsaid == [], "\n".join(unsaid)


def test_every_parameter_is_asked_for_under_a_label() -> None:
    unlabelled: list[str] = []
    for name, document in _documents():
        words = _presentation(document)
        labelled: set[str] = set() if words is None else {shown.key for shown in words.inputs}
        declared = set(document.workflow.parameters)
        if labelled != declared:
            unlabelled.append(f"{name}: labels {sorted(labelled)}, but declares the parameters {sorted(declared)}")
    assert unlabelled == [], "\n".join(unlabelled)


def test_the_knowledge_base_is_asked_for_through_the_knowledge_base_control() -> None:
    """One input per workflow takes a registered knowledge base, so the host checks the answer against its registry."""
    wrong: list[str] = []
    for name, document in _documents():
        words = _presentation(document)
        through: list[str] = [] if words is None else [shown.key for shown in words.inputs if shown.control == "knowledge-base"]
        if len(through) != 1:
            wrong.append(f"{name}: asks for {len(through)} knowledge bases through the closed selector, wanted exactly one")
    assert wrong == [], "\n".join(wrong)


def test_every_template_workflow_declares_one_primary_work() -> None:
    wrong: list[str] = []
    for name, document in _documents():
        result = document.workflow.result
        if result is None:
            wrong.append(f"{name}: declares no result, so a reader is left to guess what the run was for")
            continue
        if not result.summary.strip():
            wrong.append(f"{name}: declares a result with no summary")
        if result.primary_work not in _node_ids(document):
            wrong.append(f"{name}: names primary work {result.primary_work!r}, which is no node of its graph")
    assert wrong == [], "\n".join(wrong)


def test_a_no_work_run_is_only_claimed_where_the_primary_work_is_a_map() -> None:
    """`noop` says an empty batch is a finished run with nothing to do, which only a map over a batch can mean."""
    for name, document in _documents():
        result = document.workflow.result
        if result is None or result.empty_primary != "noop":
            continue
        primary = next(node for node in document.workflow.nodes if node.id == result.primary_work)
        assert isinstance(primary, MapNode), f"{name} calls an empty {primary.kind} node a no-work success"


def test_every_declared_artifact_comes_off_a_node_the_graph_has() -> None:
    for name, document in _documents():
        result = document.workflow.result
        if result is None:
            continue
        ids = _node_ids(document)
        for artifact in result.artifacts:
            assert isinstance(artifact.source, FromNode), f"{name} artifact {artifact.id!r} does not come off a node"
            source = artifact.source.node
            assert source in ids, f"{name} artifact {artifact.id!r} comes off {source!r}, which is no node of its graph"
            assert artifact.label.strip(), f"{name} artifact {artifact.id!r} carries no label"


def test_every_artifact_action_names_an_artifact_the_result_declares() -> None:
    for name, document in _documents():
        words = _presentation(document)
        if words is None:
            continue
        result = document.workflow.result
        declared: set[str] = set() if result is None else {artifact.id for artifact in result.artifacts}
        for action in words.actions:
            wanted = (action.artifact_id,) if isinstance(action, SaveNoteAction) else action.artifact_ids
            for artifact_id in wanted:
                assert artifact_id in declared, f"{name} action {action.id!r} names artifact {artifact_id!r}, which it does not declare"


def test_every_recorded_output_field_reads_a_value_the_run_recorded() -> None:
    """A result field is display-only: it reads what the run recorded under a node, and selects no work."""
    for name, document in _documents():
        words = _presentation(document)
        if words is None:
            continue
        ids = _node_ids(document)
        for shown in words.result_fields:
            head, _, rest = shown.path.partition(".")
            assert head == "values", f"{name} shows {shown.path!r}, which does not read the run's recorded values"
            node_id = rest.partition(".")[0]
            assert node_id in ids, f"{name} shows {shown.path!r}, and {node_id!r} is no node of its graph"


def test_every_node_id_names_the_job_that_node_does() -> None:
    """A node id opens with a job verb. `flags` and `written` name the aftermath; `propose_flags` names the work."""
    aftermath: list[str] = []
    for name, document in _documents():
        aftermath.extend(
            f"{name}: node {node.id!r} opens with {node.id.partition('_')[0]!r}, which names no job"
            for node in document.workflow.nodes
            if node.id.partition("_")[0] not in JOB_VERBS
        )
    advice = (
        "Rename the node after the job it does. When the node really does a kind of job nothing here names yet, "
        f"add that verb to JOB_VERBS in this file. The verbs on file: {sorted(JOB_VERBS)}."
    )
    assert aftermath == [], "\n".join([*aftermath, advice])
