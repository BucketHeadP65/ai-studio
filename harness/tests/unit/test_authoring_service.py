"""Saving the draft is a use case with rules, not an HTTP handler's inline hobby.

The rules: a draft must exist, parse as a spec, and validate against this run's steps --
and a refusal must leave nothing behind. Tested against `FolderLibrary`, a real library over a
directory, because the use case should not care which backend answers.
"""

from __future__ import annotations

import json

from typing import TYPE_CHECKING

import pytest
import yaml

from core.ai.workflows.contracts import (
    AdaptationRefusedError,
    Briefing,
    FromNode,
    FromParameter,
    JudgeGate,
    LibraryError,
    Lineage,
    MapNode,
    ParameterSpec,
    RunNode,
    Shape,
    StepRegistry,
    Workflow,
    WorkflowContract,
    WorkflowFingerprint,
    WorkflowIntent,
    WorkflowPresentation,
    intent_issues,
    load_spec_document,
    prepare,
    referenced_steps,
)
from core.ai.workflows.domain.vocabulary import SameAs
from core.ai.workflows.infrastructure.folder import FOLDER, FolderLibrary, folder_toolset
from pydantic_ai import Agent, RunContext
from pydantic_ai.messages import ModelResponse, TextPart
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.models.test import TestModel
from pydantic_ai.usage import RunUsage

from harness.domain.readiness import (
    PLACEHOLDERS,
    PROSE_LIMIT,
    DraftReadiness,
    WorkflowPresentationPatch,
    effective_presentation,
    placeholder_key,
)
from harness.features.authoring.domain import Draft
from harness.features.authoring.exceptions import DraftNotSavableError
from harness.features.authoring.process import WorkflowProcess, check_draft, draft_fit, step_inventory
from harness.features.authoring.service import adapt_saved, draft_payload, draft_readiness, save_draft, settled_draft
from harness.features.running.spend_report import SpendReporter
from harness.features.scheduling.contracts import Scheduler
from harness.session import Conversation, NoSessionLog, Runtime, SessionPool, remembered_draft
from harness.settings import HarnessSettings, RunSettings, SelfAuthoringSettings
from tests.conftest import CallLog, register_quarterly_steps
from tests.support.authoring import a_controller, one_session
from tests.support.fakes.broadcaster import FakeBroadcaster
from tests.support.fakes.schedules import FakeSchedules
from tests.support.fakes.sessions import APool
from tests.support.fakes.telemetry import FakeTelemetry
from tests.support.state import records_under

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from pathlib import Path

    from pydantic_ai.messages import ModelMessage
    from pydantic_ai.models.function import AgentInfo

from tests.support.state import a_state


def _draft(spec: Workflow) -> Draft:
    return Draft(
        spec_yaml=yaml.safe_dump(spec.model_dump(mode="json"), sort_keys=False),
        title="Quarterly report",
        summary="Close the quarter",
        applicability="Use for quarterly closes. Not for ad-hoc reports.",
        presentation=WorkflowPresentation(
            purpose="Close one quarter's books.",
            produces="The published quarterly report.",
            owner_participation="Approve the draft before it is published.",
            not_for="A question about a single month.",
        ),
    )


async def test_a_valid_draft_saves_into_the_store(tmp_path: Path, quarterly_report: Workflow, registry: StepRegistry) -> None:
    library = FolderLibrary(tmp_path / "workflows")
    workflow_id = await save_draft(_draft(quarterly_report), library=library, registry=registry, self_authoring=SelfAuthoringSettings(custom_root=tmp_path / "custom"))
    assert await library.fetch_workflow(workflow_id) == quarterly_report


async def test_a_save_is_attributed_to_the_name_the_owners_section_carries(tmp_path: Path, quarterly_report: Workflow, registry: StepRegistry) -> None:
    """Who a save is by is this deployment's to say, so no person's name is written into the code."""
    library = _RecordingStore(tmp_path / "workflows")
    surface = SelfAuthoringSettings(custom_root=tmp_path / "custom", saved_by="human:someone")

    await save_draft(_draft(quarterly_report), library=library, registry=registry, self_authoring=surface)

    assert library.saved_with["saved_by"] == "human:someone"


async def test_a_save_with_no_name_exported_carries_the_committed_default(tmp_path: Path, quarterly_report: Workflow, registry: StepRegistry) -> None:
    """The section's own default answers, so a deployment that names nobody still records a saver."""
    library = _RecordingStore(tmp_path / "workflows")

    await save_draft(_draft(quarterly_report), library=library, registry=registry, self_authoring=SelfAuthoringSettings(custom_root=tmp_path / "custom"))

    assert library.saved_with["saved_by"] == HarnessSettings().self_authoring.saved_by


async def test_an_empty_draft_is_refused(tmp_path: Path, registry: StepRegistry) -> None:
    with pytest.raises(DraftNotSavableError, match="no draft"):
        await save_draft(Draft(), library=FolderLibrary(tmp_path / "w"), registry=registry, self_authoring=SelfAuthoringSettings(custom_root=tmp_path / "custom"))


async def test_a_draft_that_is_not_a_spec_is_refused(tmp_path: Path, registry: StepRegistry) -> None:
    with pytest.raises(DraftNotSavableError):
        await save_draft(Draft(spec_yaml="nodes: what"), library=FolderLibrary(tmp_path / "w"), registry=registry, self_authoring=SelfAuthoringSettings(custom_root=tmp_path / "custom"))


async def test_a_draft_this_run_cannot_execute_writes_nothing(tmp_path: Path, quarterly_report: Workflow, call_log: CallLog) -> None:
    thin = StepRegistry()
    register_quarterly_steps(thin, call_log, exclude=frozenset({"publish"}))
    root = tmp_path / "workflows"
    with pytest.raises(DraftNotSavableError, match="publish"):
        await save_draft(_draft(quarterly_report), library=FolderLibrary(root), registry=thin, self_authoring=SelfAuthoringSettings(custom_root=tmp_path / "custom"))
    assert not list(root.glob("*.md")), "a refused save must leave no file behind"


class _RecordingStore(FolderLibrary):
    """A real folder library that also keeps the provenance every write was handed."""

    saved_with: Mapping[str, str] = {}

    async def put(
        self,
        workflow: Workflow,
        *,
        title: str,
        summary: str,
        applicability: str,
        provenance: Mapping[str, str],
        fingerprint: WorkflowFingerprint | None = None,
        lineage: Lineage | None = None,
        presentation: WorkflowPresentation | None = None,
        intent: WorkflowIntent | None = None,
    ) -> str:
        self.saved_with = provenance
        return await super().put(
            workflow,
            title=title,
            summary=summary,
            applicability=applicability,
            provenance=provenance,
            fingerprint=fingerprint,
            lineage=lineage,
            presentation=presentation,
            intent=intent,
        )


