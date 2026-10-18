"""The contract port over a directory of files, to find out whether the seam is real.

`LibraryVocabulary` had exactly one instance -- engram's -- so "the port adapts any host, it is not
engram's shape" was an assertion with nothing behind it. This drives the same port over
`folder_toolset()`: no SQL, no MCP, no `#graph` unit ids, different argument names, and a write model
that creates a file per version instead of upserting a page and recompiling.

Every test here goes through the port's own tools, not through `FolderLibrary`. Calling the store
directly would test the store; the question is whether the *adapters* absorb a host this different.
The presentation tests at the foot are the one exception: the port publishes no presentation write,
so the store is the only place one can be put in and read back.
"""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

import pytest
import yaml

from pydantic import ValidationError
from pydantic_ai.models.test import TestModel
from pydantic_ai.tools import RunContext
from pydantic_ai.usage import RunUsage

from core.ai.workflows.contract_port import ADAPT_TOOL, FETCH_TOOL, SAVE_TOOL, SEARCH_TOOL, WorkflowWrite
from core.ai.workflows.contracts import (
    Briefing,
    FromNode,
    FromParameter,
    InputPresentation,
    ParameterSpec,
    PresentationRefusedError,
    Program,
    ResultArtifactSpec,
    ResultFieldPresentation,
    RunNode,
    Shape,
    StepRegistry,
    Workflow,
    WorkflowContract,
    WorkflowIntent,
    WorkflowPresentation,
    WorkflowResultSpec,
    intent_issues,
)
from core.ai.workflows.domain.presentation import RecordOwnerReviewAction, SaveNoteAction
from core.ai.workflows.infrastructure.folder import FOLDER, FolderLibrary, folder_toolset

if TYPE_CHECKING:
    from pathlib import Path


async def _perform(task: Briefing, output_type: type, /, **inputs: object) -> object:
    del task, inputs
    return output_type(note="done")  # type: ignore[call-arg]


async def _execute(step: Program, output_type: type, /, **inputs: object) -> object:
    del step, inputs
    return output_type(note="done")  # type: ignore[call-arg]


def _spec(name: str = "audit-a-claim") -> Workflow:
    """A workflow that brings its own step, so saving has something real to verify."""
    return Workflow(
        name=name,
        version=1,
        description="Check one contested claim and report what would settle it.",
        parameters={"claim": ParameterSpec(description="the claim", json_schema={"type": "string"})},
        inline=(
            Briefing(
                name="weigh",
                instructions="Weigh the claim against what the sources actually say. Do not soften a conflict.",
                inputs={"claim": Shape(json_schema={"type": "string"})},
                output=Shape(json_schema={"type": "object", "properties": {"note": {"type": "string"}}, "required": ["note"]}),
            ),
        ),
        nodes=(RunNode(id="weigh", work="weigh", inputs={"claim": FromParameter(parameter="claim")}),),
        output={"node": "weigh"},  # type: ignore[arg-type]
    )


def _yaml(spec: Workflow) -> str:
    """`spec` as the YAML text `save_workflow`'s `spec_yaml` argument now takes."""
    return yaml.safe_dump(spec.model_dump(mode="json"), sort_keys=False)


@pytest.fixture
def port(tmp_path: Path) -> WorkflowContract[None]:
    """The port over a directory, with a registry that can perform a briefing so saving is offered."""
    return WorkflowContract[None](
        source=folder_toolset(tmp_path / "workflows"),
        vocabulary=FOLDER,
        expose=(),
        registry=StepRegistry(perform=_perform),
        saved_by="test",
    )


@pytest.fixture
def sandboxed(tmp_path: Path) -> WorkflowContract[None]:
    """The same port over a run that can execute a program too, so a briefing may be adapted into one."""
    return WorkflowContract[None](
        source=folder_toolset(tmp_path / "workflows"),
        vocabulary=FOLDER,
        expose=(),
        registry=StepRegistry(perform=_perform, run_code=_execute),
        saved_by="test",
    )


async def _call(port: WorkflowContract[None], name: str, **args: object):
    toolset = port.get_toolset()
    assert toolset is not None
    ctx: RunContext[None] = RunContext(deps=None, model=TestModel(), usage=RunUsage())
    tools = await toolset.get_tools(ctx)
    return await toolset.call_tool(name, dict(args), ctx, tools[name])


async def test_the_contract_tools_are_the_same_four(port: WorkflowContract[None]) -> None:
    """A different host must not change the surface. `expose=()` means only the contract shows."""
    toolset = port.get_toolset()
    assert toolset is not None

    ctx: RunContext[None] = RunContext(deps=None, model=TestModel(), usage=RunUsage())
    assert set(await toolset.get_tools(ctx)) == {SEARCH_TOOL, FETCH_TOOL, SAVE_TOOL, ADAPT_TOOL}


async def test_a_port_that_was_never_told_what_to_expose_exposes_nothing(tmp_path: Path) -> None:
    """The verified write path is the only door by default, not once an integrator remembers.

    `folder_toolset` offers a raw `write_file`, and the guarantee that `save_workflow` is the only
    route into the store rests on that tool never reaching the model. A default that showed the
    host's whole surface would make the guarantee opt-in, which is the one thing a structural
    guarantee cannot be.
    """
    unconfigured = WorkflowContract[None](
        source=folder_toolset(tmp_path / "workflows"), vocabulary=FOLDER, registry=StepRegistry(perform=_perform), saved_by="test"
    )
    toolset = unconfigured.get_toolset()
    assert toolset is not None
    ctx: RunContext[None] = RunContext(deps=None, model=TestModel(), usage=RunUsage())

    assert set(await toolset.get_tools(ctx)) == {SEARCH_TOOL, FETCH_TOOL, SAVE_TOOL, ADAPT_TOOL}