class _RefusingStore(FolderLibrary):
    """A library whose write refuses -- the shape `LibraryCollection` takes when re-minting a superseded version."""

    async def put(self, *args: object, **kwargs: object) -> str:
        del args, kwargs
        msg = "'wf.x.v1' is already superseded by a later version. Fetch the newest version of 'x' and adapt that instead."
        raise LibraryError(msg)


async def test_a_store_refusal_is_the_same_answer_as_any_other_unpackable_draft(
    tmp_path: Path, quarterly_report: Workflow, registry: StepRegistry
) -> None:
    """The library can refuse a write the validator cannot see; the person still hears "not saved, and why".

    Untranslated, the `LibraryError` left `save_draft` as a raw exception and the route's JSON
    contract answered 500 -- a refusal with a reason in it, delivered as a crash without one.
    """
    with pytest.raises(DraftNotSavableError, match="superseded"):
        await save_draft(_draft(quarterly_report), library=_RefusingStore(tmp_path / "w"), registry=registry, self_authoring=SelfAuthoringSettings(custom_root=tmp_path / "custom"))


def test_a_checked_spec_settles_with_its_card(quarterly_report: Workflow) -> None:
    """A passing check settles a draft carrying the re-serialised spec and the given card."""
    kept = settled_draft(quarterly_report, title="Quarterly", summary="s", applicability="a", existing=Draft())
    assert yaml.safe_load(kept.spec_yaml)["name"] == quarterly_report.name
    assert kept.title == "Quarterly"
    assert kept.summary == "s"
    assert kept.applicability == "a"


def test_blank_card_fields_keep_the_existing_card(quarterly_report: Workflow) -> None:
    """A turn that answers with only the checker's verdict must not erase the card."""
    settled = settled_draft(
        quarterly_report,
        title="",
        summary="",
        applicability="",
        existing=Draft(spec_yaml="old", title="Quarterly report", summary="Close the quarter", applicability="Not ad hoc."),
    )
    assert settled.title == "Quarterly report"
    assert settled.summary == "Close the quarter"
    assert settled.applicability == "Not ad hoc."
    assert yaml.safe_load(settled.spec_yaml)["name"] == quarterly_report.name, "the spec still comes from the checked object"


def test_given_card_fields_win_over_the_existing_ones(quarterly_report: Workflow) -> None:
    """Rewriting the card is a normal authoring act; only a blank field means keep."""
    settled = settled_draft(
        quarterly_report,
        title="Quarter close",
        summary="Now with commentary",
        applicability="",
        existing=Draft(title="Quarterly report", summary="Close the quarter", applicability="Not ad hoc."),
    )
    assert settled.title == "Quarter close"
    assert settled.summary == "Now with commentary"
    assert settled.applicability == "Not ad hoc.", "one field given does not blank the others"


def _kept_path(tmp_path: Path) -> Path:
    """Where a session built by `_ctx` keeps its proposal, never the checkout's own `draft.json`."""
    return tmp_path / "draft.json"


def _ctx(tmp_path: Path, registry: StepRegistry, *, draft: Draft | None = None) -> RunContext[Runtime]:
    """A run context whose session holds `registry`, `draft` and the file the settled draft is kept in."""
    session = Runtime(
        registry=registry,
        library=FolderLibrary(tmp_path / "workflows"),
        agent=Agent(TestModel(), deps_type=Runtime),
        conversation=Conversation(draft=draft or Draft()),
        draft_file=_kept_path(tmp_path),
        session_log=NoSessionLog(),
        state=a_state(tmp_path),
    )
    return RunContext(deps=session, model=TestModel(), usage=RunUsage())


async def test_a_passing_check_settles_the_draft_with_the_given_card(
    tmp_path: Path, quarterly_report: Workflow, registry: StepRegistry
) -> None:
    ctx = _ctx(tmp_path, registry)
    spec_yaml = yaml.safe_dump(quarterly_report.model_dump(mode="json"), sort_keys=False)

    answer = await check_draft(ctx, spec_yaml, title="Quarterly report", summary="Close the quarter", applicability="Not ad hoc.")

    draft = ctx.deps.conversation.draft
    assert yaml.safe_load(draft.spec_yaml)["name"] == quarterly_report.name
    assert draft.title == "Quarterly report"
    assert draft.summary == "Close the quarter"
    assert draft.applicability == "Not ad hoc."
    assert str(len(quarterly_report.nodes)) in answer
    assert "Quarterly report" in answer


async def test_a_passing_check_with_no_card_keeps_the_existing_one(
    tmp_path: Path, quarterly_report: Workflow, registry: StepRegistry
) -> None:
    existing = Draft(spec_yaml="old", title="Quarterly report", summary="Close the quarter", applicability="Not ad hoc.")
    ctx = _ctx(tmp_path, registry, draft=existing)
    spec_yaml = yaml.safe_dump(quarterly_report.model_dump(mode="json"), sort_keys=False)

    await check_draft(ctx, spec_yaml)

    draft = ctx.deps.conversation.draft
    assert draft.title == "Quarterly report"
    assert draft.summary == "Close the quarter"
    assert draft.applicability == "Not ad hoc."
    assert yaml.safe_load(draft.spec_yaml)["name"] == quarterly_report.name


async def test_a_failing_check_leaves_the_draft_untouched(tmp_path: Path, quarterly_report: Workflow, call_log: CallLog) -> None:
    thin = StepRegistry()
    register_quarterly_steps(thin, call_log, exclude=frozenset({"publish"}))
    existing = Draft(spec_yaml="name: kept\nversion: 1\n", title="Kept")
    ctx = _ctx(tmp_path, thin, draft=existing)
    spec_yaml = yaml.safe_dump(quarterly_report.model_dump(mode="json"), sort_keys=False)

    answer = await check_draft(ctx, spec_yaml, title="Ignored, the check failed")

    assert ctx.deps.conversation.draft == existing, "a failing check must not touch the draft"
    assert "publish" in answer


async def test_a_passing_check_keeps_the_settled_draft_on_disk(
    tmp_path: Path, quarterly_report: Workflow, registry: StepRegistry
) -> None:
    """The disk copy follows the in-memory one on the spot, so a turn that never settles still steps the proposal."""
    ctx = _ctx(tmp_path, registry)
    spec_yaml = yaml.safe_dump(quarterly_report.model_dump(mode="json"), sort_keys=False)

    await check_draft(ctx, spec_yaml, title="Quarterly report", summary="Close the quarter", applicability="Not ad hoc.")

    assert remembered_draft(path=_kept_path(tmp_path)) == ctx.deps.conversation.draft


async def test_a_failing_check_keeps_nothing_on_disk(tmp_path: Path, quarterly_report: Workflow, call_log: CallLog) -> None:
    """Only a spec that holds reaches the file the next boot restores from."""
    thin = StepRegistry()
    register_quarterly_steps(thin, call_log, exclude=frozenset({"publish"}))
    ctx = _ctx(tmp_path, thin)
    spec_yaml = yaml.safe_dump(quarterly_report.model_dump(mode="json"), sort_keys=False)

    await check_draft(ctx, spec_yaml, title="Ignored, the check failed")

    assert remembered_draft(path=_kept_path(tmp_path)) is None


async def test_an_adaptation_of_a_stored_artifact_becomes_the_draft(
    tmp_path: Path, quarterly_report: Workflow, registry: StepRegistry
) -> None:
    """Adaptation was built, tested, and reachable from nowhere.

    `adapt` and `carried_trust` had no caller outside the test suite, which meant the only way
    to change a saved workflow in this app was to have the agent write a fresh spec -- a rewrite,
    which is exactly what the typed diff exists to avoid. A rewrite discards the node-granular
    trust the ledger earned, because nothing records which nodes were left alone.
    """
    library = FolderLibrary(tmp_path / "workflows")
    parent_id = await save_draft(_draft(quarterly_report), library=library, registry=registry, self_authoring=SelfAuthoringSettings(custom_root=tmp_path / "custom"))

    draft, lineage = await adapt_saved(parent_id, ({"op": "set_description", "to": "Now with commentary."},), library=library)

    assert yaml.safe_load(draft.spec_yaml)["description"] == "Now with commentary."
    assert yaml.safe_load(draft.spec_yaml)["version"] == quarterly_report.version + 1
    assert lineage.parent_name == quarterly_report.name
    assert lineage.touched == ()
    assert await library.fetch_workflow(parent_id) == quarterly_report, "the incumbent is untouched"


async def test_an_adaptation_inherits_the_words_a_chooser_reads(
    tmp_path: Path, quarterly_report: Workflow, registry: StepRegistry
) -> None:
    """The child of a described artifact must not land in the library described as nothing.

    A diff edits the *spec*; title, summary and applicability live in the library's card, so
    adapting used to hand back a draft with all three blank -- and `save_draft`'s fallbacks turned
    that into a card titled with the slug, summarised by the spec's description, and applicable
    "(not stated)". Applicability is the field the library ranks on and the one a chooser actually
    decides by, so the child of a carefully described artifact became unfindable and, once found,
    unjudgeable.

    Same ruling `settled_draft` makes about the model's silence, one function down: absent means
    unchanged, not blank. These are the most expensive words in an artifact and a diff that
    changed one node is no reason to retype them.
    """
    library = FolderLibrary(tmp_path / "workflows")
    parent_id = await save_draft(_draft(quarterly_report), library=library, registry=registry, self_authoring=SelfAuthoringSettings(custom_root=tmp_path / "custom"))

    draft, lineage = await adapt_saved(parent_id, ({"op": "set_description", "to": "Now with commentary."},), library=library)

    assert draft.title == "Quarterly report"
    assert draft.summary == "Close the quarter"
    assert draft.applicability == "Use for quarterly closes. Not for ad-hoc reports."

    child_id = await save_draft(draft, library=library, registry=registry, self_authoring=SelfAuthoringSettings(custom_root=tmp_path / "custom"), lineage=lineage)
    ((found,)) = [card for card in await library.search("quarterly closes ad-hoc") if card.id == child_id]
    assert found.applicability == "Use for quarterly closes. Not for ad-hoc reports.", "and it survives the round trip"


async def test_saving_an_adaptation_keeps_the_diff_that_made_it(
    tmp_path: Path, quarterly_report: Workflow, registry: StepRegistry
) -> None:
    """Supersession is a link. A child saved without its lineage is just an unrelated artifact."""
    library = FolderLibrary(tmp_path / "workflows")
    parent_id = await save_draft(_draft(quarterly_report), library=library, registry=registry, self_authoring=SelfAuthoringSettings(custom_root=tmp_path / "custom"))
    draft, lineage = await adapt_saved(parent_id, ({"op": "set_description", "to": "Now with commentary."},), library=library)

    child_id = await save_draft(draft, library=library, registry=registry, self_authoring=SelfAuthoringSettings(custom_root=tmp_path / "custom"), lineage=lineage)

    assert child_id != parent_id
    kept = await library.fetch_lineage(child_id)
    assert kept is not None
    assert kept.parent_version == quarterly_report.version
    assert await library.fetch_lineage(parent_id) is None


async def test_an_incoherent_adaptation_is_refused_before_anything_is_written(
    tmp_path: Path, quarterly_report: Workflow, registry: StepRegistry
) -> None:
    """Valid by construction: an op naming a node that is not there never reaches the library."""
    library = FolderLibrary(tmp_path / "workflows")
    parent_id = await save_draft(_draft(quarterly_report), library=library, registry=registry, self_authoring=SelfAuthoringSettings(custom_root=tmp_path / "custom"))

    with pytest.raises(AdaptationRefusedError, match="ghost"):
        await adapt_saved(parent_id, ({"op": "remove_node", "id": "ghost"},), library=library)


async def test_saving_the_same_adaptation_twice_keeps_it_an_adaptation(
    tmp_path: Path, quarterly_report: Workflow, registry: StepRegistry
) -> None:
    """The second save used to library the same child stripped of its parent, and say nothing.

    Found live. A save whose supersession link failed on a flaky connection raised past the
    route, so `conversation.lineage = None` never ran; the retry saved *with* the lineage and
    spent it; a third press saved the same draft *without* one and upserted the child's page,
    erasing the link. The artifact stopped being anybody's child and the library had no way to know
    it ever had been.

    The token was the mistake, not the clearing of it. A draft that came from an adaptation IS an
    adaptation -- that is a fact about the draft, not a one-shot side-channel on the conversation
    -- so it rides on the draft, survives being saved twice, and (because the draft is
    persisted) survives a restart, which it also did not do before.
    """
    library = FolderLibrary(tmp_path / "workflows")
    parent_id = await save_draft(_draft(quarterly_report), library=library, registry=registry, self_authoring=SelfAuthoringSettings(custom_root=tmp_path / "custom"))
    draft, _ = await adapt_saved(parent_id, ({"op": "set_description", "to": "Now with commentary."},), library=library)

    first = await save_draft(draft, library=library, registry=registry, self_authoring=SelfAuthoringSettings(custom_root=tmp_path / "custom"))
    second = await save_draft(draft, library=library, registry=registry, self_authoring=SelfAuthoringSettings(custom_root=tmp_path / "custom"))

    assert first == second, "the same draft saves to the same artifact"
    kept = await library.fetch_lineage(second)
    assert kept is not None, "saving twice must not orphan the child"
    assert kept.parent_version == quarterly_report.version