async def test_the_folder_catalog_tool_answers_a_total_alongside_its_rows(tmp_path: Path) -> None:
    """`list_files` gains a `total` field; the row shape `find_files` already answers in is untouched."""
    root = tmp_path / "workflows"
    port = WorkflowContract[None](
        source=folder_toolset(root), vocabulary=FOLDER, expose=("list_files",), registry=StepRegistry(perform=_perform), saved_by="test"
    )
    await _call(port, SAVE_TOOL, spec_yaml=_yaml(_spec(name="a")), applicability="Use when a. Not otherwise.")
    await _call(port, SAVE_TOOL, spec_yaml=_yaml(_spec(name="b")), applicability="Use when b. Not otherwise.")

    answer = await _call(port, "list_files")

    assert answer["total"] == 2
    assert [row["name"] for row in answer["files"]] == ["a@1", "b@1"]


async def test_an_empty_directory_says_nothing_matched_rather_than_failing(port: WorkflowContract[None]) -> None:
    """The first call a fresh store gets. "No candidates" is an answer; a traceback is not."""
    answer = await _call(port, SEARCH_TOOL, task="audit a contested claim")
    assert "no saved workflow matches" in answer


async def test_save_then_search_then_fetch_round_trips_through_files(port: WorkflowContract[None]) -> None:
    """The whole contract over a directory: keep it, find it by words, read the spec back as data.

    The round trip is the point. Saving goes out as `write_file(title, summary, when, spec, by)` and
    comes back as a name the *host* minted (`audit-a-claim@1`, not a `wf.` page id); search finds it
    by token overlap rather than BM25; fetch reads it by that name with no `#graph` suffix. The port
    never learns any of that.
    """
    saved = await _call(
        port, SAVE_TOOL, spec_yaml=_yaml(_spec()), applicability="Use when one claim is contested. Do not use for a broad multi-source audit."
    )
    assert "saved as audit-a-claim@1" in saved, saved

    found = await _call(port, SEARCH_TOOL, task="contested claim audit")
    assert "audit-a-claim@1" in found
    assert "Do not use for a broad" in found, "the applicability half that decides reuse survives the host"

    read_back = await _call(port, FETCH_TOOL, workflow_id="audit-a-claim@1")
    assert "audit-a-claim v1" in read_back["summary"]
    assert "weigh" in read_back["summary"]  # the step it brought
    assert read_back["fits_here"] is True, "a spec that brings its own step fits a run that can perform one"
    assert "FITS THIS RUN" in read_back["fit"]


async def test_the_catalog_listing_drops_the_when_line_a_real_search_hit_still_carries(port: WorkflowContract[None]) -> None:
    """The zero-hit library trades the applicability note for width; a real hit keeps its note."""
    await _call(
        port, SAVE_TOOL, spec_yaml=_yaml(_spec()), applicability="Use when one claim is contested. Do not use for a broad multi-source audit."
    )

    hit = await _call(port, SEARCH_TOOL, task="contested claim audit")
    assert "Do not use for a broad" in hit, "a real search hit still carries its applicability note"

    listing = await _call(port, SEARCH_TOOL, task="something nobody wrote about")
    assert "audit-a-claim@1" in listing
    assert "when:" not in listing, "the catalog listing drops the when: line"
    assert "Do not use for a broad" not in listing
    assert "1 saved workflow, all of it follows." in listing, "one workflow in the library, and the header says so"
    assert "Narrow with words from the request, or fetch one by id with fetch_workflow." in listing


async def test_a_spec_that_will_not_run_is_refused_before_a_file_is_written(tmp_path: Path) -> None:
    """The guarantee has to hold on every host, not just the one it was written for."""
    root = tmp_path / "workflows"
    port = WorkflowContract[None](
        source=folder_toolset(root),
        vocabulary=FOLDER,
        expose=(),
        registry=StepRegistry(),  # cannot perform a briefing, so a spec bringing a step cannot be built
        saved_by="test",
    )

    refused = await _call(port, SAVE_TOOL, spec_yaml=_yaml(_spec()), applicability="Use when. Do not use when.")
    assert "REFUSED" in str(refused)
    assert list(root.glob("*.md")) == [], "nothing reached the disk"


# --- adaptation through the contract: the model supplies ops, the machinery derives the diff ------


async def test_the_port_can_adapt_a_stored_workflow_and_the_diff_travels(port: WorkflowContract[None], tmp_path: Path) -> None:
    """The agent's route to a new version, which until now silently produced an orphan.

    `save_workflow` could store a spec that *happened* to be version n+1 of something, but
    `WorkflowWrite` had no lineage slot -- so the child arrived recorded as nobody's, its parent
    kept nothing pointing forward, and every node lost the evidence its parent's runs had earned.
    The app's own `/api/adapt` had all of this; the agent, which authors most artifacts, had none.

    The model supplies *ops* and never a lineage: a diff is derived by the machinery from what it
    itself applied, because a lineage a model could assert is a lineage a model could launder.
    """
    await _call(port, SAVE_TOOL, spec_yaml=_yaml(_spec()), applicability="Use when one claim is contested. Do not use it broadly.")

    answer = await _call(
        port,
        ADAPT_TOOL,
        workflow_id="audit-a-claim@1",
        ops=[{"op": "set_description", "to": "Check one contested claim, with the sharper briefing."}],
        applicability="Use when one claim is contested. Do not use it broadly.",
    )
    assert "audit-a-claim@2" in answer, answer

    store = FolderLibrary(tmp_path / "workflows")
    child = await store.fetch_workflow("audit-a-claim@2")
    assert child.version == 2
    assert child.description == "Check one contested claim, with the sharper briefing."
    kept = await store.fetch_lineage("audit-a-claim@2")
    assert kept is not None, "the diff travelled with the write"
    assert (kept.parent_name, kept.parent_version) == ("audit-a-claim", 1)
    assert await store.fetch_workflow("audit-a-claim@1") is not None, "the incumbent is untouched"