async def test_editing_an_adaptation_before_saving_widens_what_it_admits_to_touching(
    tmp_path: Path, quarterly_report: Workflow, registry: StepRegistry
) -> None:
    """Between adapting and saving, the child is a draft -- and drafts get edited.

    `touched` is what the author's ops named, which is exactly right when `adapt` returns and can
    be wrong by the time anybody presses Save. Every node the ops never named is claimed as
    carried, so a node edited afterwards keeps evidence from a run of a different node. That is
    the same laundering the version-scoped tier fixed, at node granularity.

    So saving re-derives it against the stored parent and unions the two. Union, not replace: the
    ops still say what was asked for, and the answer can only widen, which can only withdraw
    trust.
    """
    library = FolderLibrary(tmp_path / "workflows")
    parent_id = await save_draft(_draft(quarterly_report), library=library, registry=registry, self_authoring=SelfAuthoringSettings(custom_root=tmp_path / "custom"))
    draft, lineage = await adapt_saved(parent_id, ({"op": "set_description", "to": "Now with commentary."},), library=library)
    assert lineage.touched == (), "the diff moved no node"

    spec = yaml.safe_load(draft.spec_yaml)
    for node in spec["nodes"]:
        if node["id"] == "approve":
            node["rejected_to"] = "reconcile"  # rewind further on rejection: valid, and not what the ops said
    child_id = await save_draft(draft.model_copy(update={"spec_yaml": yaml.safe_dump(spec)}), library=library, registry=registry, self_authoring=SelfAuthoringSettings(custom_root=tmp_path / "custom"))

    kept = await library.fetch_lineage(child_id)
    assert kept is not None
    assert kept.touched == ("approve",), "the node the edit moved is no longer claimed as carried"
    assert kept.ops == lineage.ops, "and the ops still say what the author actually asked for"


ITEM_SCHEMA = {"type": "object", "properties": {"title": {"type": "string"}}, "required": ["title"]}
NOTE_SCHEMA = {"type": "object", "properties": {"note": {"type": "string"}}, "required": ["note"]}
TEXT_SCHEMA = {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}
VERDICT_SCHEMA = {"type": "object", "properties": {"approved": {"type": "boolean"}}, "required": ["approved"]}

TRIAGE_BRIEF = "Read the item and write one short triage note about it, and nothing else at all."


async def _never_performed(task: Briefing, output_type: type, /, **inputs: object) -> object:
    """A briefing this test builds and never runs -- an inventory is read, not executed."""
    del task, output_type, inputs
    msg = "the inventory builds steps, it never performs them"
    raise AssertionError(msg)


def _live_registry() -> StepRegistry:
    """What one run can execute: the app's own built-in step, and one the owner activated."""
    registry = StepRegistry(perform=_never_performed)

    async def notify_owner(collection: str, written: int) -> str:
        del collection
        return f"{written} written"

    async def tally_votes(rows: list[str]) -> int:
        return len(rows)

    registry.register(notify_owner)
    registry.register(tally_votes)
    return registry


def _inventory_spec(*, unbuildable: bool = False, unwired: bool = False) -> Workflow:
    """A draft touching every kind of work a run can name: mapped turns, a judge, and both registered steps.

    `unbuildable` points one node at a name nothing here can build; `unwired` leaves a required
    input of a name that does build unwired, which validates badly while every name still resolves.
    """
    triage = Briefing(name="triage", instructions=TRIAGE_BRIEF, inputs={"item": Shape(json_schema=ITEM_SCHEMA)}, output=Shape(json_schema=NOTE_SCHEMA))
    polish = Briefing(
        name="polish",
        instructions="Polish the triage note into the final short text, ready for the owner to read.",
        inputs={"note": SameAs(like="triage"), "context": Shape(json_schema={"type": "string"})},
        output=Shape(json_schema=TEXT_SCHEMA),
    )
    verdict = Briefing(
        name="verdict",
        instructions="Say whether the tally is high enough to tell the owner about, and nothing more.",
        inputs={"count": Shape(json_schema={"type": "integer"})},
        output=Shape(json_schema=VERDICT_SCHEMA),
    )
    return Workflow(
        name="inventory-check",
        version=1,
        description="One tally a judge decides on, a notification, and one mapped sequence over the items.",
        parameters={
            "items": ParameterSpec(description="items to digest", json_schema={"type": "array", "items": ITEM_SCHEMA}),
            "rows": ParameterSpec(description="rows to tally", json_schema={"type": "array", "items": {"type": "string"}}),
            "collection": ParameterSpec(description="where the pages landed", json_schema={"type": "string"}),
        },
        inline=(triage, polish, verdict),
        nodes=(
            RunNode(
                id="tally",
                work="no_such_step" if unbuildable else "tally_votes",
                inputs={} if unwired else {"rows": FromParameter(parameter="rows")},
            ),
            JudgeGate(id="review", work="verdict", reviews=FromNode(node="tally"), rejected_to="tally", approved_when="approved"),
            RunNode(
                id="tell",
                work="notify_owner",
                inputs={"collection": FromParameter(parameter="collection"), "written": FromNode(node="review")},
            ),
            MapNode(
                id="each",
                turns=("triage", "polish"),
                over=FromParameter(parameter="items"),
                each="item",
                inputs={"context": FromNode(node="tell")},
            ),
        ),
        output=FromNode(node="each"),
    )


RUNNABLE_CLAIM = "Every step it names resolves here, and its wiring checked clean."


def _claims_it_runs(text: str) -> bool:
    """Whether the inventory tells the agent this draft is ready to run, however it phrases it."""
    return RUNNABLE_CLAIM in text or "It runs" in text


async def _checked(tmp_path: Path, registry: StepRegistry, spec: Workflow) -> tuple[str, str]:
    """What `check_draft` answered about `spec`, and the standing inventory beside it.

    The two halves the agent reads: how this draft stands comes back with the check that read it,
    and what the run can build stands in the instructions whatever the draft is doing.
    """
    ctx = _ctx(tmp_path, registry)
    answer = await check_draft(ctx, yaml.safe_dump(spec.model_dump(mode="json"), sort_keys=False))
    return answer, step_inventory(ctx)