async def test_an_op_naming_a_ghost_is_a_correction_not_a_crash(port: WorkflowContract[None]) -> None:
    """`AdaptationRefusedError` carries exactly the sentence the model needs; a traceback carries none."""
    await _call(port, SAVE_TOOL, spec_yaml=_yaml(_spec()), applicability="Use when one claim is contested. Do not use it broadly.")
    answer = await _call(port, ADAPT_TOOL, workflow_id="audit-a-claim@1", ops=[{"op": "remove_node", "id": "ghost"}], applicability="Use when.")
    assert "ghost" in str(answer)
    assert "refused" in str(answer)


async def test_an_adaptation_that_will_not_run_is_refused_before_the_write(port: WorkflowContract[None], tmp_path: Path) -> None:
    """Coherent ops can still produce a graph with nothing to answer; the same gate refuses it."""
    await _call(port, SAVE_TOOL, spec_yaml=_yaml(_spec()), applicability="Use when one claim is contested. Do not use it broadly.")
    # Removing the only node leaves the output wire dangling: ops fine, graph unexecutable.
    answer = await _call(port, ADAPT_TOOL, workflow_id="audit-a-claim@1", ops=[{"op": "remove_node", "id": "weigh"}], applicability="Use when.")
    assert "REFUSED" in str(answer)
    store = FolderLibrary(tmp_path / "workflows")
    assert await store.locate("audit-a-claim", 2) is None, "nothing reached the disk"


async def test_the_port_can_replace_a_brought_step_with_a_program(sandboxed: WorkflowContract[None], tmp_path: Path) -> None:
    """The op a live studio conversation asked for, over the whole surface the agent actually uses.

    The agent was asked to turn a brought briefing into a program and had nothing to answer with:
    the vocabulary could shed a task and never replace one, so the only route stored an orphan. This
    is that turn, end to end -- ops in as data, the child validated against this run's steps before
    a file is written, and the node running the swapped step named as no longer carrying.
    """
    await _call(sandboxed, SAVE_TOOL, spec_yaml=_yaml(_spec()), applicability="Use when one claim is contested. Do not use it broadly.")
    program = Program(
        name="weigh",
        source="{'note': claim.strip()}",
        inputs={"claim": Shape(json_schema={"type": "string"})},
        output=Shape(json_schema={"type": "object", "properties": {"note": {"type": "string"}}, "required": ["note"]}),
    )

    answer = await _call(
        sandboxed,
        ADAPT_TOOL,
        workflow_id="audit-a-claim@1",
        ops=[{"op": "replace_inline_step", "name": "weigh", "with": program.model_dump(mode="json")}],
        applicability="Use when one claim is contested. Do not use it broadly.",
    )
    assert "audit-a-claim@2" in answer, answer
    assert "except weigh" in answer, "the node running the swapped step is the one that stopped carrying"

    store = FolderLibrary(tmp_path / "workflows")
    child = await store.fetch_workflow("audit-a-claim@2")
    assert child.inline == (program,), "the program travelled through the tool call, the write and the file"
    kept = await store.fetch_lineage("audit-a-claim@2")
    assert kept is not None
    assert kept.touched == ("weigh",)
    assert (await store.fetch_workflow("audit-a-claim@1")).inline == _spec().inline, "the incumbent keeps the briefing"


async def test_a_replacement_naming_a_task_that_is_not_there_is_a_correction_not_a_crash(port: WorkflowContract[None]) -> None:
    await _call(port, SAVE_TOOL, spec_yaml=_yaml(_spec()), applicability="Use when one claim is contested. Do not use it broadly.")
    answer = await _call(
        port,
        ADAPT_TOOL,
        workflow_id="audit-a-claim@1",
        ops=[{"op": "replace_inline_step", "name": "ghost", "with": _spec().inline[0].model_dump(mode="json")}],
        applicability="Use when.",
    )
    assert "ghost" in str(answer)


async def test_a_read_only_store_offers_no_adapt_tool(tmp_path: Path) -> None:
    """Same rule as saving: a tool that would fail when called is worse than no tool."""
    read_only = replace(FOLDER, put_tool=None, put_args=None, to_id=None)
    port = WorkflowContract[None](
        source=folder_toolset(tmp_path / "workflows"),
        vocabulary=read_only,
        expose=(),
        registry=StepRegistry(perform=_perform),
        saved_by="test",
    )
    toolset = port.get_toolset()
    assert toolset is not None

    ctx: RunContext[None] = RunContext(deps=None, model=TestModel(), usage=RunUsage())
    assert set(await toolset.get_tools(ctx)) == {SEARCH_TOOL, FETCH_TOOL}


# --- the presentation a card carries -------------------------------------------------------------
#
# Driven against `FolderLibrary` rather than through the port's tools: the port publishes no
# presentation write, so the store is the only place a presentation can be put in and read back.