async def test_the_process_voice_reads_the_steps_this_run_can_actually_execute(tmp_path: Path) -> None:
    """The inventory is the prepared registry, not a standing claim about it."""
    registry = _live_registry()
    spec = _inventory_spec()
    prepared = prepare(spec, registry)
    assert prepared.report.ok, prepared.report.issues
    live = [name for name in referenced_steps(spec) if name in prepared.registry]
    missing = [name for name in referenced_steps(spec) if name not in prepared.registry]
    assert live == ["tally_votes", "verdict", "notify_owner", "triage", "polish"]
    assert missing == []

    answer, inventory = await _checked(tmp_path, registry, spec)

    assert "notify_owner, tally_votes" in inventory, "the built-in step and the owner's active one are both nameable"
    assert "verdict, triage, polish" in answer, "the steps the draft brings are marked as its own"
    assert "tally_votes, notify_owner" in answer, "and the registered ones it names are marked as this run's"
    assert RUNNABLE_CLAIM in answer, "every name resolves and the wiring checked clean, so the claim is true"


async def test_a_draft_naming_a_step_nothing_can_build_is_never_called_runnable(tmp_path: Path) -> None:
    """A name this run cannot build is the whole reason the claim must be withheld."""
    registry = _live_registry()
    spec = _inventory_spec(unbuildable=True)
    prepared = prepare(spec, registry)
    assert [name for name in referenced_steps(spec) if name not in prepared.registry] == ["no_such_step"]

    answer, _inventory = await _checked(tmp_path, registry, spec)

    assert "Nothing here can build: no_such_step" in answer
    assert not _claims_it_runs(answer), "a draft missing a step does not run, whatever else resolves"


async def test_a_draft_whose_names_resolve_but_whose_wiring_fails_is_never_called_runnable(tmp_path: Path) -> None:
    """Every name resolving says nothing about the wiring, and the validator is the only authority on that."""
    registry = _live_registry()
    spec = _inventory_spec(unwired=True)
    prepared = prepare(spec, registry)
    assert [name for name in referenced_steps(spec) if name not in prepared.registry] == [], "every name this draft uses resolves"
    assert not prepared.report.ok, "and the wiring still does not check"

    answer, _inventory = await _checked(tmp_path, registry, spec)

    assert "Nothing here can build:" not in answer, "nothing is missing, so nothing is reported missing"
    assert not _claims_it_runs(answer), "a draft that does not check does not run"


def test_the_process_voice_never_claims_this_run_registers_nothing(tmp_path: Path) -> None:
    """A run holding registered steps must never be described as holding none, draft or no draft."""
    ctx = _ctx(tmp_path, _live_registry())

    text = step_inventory(ctx)

    for claim in ("registers no steps", "no steps are registered", "registry is empty"):
        assert claim not in text
    assert "notify_owner" in text, "with no draft checked yet, the registered steps are still named"
    assert not _claims_it_runs(text), "with no draft at all there is nothing to call runnable"


async def test_the_inventory_names_steps_and_never_pastes_their_briefs(tmp_path: Path) -> None:
    """A brief per step would cost the whole library on every request, and drift from the spec besides."""
    spec, registry = _inventory_spec(unbuildable=True), _live_registry()
    answer, inventory = await _checked(tmp_path, registry, spec)
    fit = draft_fit(spec, registry)

    assert TRIAGE_BRIEF not in answer
    assert TRIAGE_BRIEF not in inventory
    assert len(fit.splitlines()) <= 3, "three lines at most, whatever the draft holds"
    names = [name for line in fit.splitlines() for name in ("triage", "polish", "verdict", "tally_votes", "notify_owner") if name in line]
    assert len(names) == len(set(names)), "each step the draft uses is named once, not once per list"


async def test_the_instruction_block_is_byte_identical_while_the_registry_holds_still(tmp_path: Path, quarterly_report: Workflow) -> None:
    """Instructions render in the first request part, so a block that moves with the draft costs the prefix cache.

    Two real requests through the capability's own mounted instructions, with a `check_draft`
    between them that changes what this session's draft is. The registry is untouched across the
    two, so the block must arrive byte for byte the same.
    """
    registry = _live_registry()
    ctx = _ctx(tmp_path, registry)
    process = WorkflowProcess(
        port=_contract_port(tmp_path),
        records=records_under(tmp_path),
        reporter=SpendReporter(FakeTelemetry()),
        scheduler=Scheduler(FakeSchedules(), FakeSchedules(), FakeBroadcaster()),
        sessions=APool(SessionPool(ctx.deps)),
        topics=FakeBroadcaster(),
        run=RunSettings(),
    )
    rendered: list[str | None] = []

    async def answer(messages: Sequence[ModelMessage], info: AgentInfo) -> ModelResponse:
        del messages
        rendered.append(info.instructions)
        return ModelResponse(parts=[TextPart("ok")])

    agent = Agent(FunctionModel(answer), deps_type=Runtime, capabilities=[process])

    await agent.run("what can this run build?", deps=ctx.deps)
    await check_draft(ctx, yaml.safe_dump(_inventory_spec().model_dump(mode="json"), sort_keys=False))
    await agent.run("and now?", deps=ctx.deps)

    assert len(rendered) == 2
    assert rendered[0] is not None
    assert rendered[0] == rendered[1], "the draft changed and the registry did not, so the standing instructions must not move"
    assert "notify_owner, tally_votes" in rendered[0], "what the run can build is still there -- it is the draft-shaped half that left"


def _contract_port(tmp_path: Path) -> WorkflowContract[Runtime]:
    """The library half the workflow capability is built over, rooted in this test's own folder."""
    return WorkflowContract[Runtime](
        source=folder_toolset(tmp_path / "library"), vocabulary=FOLDER, expose=(), registry=StepRegistry(), saved_by="test"
    )


# -- described readiness ---------------------------------------------------------------

DESCRIBED_TITLE = "Design a release workflow"
DESCRIBED_SUMMARY = "Turns release intent into a checked rollout plan."
DESCRIBED_WORDS = WorkflowPresentation(
    purpose="Prepare repeatable releases.",
    produces="A reviewed rollout plan.",
    owner_participation="Approve the rollout gate.",
    not_for="Incidents already in progress.",
)

PROSE_FIELDS = ("purpose", "produces", "owner_participation", "not_for")
PLACEHOLDER_VALUES = ("TBD", " todo ", "N/A", "(not stated)", "Coming   Soon")


def _described(spec: Workflow, **overrides: str) -> Draft:
    """A draft nobody has to guess about: the positive fixture, with `overrides` spoiling one field."""
    words = DESCRIBED_WORDS.model_dump()
    words.update({field: value for field, value in overrides.items() if field in PROSE_FIELDS})
    return Draft(
        spec_yaml=yaml.safe_dump(spec.model_dump(mode="json"), sort_keys=False),
        title=overrides.get("title", DESCRIBED_TITLE),
        summary=overrides.get("summary", DESCRIBED_SUMMARY),
        applicability="Use for a planned release. Not for an incident.",
        presentation=WorkflowPresentation.model_validate(words),
    )


def _errors(readiness: DraftReadiness) -> list[str]:
    return [finding.field for finding in readiness.findings if finding.severity == "error"]


def _warnings(readiness: DraftReadiness) -> list[str]:
    return [finding.field for finding in readiness.findings if finding.severity == "warning"]


def test_the_positive_fixture_is_both_executable_and_described(quarterly_report: Workflow, registry: StepRegistry) -> None:
    """Two questions, answered apart: this run can execute it, and a reader is told what it is."""
    readiness = draft_readiness(quarterly_report, _described(quarterly_report), registry)

    assert readiness.executable is True
    assert readiness.described is True
    assert readiness.findings == ()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("title", ""),
        ("title", "x" * 81),
        ("summary", ""),
        ("summary", "x" * 281),
        *((field, "") for field in PROSE_FIELDS),
        *((field, placeholder) for field in PROSE_FIELDS for placeholder in PLACEHOLDER_VALUES),
    ],
)
def test_one_spoiled_field_is_the_one_field_reported(field: str, value: str, quarterly_report: Workflow, registry: StepRegistry) -> None:
    """Each rule names its own field, so a form can put the finding beside the box it belongs to."""
    readiness = draft_readiness(quarterly_report, _described(quarterly_report, **{field: value}), registry)

    assert _errors(readiness) == [field]
    assert readiness.described is False


@pytest.mark.parametrize("value", PLACEHOLDER_VALUES)
def test_a_placeholder_title_is_not_a_title(value: str, quarterly_report: Workflow, registry: StepRegistry) -> None:
    """Trim, collapse the spaces inside, then casefold: the same word however it was typed."""
    readiness = draft_readiness(quarterly_report, _described(quarterly_report, title=value), registry)

    assert _errors(readiness) == ["title"]


def test_the_exact_placeholder_set_is_the_whole_rule() -> None:
    """No English is inspected for negative terms. These five words, normalised, and nothing else."""
    assert frozenset({"tbd", "todo", "n/a", "(not stated)", "coming soon"}) == PLACEHOLDERS
    assert placeholder_key("  Coming   Soon ") == "coming soon"
    assert placeholder_key("N/A") == "n/a"
    assert placeholder_key("Prepare repeatable releases.") not in PLACEHOLDERS


def test_prose_over_the_limit_is_refused_on_its_own_field(quarterly_report: Workflow, registry: StepRegistry) -> None:
    """A field HTTP now accepts is bounded here, so nobody pastes a document into a card."""
    readiness = draft_readiness(quarterly_report, _described(quarterly_report, purpose="x" * (PROSE_LIMIT + 1)), registry)

    assert _errors(readiness) == ["purpose"]


def test_a_summary_that_repeats_the_spec_description_warns_and_still_saves(quarterly_report: Workflow, registry: StepRegistry) -> None:
    """A warning is advice, not a refusal: it never decides whether the workflow is described."""
    draft = _described(quarterly_report, summary=f"  {quarterly_report.description.upper()}  ")

    readiness = draft_readiness(quarterly_report, draft, registry)

    assert _warnings(readiness) == ["summary"]
    assert _errors(readiness) == []
    assert readiness.described is True


async def test_a_warned_draft_still_saves(tmp_path: Path, quarterly_report: Workflow, registry: StepRegistry) -> None:
    library = FolderLibrary(tmp_path / "workflows")
    draft = _described(quarterly_report, summary=quarterly_report.description)

    workflow_id = await save_draft(
        draft, library=library, registry=registry, self_authoring=SelfAuthoringSettings(custom_root=tmp_path / "custom")
    )

    assert await library.fetch_workflow(workflow_id) == quarterly_report


async def test_work_this_run_cannot_build_is_a_warning_that_leaves_it_described(
    tmp_path: Path, quarterly_report: Workflow, call_log: CallLog
) -> None:
    """Described and executable are independent: words a reader needs, and steps this run holds."""
    del tmp_path
    thin = StepRegistry()
    register_quarterly_steps(thin, call_log, exclude=frozenset({"publish"}))

    readiness = draft_readiness(quarterly_report, _described(quarterly_report), thin)

    assert readiness.executable is False
    assert _warnings(readiness) == ["work"]
    assert readiness.described is True


async def test_an_undescribed_draft_is_refused_and_nothing_is_invented(
    tmp_path: Path, quarterly_report: Workflow, registry: StepRegistry
) -> None:
    """The fallbacks are gone: a draft nobody described is refused, not saved under words this code wrote."""
    root = tmp_path / "workflows"
    library = FolderLibrary(root)
    draft = Draft(spec_yaml=yaml.safe_dump(quarterly_report.model_dump(mode="json"), sort_keys=False), title="Quarterly report")

    with pytest.raises(DraftNotSavableError) as refused:
        await save_draft(draft, library=library, registry=registry, self_authoring=SelfAuthoringSettings(custom_root=tmp_path / "custom"))

    assert not list(root.glob("*.md")), "a refused save must leave no file behind"
    assert "(not stated)" not in str(refused.value)
    readiness = refused.value.readiness
    assert readiness is not None
    assert sorted(_errors(readiness)) == sorted(["summary", *PROSE_FIELDS])


async def test_the_refusal_names_every_missing_field_at_once(tmp_path: Path, quarterly_report: Workflow, registry: StepRegistry) -> None:
    """One round trip per rewrite, not one per field."""
    draft = _described(quarterly_report, title="", purpose="TBD")

    with pytest.raises(DraftNotSavableError) as refused:
        await save_draft(
            draft, library=FolderLibrary(tmp_path / "w"), registry=registry, self_authoring=SelfAuthoringSettings(custom_root=tmp_path / "custom")
        )

    assert sorted(_errors(refused.value.readiness or DraftReadiness(executable=False, described=False, findings=()))) == ["purpose", "title"]