def _presented_spec() -> Workflow:
    """The same workflow, with two parameters and one declared result artifact to point a presentation at."""
    return Workflow(
        name="audit-a-claim",
        version=1,
        description="Check one contested claim and report what would settle it.",
        parameters={
            "claim": ParameterSpec(description="the claim", json_schema={"type": "string"}),
            "deadline": ParameterSpec(description="when the answer is needed", json_schema={"type": "string"}),
        },
        inline=(
            Briefing(
                name="weigh",
                instructions="Weigh the claim against what the sources actually say. Do not soften a conflict.",
                inputs={"claim": Shape(json_schema={"type": "string"})},
                output=Shape(json_schema={"type": "object", "properties": {"note": {"type": "string"}}, "required": ["note"]}),
            ),
        ),
        nodes=(RunNode(id="weigh", work="weigh", inputs={"claim": FromParameter(parameter="claim")}),),
        output={"node": "weigh"},  # type: ignore[arg-type]
        result=WorkflowResultSpec(
            summary="The note the run settled on.",
            primary_work="weigh",
            artifacts=(
                ResultArtifactSpec(id="note", label="The note", source=FromNode(node="weigh"), field="note", media_type="text/markdown"),
            ),
        ),
    )


def _presentation() -> WorkflowPresentation:
    """Every field the card must carry back unchanged."""
    return WorkflowPresentation(
        purpose="Settle one contested claim against the sources you already hold.",
        produces="A note naming what would settle the claim, and what is still open.",
        owner_participation="You read the note and decide whether to keep it.",
        not_for="Auditing every claim in a long document at once.",
        inputs=(
            InputPresentation(key="claim", label="The claim to weigh", control="textarea"),
            InputPresentation(key="deadline", label="When you need the answer"),
        ),
        result_fields=(ResultFieldPresentation(label="Sources weighed", path="counts.succeeded"),),
        actions=(SaveNoteAction(id="keep_the_note", label="Save the note", artifact_id="note"),),
    )


async def test_a_presentation_comes_back_unchanged_from_every_folder_surface(tmp_path: Path) -> None:
    """Put it once, then read it off search, catalog, the card, redescribe, archive and restore."""
    store = FolderLibrary(tmp_path / "workflows")
    presentation = _presentation()
    workflow_id = await store.put(
        _presented_spec(),
        title="Audit a claim",
        summary="One contested claim.",
        applicability="Use when one claim is contested.",
        provenance={"saved_by": "test"},
        presentation=presentation,
    )

    assert (await store.search("claim"))[0].presentation == presentation
    assert (await store.catalog())[0].presentation == presentation
    card = await store.fetch_card(workflow_id)
    assert card is not None
    assert card.presentation == presentation

    await store.redescribe(workflow_id, title="Audit one claim", summary="One claim.", applicability="Use when one claim is contested.")
    redescribed = await store.fetch_card(workflow_id)
    assert redescribed is not None
    assert redescribed.presentation == presentation, "redescribe rewrites prose, never the presentation"

    await store.set_archived(workflow_id, archived=True)
    assert (await store.archived())[0].presentation == presentation
    await store.set_archived(workflow_id, archived=False)
    restored = await store.fetch_card(workflow_id)
    assert restored is not None
    assert restored.presentation == presentation


async def test_a_card_written_without_a_presentation_reads_as_none_and_keeps_its_own_words(tmp_path: Path) -> None:
    """A legacy file carries no presentation, and nothing invents one for it."""
    store = FolderLibrary(tmp_path / "workflows")
    workflow_id = await store.put(
        _spec(),
        title="Audit a claim",
        summary="One contested claim.",
        applicability="Use when one claim is contested.",
        provenance={"saved_by": "test"},
    )

    card = await store.fetch_card(workflow_id)
    assert card is not None
    assert card.presentation is None
    assert (card.id, card.title, card.summary, card.applicability) == (
        "audit-a-claim@1",
        "Audit a claim",
        "One contested claim.",
        "Use when one claim is contested.",
    )
    assert "presentation" not in (tmp_path / "workflows" / "audit-a-claim-v1.md").read_text(encoding="utf-8")


async def test_a_presentation_naming_an_input_the_workflow_has_no_parameter_for_is_refused_before_the_write(tmp_path: Path) -> None:
    store = FolderLibrary(tmp_path / "workflows")
    with pytest.raises(PresentationRefusedError, match="quarter"):
        await store.put(
            _presented_spec(),
            title="Audit a claim",
            summary="One contested claim.",
            applicability="Use when.",
            provenance={"saved_by": "test"},
            presentation=WorkflowPresentation(inputs=(InputPresentation(key="quarter", label="Which quarter"),)),
        )
    assert list((tmp_path / "workflows").glob("*.md")) == [], "a refused presentation leaves nothing on disk"


async def test_a_presentation_naming_an_artifact_the_workflow_never_declared_is_refused_before_the_write(tmp_path: Path) -> None:
    store = FolderLibrary(tmp_path / "workflows")
    with pytest.raises(PresentationRefusedError, match="transcript"):
        await store.put(
            _presented_spec(),
            title="Audit a claim",
            summary="One contested claim.",
            applicability="Use when.",
            provenance={"saved_by": "test"},
            presentation=WorkflowPresentation(actions=(SaveNoteAction(id="keep", label="Save it", artifact_id="transcript"),)),
        )
    assert list((tmp_path / "workflows").glob("*.md")) == [], "a refused presentation leaves nothing on disk"


async def test_a_review_action_naming_an_undeclared_artifact_is_refused_before_the_write(tmp_path: Path) -> None:
    store = FolderLibrary(tmp_path / "workflows")
    with pytest.raises(PresentationRefusedError, match="transcript"):
        await store.put(
            _presented_spec(),
            title="Audit a claim",
            summary="One contested claim.",
            applicability="Use when.",
            provenance={"saved_by": "test"},
            presentation=WorkflowPresentation(
                actions=(RecordOwnerReviewAction(id="sign_off", label="Record the review", artifact_ids=("note", "transcript")),)
            ),
        )
    assert list((tmp_path / "workflows").glob("*.md")) == [], "a refused presentation leaves nothing on disk"


async def test_a_workflow_that_declares_no_result_cannot_carry_an_artifact_action(tmp_path: Path) -> None:
    store = FolderLibrary(tmp_path / "workflows")
    with pytest.raises(PresentationRefusedError, match="note"):
        await store.put(
            _spec(),
            title="Audit a claim",
            summary="One contested claim.",
            applicability="Use when.",
            provenance={"saved_by": "test"},
            presentation=WorkflowPresentation(actions=(SaveNoteAction(id="keep", label="Save the note", artifact_id="note"),)),
        )
    assert list((tmp_path / "workflows").glob("*.md")) == [], "a refused presentation leaves nothing on disk"


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param({"kind": "call_webhook", "id": "ping", "label": "Ping"}, id="an action kind nobody implements"),
        pytest.param({"kind": "save_note", "id": "  ", "label": "Save", "artifact_id": "note"}, id="a blank action id"),
        pytest.param(
            {"kind": "save_note", "id": "keep", "label": "Save", "artifact_id": "note", "destination": "webhook"}, id="a chosen destination"
        ),
        pytest.param(
            {"kind": "save_note", "id": "keep", "label": "Save", "artifact_id": "note", "endpoint": "https://example.test/x"}, id="an endpoint"
        ),
        pytest.param({"kind": "save_note", "id": "keep", "label": "Save", "artifact_id": "note", "method": "POST"}, id="a method"),
        pytest.param({"kind": "save_note", "id": "keep", "label": "Save", "artifact_id": "note", "path": "/hooks/note"}, id="a path"),
        pytest.param(
            {"kind": "save_note", "id": "keep", "label": "Save", "artifact_id": "note", "base_url": "https://example.test"}, id="a base url"
        ),
        pytest.param({"kind": "record_owner_review", "id": "sign_off", "label": "Sign off", "artifact_ids": []}, id="a review of nothing"),
    ],
)
def test_an_action_the_registry_does_not_offer_never_becomes_a_presentation(payload: dict[str, object]) -> None:
    """The closed action set is a parse-time fact, so no such presentation can reach a store at all."""
    with pytest.raises(ValidationError):
        WorkflowPresentation.model_validate({"actions": [payload]})


def test_an_action_keeps_the_id_it_was_checked_under() -> None:
    """Stripping happens once, on the way in, so the recorded id and the checked id are one string."""
    presentation = WorkflowPresentation.model_validate(
        {"actions": [{"kind": "save_note", "id": "  keep  ", "label": "Save the note", "artifact_id": "note"}]}
    )

    assert presentation.actions[0].id == "keep"


def test_two_actions_cannot_share_one_id() -> None:
    with pytest.raises(ValidationError, match="keep"):
        WorkflowPresentation.model_validate(
            {
                "actions": [
                    {"kind": "save_note", "id": "keep", "label": "Save the note", "artifact_id": "note"},
                    {"kind": "record_owner_review", "id": "  keep  ", "label": "Sign off", "artifact_ids": ["note"]},
                ]
            }
        )


@pytest.mark.parametrize("path", [pytest.param("", id="an empty path"), pytest.param("   ", id="a path of spaces")])
def test_a_result_field_with_no_path_is_not_a_result_field(path: str) -> None:
    with pytest.raises(ValidationError):
        WorkflowPresentation.model_validate({"result_fields": [{"label": "Sources weighed", "path": path}]})


def test_two_inputs_cannot_label_the_same_parameter() -> None:
    """One parameter is asked for once. Two labels for it would draw a form with two of the same field."""
    with pytest.raises(ValidationError, match="claim"):
        WorkflowPresentation.model_validate(
            {"inputs": [{"key": "claim", "label": "The claim"}, {"key": "claim", "label": "The claim again"}]}
        )


# -- the host's own bar ----------------------------------------------------------------

PLACEHOLDERS = frozenset({"tbd", "todo", "n/a", "(not stated)", "coming soon"})
"""The same five words a host in this repository refuses. Held here so the guard under test is a
real one and not a lambda that always answers None."""


def _described_or_refused(
    workflow: Workflow, *, title: str, summary: str, applicability: str, presentation: WorkflowPresentation | None
) -> str | None:
    """One host's bar: a title, a summary, and four sentences nobody left as a placeholder."""
    del applicability, workflow
    words = presentation if presentation is not None else WorkflowPresentation()
    missing = [
        name
        for name, value in (
            ("title", title),
            ("summary", summary),
            ("purpose", words.purpose),
            ("produces", words.produces),
            ("owner_participation", words.owner_participation),
            ("not_for", words.not_for),
        )
        if not value.strip() or " ".join(value.split()).casefold() in PLACEHOLDERS
    ]
    return None if not missing else "This workflow is not described yet: " + ", ".join(missing) + "."


@pytest.fixture
def guarded(tmp_path: Path) -> WorkflowContract[None]:
    """The same port, with a host that will not keep a workflow nobody described."""
    return WorkflowContract[None](
        source=folder_toolset(tmp_path / "workflows"),
        vocabulary=FOLDER,
        expose=(),
        registry=StepRegistry(perform=_perform),
        saved_by="test",
        save_guard=_described_or_refused,
    )