async def test_both_save_routes_refuse_the_same_draft_with_the_same_findings(
    tmp_path: Path, quarterly_report: Workflow, registry: StepRegistry
) -> None:
    """The agent's save and the person's are one gate: the same draft is refused the same way at both."""
    root = tmp_path / "workflows"
    runtime = Runtime(
        registry=registry,
        library=FolderLibrary(root),
        agent=Agent(TestModel(), deps_type=Runtime),
        conversation=Conversation(draft=_described(quarterly_report, produces="N/A")),
        session_log=NoSessionLog(),
        state=a_state(tmp_path),
    )
    pool = SessionPool(runtime)
    controller = a_controller(pool, state=tmp_path / "state")
    session_id = one_session(pool)

    process_save = await controller.save_current()
    session_save = await controller.save_session(session_id)

    assert (process_save.status_code, session_save.status_code) == (400, 400)
    first = json.loads(bytes(process_save.body))
    second = json.loads(bytes(session_save.body))
    assert first["readiness"] == second["readiness"]
    assert first["readiness"]["described"] is False
    assert [finding["field"] for finding in first["readiness"]["findings"]] == ["produces"]
    assert not list(root.glob("*.md")), "a refused save writes nothing"


async def test_a_described_draft_saves_through_the_route(tmp_path: Path, quarterly_report: Workflow, registry: StepRegistry) -> None:
    library = FolderLibrary(tmp_path / "workflows")
    runtime = Runtime(
        registry=registry,
        library=library,
        agent=Agent(TestModel(), deps_type=Runtime),
        conversation=Conversation(draft=_described(quarterly_report)),
        session_log=NoSessionLog(),
        state=a_state(tmp_path),
    )
    controller = a_controller(SessionPool(runtime), state=tmp_path / "state")

    response = await controller.save_current()

    assert response.status_code == 200
    saved = json.loads(bytes(response.body))
    card = await library.fetch_card(saved["id"])
    assert card is not None
    assert card.title == DESCRIBED_TITLE
    assert card.presentation == DESCRIBED_WORDS


async def test_the_saved_card_carries_the_authors_own_words(tmp_path: Path, quarterly_report: Workflow, registry: StepRegistry) -> None:
    """The words the readiness gate checked are the words the library keeps."""
    library = FolderLibrary(tmp_path / "workflows")

    workflow_id = await save_draft(
        _described(quarterly_report), library=library, registry=registry, self_authoring=SelfAuthoringSettings(custom_root=tmp_path / "custom")
    )

    card = await library.fetch_card(workflow_id)
    assert card is not None
    assert card.presentation is not None
    assert card.presentation.purpose == "Prepare repeatable releases."


def test_a_patch_replaces_only_the_fields_it_carries() -> None:
    """Absent means unchanged, the same ruling `settled_draft` makes about the model's silence."""
    merged = effective_presentation(DESCRIBED_WORDS, WorkflowPresentationPatch(purpose="Ship the same way every time."))

    assert merged.purpose == "Ship the same way every time."
    assert merged.produces == DESCRIBED_WORDS.produces
    assert merged.owner_participation == DESCRIBED_WORDS.owner_participation
    assert merged.not_for == DESCRIBED_WORDS.not_for


def test_a_patch_onto_nothing_starts_from_an_empty_presentation() -> None:
    """A legacy card carries none, and a partial patch onto it stays partial rather than inventing the rest."""
    merged = effective_presentation(None, WorkflowPresentationPatch(purpose="Ship the same way every time."))

    assert merged.purpose == "Ship the same way every time."
    assert (merged.produces, merged.owner_participation, merged.not_for) == ("", "", "")


async def test_a_check_carries_the_words_onto_the_draft_and_says_what_is_still_missing(
    tmp_path: Path, quarterly_report: Workflow, registry: StepRegistry
) -> None:
    """The draft-shaped half of the agent's reading: what this check settled, and what it still owes."""
    ctx = _ctx(tmp_path, registry)
    spec_yaml = yaml.safe_dump(quarterly_report.model_dump(mode="json"), sort_keys=False)

    answer = await check_draft(
        ctx,
        spec_yaml,
        title=DESCRIBED_TITLE,
        summary=DESCRIBED_SUMMARY,
        applicability="Use for a planned release. Not for an incident.",
        purpose="Prepare repeatable releases.",
        produces="A reviewed rollout plan.",
    )

    draft = ctx.deps.conversation.draft
    assert draft.presentation is not None
    assert draft.presentation.purpose == "Prepare repeatable releases."
    assert "owner_participation" in answer, "the check says which words it is still waiting for"
    assert "not_for" in answer


async def test_a_later_check_keeps_the_words_an_earlier_one_settled(
    tmp_path: Path, quarterly_report: Workflow, registry: StepRegistry
) -> None:
    ctx = _ctx(tmp_path, registry, draft=_described(quarterly_report))
    spec_yaml = yaml.safe_dump(quarterly_report.model_dump(mode="json"), sort_keys=False)

    await check_draft(ctx, spec_yaml)

    assert ctx.deps.conversation.draft.presentation == DESCRIBED_WORDS


def test_the_draft_payload_carries_the_readiness_the_save_gate_reads(tmp_path: Path, quarterly_report: Workflow, registry: StepRegistry) -> None:
    """The browser never decides this: Save is disabled from the same reading the gate refuses on."""
    runtime = Runtime(
        registry=registry,
        library=FolderLibrary(tmp_path / "workflows"),
        agent=Agent(TestModel(), deps_type=Runtime),
        conversation=Conversation(draft=_described(quarterly_report, not_for="TBD")),
        session_log=NoSessionLog(),
        state=a_state(tmp_path),
    )

    payload = draft_payload(runtime)

    readiness = payload["readiness"]
    assert isinstance(readiness, dict)
    findings = readiness["findings"]
    assert isinstance(findings, list)
    assert readiness["described"] is False
    assert [finding["field"] for finding in findings if isinstance(finding, dict)] == ["not_for"]


# --- the intent a draft carries -------------------------------------------------------------------
#
# What the person asked for, recorded beside the proposal and kept with it: through a check, through
# a changed proposal, into the library, and back out of an adaptation.

ASKED_FOR = WorkflowIntent(
    request="Close the books at the end of every quarter",
    expected_result="The published report, and the numbers behind it.",
    success_example="Q2 closed on the first working day of July with no manual edits.",
    empty_input_behavior="A quarter with no ledger entries reports that and publishes nothing.",
    failure_behavior="A source the run cannot read is named, and nothing is published.",
    trigger="scheduled",
    assumptions=("A quarter ends on the last day of March, June, September and December.",),
)


def test_a_draft_records_what_was_asked_for_beside_the_card(quarterly_report: Workflow) -> None:
    settled = settled_draft(quarterly_report, title="Quarterly", summary="s", applicability="a", existing=Draft(), intent=ASKED_FOR)

    assert settled.intent == ASKED_FOR