DESCRIBED = {
    "title": "Audit one contested claim",
    "summary": "Weighs one claim against the sources and says what would settle it.",
    "purpose": "Settle one contested claim.",
    "produces": "A note saying what would settle it.",
    "owner_participation": "You read the note and decide.",
    "not_for": "A broad multi-source audit.",
}


async def test_the_guard_refuses_a_save_nobody_described_and_writes_nothing(
    guarded: WorkflowContract[None], tmp_path: Path
) -> None:
    """The library's own bar reaches the agent's tool, so both save paths refuse the same workflow."""
    answer = await _call(guarded, SAVE_TOOL, spec_yaml=_yaml(_spec()), applicability="Use when one claim is contested. Not otherwise.")

    assert "REFUSED" in answer
    assert "purpose" in answer
    assert "produces" in answer
    assert "owner_participation" in answer
    assert "not_for" in answer
    assert not list((tmp_path / "workflows").glob("*.md")), "a refused save leaves no file behind"


async def test_the_guard_refuses_placeholder_words(guarded: WorkflowContract[None], tmp_path: Path) -> None:
    answer = await _call(
        guarded,
        SAVE_TOOL,
        spec_yaml=_yaml(_spec()),
        applicability="Use when one claim is contested. Not otherwise.",
        **{**DESCRIBED, "produces": "  Coming   Soon "},
    )

    assert "REFUSED" in answer
    assert "produces" in answer
    assert not list((tmp_path / "workflows").glob("*.md"))


async def test_the_guard_never_reads_a_title_this_port_invented(guarded: WorkflowContract[None], tmp_path: Path) -> None:
    """A blank title used to become the spec's own name, which would walk straight past the bar."""
    answer = await _call(
        guarded,
        SAVE_TOOL,
        spec_yaml=_yaml(_spec()),
        applicability="Use when one claim is contested. Not otherwise.",
        **{**DESCRIBED, "title": ""},
    )

    assert "REFUSED" in answer
    assert "title" in answer
    assert not list((tmp_path / "workflows").glob("*.md"))


async def test_a_described_save_goes_through_and_the_words_reach_the_store(
    guarded: WorkflowContract[None], tmp_path: Path
) -> None:
    answer = await _call(
        guarded, SAVE_TOOL, spec_yaml=_yaml(_spec()), applicability="Use when one claim is contested. Not otherwise.", **DESCRIBED
    )

    assert "saved as" in answer
    card = await FolderLibrary(tmp_path / "workflows").fetch_card("audit-a-claim@1")
    assert card is not None
    assert card.title == "Audit one contested claim"
    assert card.presentation is not None
    assert card.presentation.purpose == "Settle one contested claim."
    assert card.presentation.not_for == "A broad multi-source audit."


async def test_the_guard_refuses_an_adaptation_nobody_described(guarded: WorkflowContract[None], tmp_path: Path) -> None:
    """A child is a workflow like any other, so the same bar decides whether it may be kept."""
    await _call(guarded, SAVE_TOOL, spec_yaml=_yaml(_spec()), applicability="Use when one claim is contested. Not otherwise.", **DESCRIBED)

    answer = await _call(
        guarded,
        ADAPT_TOOL,
        workflow_id="audit-a-claim@1",
        ops=[{"op": "set_description", "to": "Now with the sources listed."}],
        applicability="Use when one claim is contested. Not otherwise.",
    )

    assert "REFUSED" in answer
    assert "purpose" in answer
    assert await FolderLibrary(tmp_path / "workflows").fetch_card("audit-a-claim@2") is None


async def test_a_described_adaptation_keeps_its_own_words(guarded: WorkflowContract[None], tmp_path: Path) -> None:
    await _call(guarded, SAVE_TOOL, spec_yaml=_yaml(_spec()), applicability="Use when one claim is contested. Not otherwise.", **DESCRIBED)

    answer = await _call(
        guarded,
        ADAPT_TOOL,
        workflow_id="audit-a-claim@1",
        ops=[{"op": "set_description", "to": "Now with the sources listed."}],
        applicability="Use when one claim is contested. Not otherwise.",
        **{**DESCRIBED, "produces": "A note listing every source weighed."},
    )

    assert "adapted" in answer
    card = await FolderLibrary(tmp_path / "workflows").fetch_card("audit-a-claim@2")
    assert card is not None
    assert card.presentation is not None
    assert card.presentation.produces == "A note listing every source weighed."


async def test_a_port_with_no_guard_still_derives_a_blank_card_field(port: WorkflowContract[None], tmp_path: Path) -> None:
    """A library with no bar of its own keeps the old convenience: the spec names and describes itself."""
    await _call(port, SAVE_TOOL, spec_yaml=_yaml(_spec()), applicability="Use when one claim is contested. Not otherwise.")

    card = await FolderLibrary(tmp_path / "workflows").fetch_card("audit-a-claim@1")
    assert card is not None
    assert card.title == "Audit a claim"
    assert card.summary == "Check one contested claim and report what would settle it."


async def test_the_contract_tools_are_still_the_same_four_with_a_guard(guarded: WorkflowContract[None]) -> None:
    toolset = guarded.get_toolset()
    assert toolset is not None
    ctx: RunContext[None] = RunContext(deps=None, model=TestModel(), usage=RunUsage())

    assert set(await toolset.get_tools(ctx)) == {SEARCH_TOOL, FETCH_TOOL, SAVE_TOOL, ADAPT_TOOL}


# -- rewriting the author's words on a saved card --------------------------------------


async def test_redescribe_writes_the_authors_words_and_a_later_read_carries_them(tmp_path: Path) -> None:
    """The card route's write: a workflow saved before anybody described it can be described later."""
    library = FolderLibrary(tmp_path / "workflows")
    workflow_id = await library.put(
        _spec(), title="Audit a claim", summary="Weighs a claim.", applicability="Use when contested.", provenance={"saved_by": "test"}
    )
    assert (await library.fetch_card(workflow_id)) is not None

    await library.redescribe(
        workflow_id,
        title="Audit one contested claim",
        summary="Weighs one claim against the sources.",
        applicability="Use when contested. Not for a survey.",
        presentation=WorkflowPresentation(
            purpose="Settle one contested claim.",
            produces="A note saying what would settle it.",
            owner_participation="You read the note and decide.",
            not_for="A broad multi-source audit.",
        ),
    )

    card = await library.fetch_card(workflow_id)
    assert card is not None
    assert card.title == "Audit one contested claim"
    assert card.presentation is not None
    assert card.presentation.purpose == "Settle one contested claim."
    assert card.presentation.not_for == "A broad multi-source audit."


async def test_redescribe_without_words_keeps_the_ones_already_kept(tmp_path: Path) -> None:
    """A caller rewriting a title must not silently drop the author's sentences."""
    library = FolderLibrary(tmp_path / "workflows")
    workflow_id = await library.put(
        _spec(),
        title="Audit a claim",
        summary="Weighs a claim.",
        applicability="Use when contested.",
        provenance={"saved_by": "test"},
        presentation=WorkflowPresentation(purpose="Settle one contested claim."),
    )

    await library.redescribe(workflow_id, title="A better title", summary="Weighs a claim.", applicability="Use when contested.")

    card = await library.fetch_card(workflow_id)
    assert card is not None
    assert card.title == "A better title"
    assert card.presentation is not None
    assert card.presentation.purpose == "Settle one contested claim."


async def test_redescribe_refuses_a_presentation_that_names_what_the_spec_does_not_declare(tmp_path: Path) -> None:
    library = FolderLibrary(tmp_path / "workflows")
    workflow_id = await library.put(
        _spec(), title="Audit a claim", summary="Weighs a claim.", applicability="Use when contested.", provenance={"saved_by": "test"}
    )

    with pytest.raises(PresentationRefusedError, match="ghost"):
        await library.redescribe(
            workflow_id,
            title="Audit a claim",
            summary="Weighs a claim.",
            applicability="Use when contested.",
            presentation=WorkflowPresentation(inputs=(InputPresentation(key="ghost", label="Nothing"),)),
        )

    card = await library.fetch_card(workflow_id)
    assert card is not None
    assert card.presentation is None, "a refused redescribe leaves the file as it was"


async def test_a_knowledge_base_selector_over_a_parameter_that_cannot_hold_a_name_is_refused(tmp_path: Path) -> None:
    """The selector says what the values mean. A number parameter can never hold what it means."""
    spec = _spec().model_copy(
        update={"parameters": {"claim": ParameterSpec(description="how many", json_schema={"type": "integer"})}}
    )
    library = FolderLibrary(tmp_path / "workflows")

    with pytest.raises(PresentationRefusedError, match="cannot hold a name"):
        await library.put(
            spec,
            title="Audit a claim",
            summary="Weighs a claim.",
            applicability="Use when contested.",
            provenance={"saved_by": "test"},
            presentation=WorkflowPresentation(inputs=(InputPresentation(key="claim", label="Knowledge bases", control="knowledge-base"),)),
        )

    assert list((tmp_path / "workflows").glob("*.md")) == [], "a refused presentation leaves nothing on disk"


@pytest.mark.parametrize(
    "schema",
    [
        pytest.param({"type": "string"}, id="one name"),
        pytest.param({"type": "array", "items": {"type": "string"}}, id="a list of names"),
        pytest.param({"enum": ["research", "repo-docs"]}, id="a closed set of names"),
        pytest.param({"description": "whatever the host reads"}, id="a schema that says nothing to judge"),
    ],
)
async def test_a_knowledge_base_selector_over_a_parameter_that_can_hold_a_name_is_kept(
    schema: dict[str, object], tmp_path: Path
) -> None:
    """The control passes what a parameter could answer, and judges nothing a schema does not say."""
    spec = _spec().model_copy(update={"parameters": {"claim": ParameterSpec(description="which", json_schema=schema)}})
    library = FolderLibrary(tmp_path / "workflows")

    workflow_id = await library.put(
        spec,
        title="Audit a claim",
        summary="Weighs a claim.",
        applicability="Use when contested.",
        provenance={"saved_by": "test"},
        presentation=WorkflowPresentation(inputs=(InputPresentation(key="claim", label="Knowledge bases", control="knowledge-base"),)),
    )

    card = await library.fetch_card(workflow_id)
    assert card is not None
    assert card.presentation is not None
    assert card.presentation.inputs[0].control == "knowledge-base"


# --- the intent a card carries -------------------------------------------------------------------
#
# What was asked for, kept beside the card and read back off every surface. A file is where the
# folder library keeps it, so these are the round trips a saved intent actually takes.


def _intent() -> WorkflowIntent:
    """One fully stated intent, with an incomplete field left deliberately empty."""
    return WorkflowIntent(
        request="Weigh every contested claim in the sources I keep",
        expected_result="A note naming what would settle the claim.",
        success_example="Last week's disputed figure came back with the two sources that disagree.",
        failure_behavior="A source that cannot be read is named and the rest are still weighed.",
        trigger="scheduled",
        assumptions=("Contested means two sources disagree.",),
    )