def test_a_request_only_intent_settles_and_reports_what_it_still_owes(quarterly_report: Workflow) -> None:
    """Half an intent is a normal draft. Nothing here refuses it, and nothing fills the rest in."""
    partial = WorkflowIntent(request="Close the books at the end of every quarter")

    settled = settled_draft(quarterly_report, title="Quarterly", summary="s", applicability="a", existing=Draft(), intent=partial)

    assert settled.intent == partial
    assert intent_issues(partial) == ("expected_result", "success_example", "empty_input_behavior", "failure_behavior")


def test_a_changed_proposal_keeps_the_request_it_came_from(quarterly_report: Workflow) -> None:
    """A later turn redrafts the spec and says nothing about the intent, which is not a retraction."""
    held = Draft(spec_yaml="old", title="Quarterly report", summary="Close the quarter", applicability="Not ad hoc.", intent=ASKED_FOR)

    settled = settled_draft(quarterly_report, title="", summary="", applicability="", existing=held)

    assert settled.intent == ASKED_FOR
    assert yaml.safe_load(settled.spec_yaml)["name"] == quarterly_report.name, "the spec still comes from the checked object"


def test_a_stated_intent_replaces_the_one_the_draft_held(quarterly_report: Workflow) -> None:
    restated = WorkflowIntent(request="Close the books, and flag anything that moved more than a tenth")

    settled = settled_draft(
        quarterly_report, title="", summary="", applicability="", existing=Draft(intent=ASKED_FOR), intent=restated
    )

    assert settled.intent == restated


def test_a_draft_nobody_stated_an_intent_for_carries_none(quarterly_report: Workflow) -> None:
    """Absent stays absent: no intent is ever read off the spec."""
    assert settled_draft(quarterly_report, title="t", summary="s", applicability="a", existing=Draft()).intent is None


async def test_a_check_carrying_an_intent_settles_it_onto_the_draft_and_keeps_it_on_disk(
    tmp_path: Path, quarterly_report: Workflow, registry: StepRegistry
) -> None:
    ctx = _ctx(tmp_path, registry)
    spec_yaml = yaml.safe_dump(quarterly_report.model_dump(mode="json"), sort_keys=False)

    await check_draft(ctx, spec_yaml, title="Quarterly report", summary="Close the quarter", applicability="Not ad hoc.", intent=ASKED_FOR)

    assert ctx.deps.conversation.draft.intent == ASKED_FOR
    restarted = remembered_draft(path=_kept_path(tmp_path))
    assert restarted is not None
    assert restarted.intent == ASKED_FOR, "a session restart reads back what was asked for"


async def test_a_check_that_says_nothing_about_the_intent_keeps_the_one_the_draft_holds(
    tmp_path: Path, quarterly_report: Workflow, registry: StepRegistry
) -> None:
    """Every caller that predates the argument keeps working, and none of them erases an intent."""
    ctx = _ctx(tmp_path, registry, draft=Draft(intent=ASKED_FOR))
    spec_yaml = yaml.safe_dump(quarterly_report.model_dump(mode="json"), sort_keys=False)

    await check_draft(ctx, spec_yaml)

    assert ctx.deps.conversation.draft.intent == ASKED_FOR


async def test_the_saved_card_carries_what_the_person_asked_for(tmp_path: Path, quarterly_report: Workflow, registry: StepRegistry) -> None:
    library = FolderLibrary(tmp_path / "workflows")
    draft = _described(quarterly_report).model_copy(update={"intent": ASKED_FOR})

    workflow_id = await save_draft(
        draft, library=library, registry=registry, self_authoring=SelfAuthoringSettings(custom_root=tmp_path / "custom")
    )

    card = await library.fetch_card(workflow_id)
    assert card is not None
    assert card.intent == ASKED_FOR


async def test_a_half_stated_intent_does_not_stand_between_a_described_draft_and_the_library(
    tmp_path: Path, quarterly_report: Workflow, registry: StepRegistry
) -> None:
    """The save gate reads the card. An incomplete intent is recorded, never a refusal."""
    library = FolderLibrary(tmp_path / "workflows")
    partial = WorkflowIntent(request="Close the books at the end of every quarter")
    draft = _described(quarterly_report).model_copy(update={"intent": partial})

    workflow_id = await save_draft(
        draft, library=library, registry=registry, self_authoring=SelfAuthoringSettings(custom_root=tmp_path / "custom")
    )

    card = await library.fetch_card(workflow_id)
    assert card is not None
    assert card.intent == partial


async def test_a_draft_saved_without_an_intent_reads_back_without_one(tmp_path: Path, quarterly_report: Workflow, registry: StepRegistry) -> None:
    """A workflow saved the way every one before this was saved holds exactly what it always held."""
    library = FolderLibrary(tmp_path / "workflows")

    workflow_id = await save_draft(
        _described(quarterly_report), library=library, registry=registry, self_authoring=SelfAuthoringSettings(custom_root=tmp_path / "custom")
    )

    card = await library.fetch_card(workflow_id)
    assert card is not None
    assert card.intent is None
    file = tmp_path / "workflows" / f"{quarterly_report.name}-v{quarterly_report.version}.md"
    assert "intent" not in load_spec_document(file.read_text(encoding="utf-8")).frontmatter


async def test_an_adaptation_inherits_what_the_parent_was_asked_for(tmp_path: Path, quarterly_report: Workflow, registry: StepRegistry) -> None:
    """A diff edits the spec. The automation the parent answered is still the one the child answers."""
    library = FolderLibrary(tmp_path / "workflows")
    draft = _described(quarterly_report).model_copy(update={"intent": ASKED_FOR})
    parent_id = await save_draft(draft, library=library, registry=registry, self_authoring=SelfAuthoringSettings(custom_root=tmp_path / "custom"))

    child, lineage = await adapt_saved(parent_id, ({"op": "set_description", "to": "Now with commentary."},), library=library)

    assert child.intent == ASKED_FOR
    child_id = await save_draft(
        child, library=library, registry=registry, self_authoring=SelfAuthoringSettings(custom_root=tmp_path / "custom"), lineage=lineage
    )
    kept = await library.fetch_card(child_id)
    assert kept is not None
    assert kept.intent == ASKED_FOR, "and it survives the round trip"


async def test_an_adaptation_of_a_parent_with_no_intent_invents_none(tmp_path: Path, quarterly_report: Workflow, registry: StepRegistry) -> None:
    library = FolderLibrary(tmp_path / "workflows")
    parent_id = await save_draft(
        _described(quarterly_report), library=library, registry=registry, self_authoring=SelfAuthoringSettings(custom_root=tmp_path / "custom")
    )

    child, _ = await adapt_saved(parent_id, ({"op": "set_description", "to": "Now with commentary."},), library=library)

    assert child.intent is None