async def test_an_intent_comes_back_unchanged_from_every_folder_surface(tmp_path: Path) -> None:
    """Put it once, then read it off search, catalog, the card, redescribe, archive and restore."""
    store = FolderLibrary(tmp_path / "workflows")
    intent = _intent()
    workflow_id = await store.put(
        _spec(),
        title="Audit a claim",
        summary="One contested claim.",
        applicability="Use when one claim is contested.",
        provenance={"saved_by": "test"},
        intent=intent,
    )

    assert (await store.search("claim"))[0].intent == intent
    assert (await store.catalog())[0].intent == intent
    card = await store.fetch_card(workflow_id)
    assert card is not None
    assert card.intent == intent

    await store.redescribe(workflow_id, title="Audit one claim", summary="One claim.", applicability="Use when one claim is contested.")
    redescribed = await store.fetch_card(workflow_id)
    assert redescribed is not None
    assert redescribed.intent == intent, "redescribe rewrites prose, never what was asked for"

    await store.set_archived(workflow_id, archived=True)
    assert (await store.archived())[0].intent == intent
    await store.set_archived(workflow_id, archived=False)
    restored = await store.fetch_card(workflow_id)
    assert restored is not None
    assert restored.intent == intent


async def test_a_request_only_intent_is_kept_exactly_as_incomplete_as_it_was_stated(tmp_path: Path) -> None:
    """Drafting with half an intent is normal, and nothing fills the other half in on the way to disk."""
    store = FolderLibrary(tmp_path / "workflows")
    workflow_id = await store.put(
        _spec(),
        title="Audit a claim",
        summary="One contested claim.",
        applicability="Use when one claim is contested.",
        provenance={"saved_by": "test"},
        intent=WorkflowIntent(request="Weigh every contested claim"),
    )

    card = await store.fetch_card(workflow_id)
    assert card is not None
    assert card.intent == WorkflowIntent(request="Weigh every contested claim")
    kept = card.intent
    assert kept is not None
    assert intent_issues(kept) == ("expected_result", "success_example", "empty_input_behavior", "failure_behavior")


async def test_a_file_written_without_an_intent_reads_as_none_and_carries_no_key(tmp_path: Path) -> None:
    """A file that predates intents holds what it always held, and nothing infers one from the spec."""
    store = FolderLibrary(tmp_path / "workflows")
    workflow_id = await store.put(
        _spec(),
        title="Audit a claim",
        summary="One contested claim.",
        applicability="Use when one claim is contested.",
        provenance={"saved_by": "test"},
    )

    card = await store.fetch_card(workflow_id)
    assert card is not None
    assert card.intent is None
    assert "intent" not in (tmp_path / "workflows" / "audit-a-claim-v1.md").read_text(encoding="utf-8")


async def test_a_hand_edited_intent_that_no_longer_parses_reads_as_absent(tmp_path: Path) -> None:
    """A file is hand-editable, so an unreadable intent is one card without one, never a broken listing."""
    store = FolderLibrary(tmp_path / "workflows")
    workflow_id = await store.put(
        _spec(),
        title="Audit a claim",
        summary="One contested claim.",
        applicability="Use when one claim is contested.",
        provenance={"saved_by": "test"},
        intent=_intent(),
    )
    file = tmp_path / "workflows" / "audit-a-claim-v1.md"
    file.write_text(file.read_text(encoding="utf-8").replace("request: Weigh every contested claim", "trigger: whenever"), encoding="utf-8")

    card = await store.fetch_card(workflow_id)
    assert card is not None
    assert card.intent is None


async def test_the_contracts_write_shape_carries_an_intent_all_the_way_into_the_file(tmp_path: Path) -> None:
    """A host that holds an intent keeps one: the write's own slot reaches this host's write tool."""
    root = tmp_path / "workflows"
    port = WorkflowContract[None](
        source=folder_toolset(root), vocabulary=FOLDER, expose=("write_file",), registry=StepRegistry(perform=_perform), saved_by="test"
    )
    intent = _intent()
    write = WorkflowWrite(
        workflow=_spec(), title="Audit a claim", summary="One claim.", applicability="Use when contested.", saved_by="test", intent=intent
    )

    put_args = FOLDER.put_args
    assert put_args is not None
    arguments = put_args(write)
    answer = await _call(port, "write_file", **arguments)

    assert arguments["asked_for"] == intent.model_dump(mode="json")
    card = await FolderLibrary(root).fetch_card(str(answer["wrote"]))
    assert card is not None
    assert card.intent == intent


async def test_a_write_carrying_no_intent_puts_no_intent_in_the_file(tmp_path: Path) -> None:
    """The slot is empty when nobody stated one, and an empty slot writes nothing."""
    root = tmp_path / "workflows"
    port = WorkflowContract[None](
        source=folder_toolset(root), vocabulary=FOLDER, expose=("write_file",), registry=StepRegistry(perform=_perform), saved_by="test"
    )
    write = WorkflowWrite(workflow=_spec(), title="Audit a claim", summary="One claim.", applicability="Use when contested.", saved_by="test")

    put_args = FOLDER.put_args
    assert put_args is not None
    answer = await _call(port, "write_file", **put_args(write))

    card = await FolderLibrary(root).fetch_card(str(answer["wrote"]))
    assert card is not None
    assert card.intent is None
    assert "intent" not in (root / "audit-a-claim-v1.md").read_text(encoding="utf-8")
