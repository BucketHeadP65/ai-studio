"""The conversation the panel shows, and the conversation a restart restores.

Three behaviors under test. First: a text part written before replies were plain prose is a JSON
envelope on disk, read back for its `message` field only -- never by grepping for the key, which
would trust any lookalike dict. Second: forgetting must bind the disk too. The transcript is
append-only and stays; the watermark stops a reboot from resurrecting a conversation the person
explicitly dismissed -- the dual-history divergence in the flesh. Third: what a restart restores
is the draft kept on disk, never the history's own retyping of it -- the transcript no longer
supplies a draft at all, however much its text looks like one.
"""

from __future__ import annotations

import json

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import yaml

from core.ai.workflows.contracts import Lineage, StepRegistry, Workflow, WorkflowIntent
from pydantic_ai.messages import ModelRequest, ModelResponse, TextPart, UserPromptPart
from pydantic_ai_harness.step_persistence import RunRecord

from harness import session as session_state
from harness.features.authoring.controller import _turns
from harness.features.authoring.domain import Draft
from harness.session import Conversation
from tests.conftest import CallLog, register_quarterly_steps
from tests.support.state import a_state

if TYPE_CHECKING:
    from pathlib import Path


def _draft(home: Path) -> Path:
    """This test's own kept proposal, under its own state home."""
    return a_state(home).draft


def _dialogue(*parts: object) -> list[ModelRequest | ModelResponse]:
    messages: list[ModelRequest | ModelResponse] = []
    for part in parts:
        if isinstance(part, UserPromptPart):
            messages.append(ModelRequest(parts=[part]))
        else:
            assert isinstance(part, TextPart)
            messages.append(ModelResponse(parts=[part]))
    return messages


def _legacy_envelope(*, message: str, spec_yaml: str = "", title: str = "") -> str:
    """The pre-prose reply's wire shape, exactly as a run recorded before this change would have written it."""
    return json.dumps({"message": message, "spec_yaml": spec_yaml, "title": title, "summary": "", "applicability": ""})


def test_an_old_transcripts_envelope_reads_back_as_its_message() -> None:
    history = _dialogue(UserPromptPart(content="draft me a workflow"), TextPart(content=_legacy_envelope(message="Here is the graph.")))
    assert _turns(history) == [{"who": "you", "text": "draft me a workflow"}, {"who": "agent", "text": "Here is the graph."}]


def test_prose_parts_are_shown_as_themselves() -> None:
    history = _dialogue(TextPart(content="thinking about the wiring..."))
    assert _turns(history) == [{"who": "agent", "text": "thinking about the wiring..."}]


def test_json_that_is_not_the_legacy_shape_is_not_mined_for_a_message_key() -> None:
    alien = '{"message": "spoofed", "other": 42}'
    history = _dialogue(TextPart(content=alien))
    assert _turns(history) == [{"who": "agent", "text": alien}], "a lookalike dict is not the legacy envelope"


def _record(run_id: str, *, minutes_ago: int) -> RunRecord:
    return RunRecord(run_id=run_id, started_at=datetime.now(tz=UTC) - timedelta(minutes=minutes_ago))


def test_forgotten_runs_are_not_resumable() -> None:
    watermark = datetime.now(tz=UTC) - timedelta(minutes=30)
    records = [_record("old", minutes_ago=60), _record("new", minutes_ago=5), _record("newer", minutes_ago=1)]
    kept = session_state.resumable(records, forgotten_at=watermark)
    assert [record.run_id for record in kept] == ["newer", "new"], "newest first, forgotten gone"


def test_without_a_watermark_everything_is_resumable() -> None:
    records = [_record("old", minutes_ago=60), _record("new", minutes_ago=5)]
    kept = session_state.resumable(records, forgotten_at=None)
    assert [record.run_id for record in kept] == ["new", "old"]


def test_a_restored_conversation_comes_back_checked(quarterly_report: Workflow, registry: StepRegistry, tmp_path: Path) -> None:
    """The machine's verdict has to survive the restart that the agent's claim survives.

    `check_draft` records its verdict in memory only, so a restart kept the *sentence* "I
    validated it" while dropping the thing that made it true. So the kept draft is re-checked at
    boot. It costs milliseconds, and it means the two halves of the session agree about what has
    been verified -- which also gives `run_workflow("draft")` something to run after a restart
    instead of "there is no checked draft".
    """
    session_state.remember_draft(
        Draft(spec_yaml=yaml.safe_dump(quarterly_report.model_dump(mode="json"), sort_keys=False), title="Quarterly report"),
        path=_draft(tmp_path),
    )
    conversation = Conversation()
    session_state.restore_conversation(conversation, _dialogue(TextPart("here it is")), registry, draft_file=_draft(tmp_path))

    assert conversation.last_workflow == quarterly_report, "the verdict is about the spec that came back"
    assert conversation.last_issues == ()
    assert conversation.draft.title == "Quarterly report"


def test_a_restored_draft_that_no_longer_checks_out_says_so(quarterly_report: Workflow, call_log: CallLog, tmp_path: Path) -> None:
    """The environment can move while the process is down; boot is exactly when to notice."""
    thin = StepRegistry()
    register_quarterly_steps(thin, call_log, exclude=frozenset({"publish"}))
    session_state.remember_draft(Draft(spec_yaml=yaml.safe_dump(quarterly_report.model_dump(mode="json"), sort_keys=False)), path=_draft(tmp_path))
    conversation = Conversation()
    session_state.restore_conversation(conversation, _dialogue(TextPart("x")), thin, draft_file=_draft(tmp_path))

    assert conversation.last_workflow is None, "a spec this run cannot execute is not a checked draft"
    assert any("publish" in issue for issue in conversation.last_issues)


def test_a_restored_conversation_with_no_draft_leaves_the_verdict_empty(registry: StepRegistry, tmp_path: Path) -> None:
    conversation = Conversation()
    session_state.restore_conversation(conversation, _dialogue(TextPart("hi")), registry, draft_file=_draft(tmp_path))

    assert conversation.draft.spec_yaml == ""
    assert conversation.last_workflow is None
    assert conversation.last_issues == ()


def test_a_remembered_draft_beats_the_one_in_the_transcript(quarterly_report: Workflow, registry: StepRegistry, tmp_path: Path) -> None:
    """The verified spec is not in the history, so it has to be kept somewhere.

    The dialogue rebuilds from the transcript, but the settled draft cannot: it is whatever passed
    `check_draft`, and the checker's finding never enters the transcript. A transcript whose own
    text is a JSON envelope naming a different, unverified spec must not win.
    """
    session_state.remember_draft(Draft(spec_yaml=yaml.safe_dump(quarterly_report.model_dump(mode="json"), sort_keys=False), title="Kept"), path=_draft(tmp_path))
    stale = _legacy_envelope(message="x", spec_yaml="name: retyped-and-wrong\nversion: 1\n", title="Retyped")

    conversation = Conversation()
    session_state.restore_conversation(conversation, _dialogue(TextPart(stale)), registry, draft_file=_draft(tmp_path))

    assert conversation.draft.title == "Kept"
    assert conversation.last_workflow == quarterly_report, "and it comes back checked, like any restored draft"


def test_with_nothing_remembered_no_draft_comes_back(registry: StepRegistry, tmp_path: Path) -> None:
    """No kept file means no draft, however much the transcript's own text is shaped like one.

    History no longer supplies a draft at all: `restore_conversation` reads the kept file only.
    """
    shaped_like_a_draft = _legacy_envelope(message="x", spec_yaml="name: from-history\nversion: 1\n", title="From history")
    conversation = Conversation()
    session_state.restore_conversation(conversation, _dialogue(TextPart(shaped_like_a_draft)), registry, draft_file=_draft(tmp_path))

    assert conversation.draft.spec_yaml == ""
    assert conversation.draft.title == ""


def test_forgetting_drops_the_remembered_draft(quarterly_report: Workflow, tmp_path: Path) -> None:
    """Forget means forget: the boundary hides the dialogue, and the proposal file goes with it.

    The forget route deletes the draft file beside recording the session log boundary; what this
    pins is the read-back half -- a deleted file reads as no draft, never as an error.
    """
    session_state.remember_draft(Draft(spec_yaml="name: x\nversion: 1\n", title="Doomed"), path=_draft(tmp_path))
    _draft(tmp_path).unlink(missing_ok=True)
    assert session_state.remembered_draft(path=_draft(tmp_path)) is None


def test_an_unreadable_remembered_draft_is_not_a_boot_failure(registry: StepRegistry, tmp_path: Path) -> None:
    """Same forgiveness the transcript gets: a bad file must not stop the app starting."""
    _draft(tmp_path).write_text("{not json", encoding="utf-8")
    conversation = Conversation()
    session_state.restore_conversation(conversation, _dialogue(TextPart("hi")), registry, draft_file=_draft(tmp_path))
    assert conversation.draft.spec_yaml == ""


def test_an_adaptation_is_still_an_adaptation_after_a_restart(quarterly_report: Workflow, registry: StepRegistry, tmp_path: Path) -> None:
    """The diff was in memory only, so a restart between adapting and saving lost the parent.

    Silently, and in the way that matters most: the child still saves, still validates, still
    lands in the library -- as a workflow that resembles its parent and records no relationship to
    it. Node-granular trust has nowhere to attach and the incumbent has nothing pointing at it.

    Fixed by the diff belonging to the draft rather than to the conversation, which is where the
    persistence already was. Nothing needed a new file.
    """
    lineage = Lineage(
        parent_name=quarterly_report.name, parent_version=quarterly_report.version, ops=({"op": "set_description", "to": "x"},)
    )
    session_state.remember_draft(
        Draft(spec_yaml=yaml.safe_dump(quarterly_report.model_dump(mode="json"), sort_keys=False), title="Child", lineage=lineage),
        path=_draft(tmp_path),
    )

    conversation = Conversation()
    session_state.restore_conversation(conversation, _dialogue(TextPart("hi")), registry, draft_file=_draft(tmp_path))

    assert conversation.draft.lineage == lineage, "the child came back knowing what it came from"


def test_a_draft_still_knows_what_was_asked_for_after_a_restart(quarterly_report: Workflow, registry: StepRegistry, tmp_path: Path) -> None:
    """What was asked for rides on the draft, so the kept file is the whole of the restore."""
    intent = WorkflowIntent(
        request="Close the books at the end of every quarter",
        expected_result="The published report, and the numbers behind it.",
        trigger="scheduled",
        assumptions=("A quarter ends on the last day of March, June, September and December.",),
    )
    session_state.remember_draft(
        Draft(spec_yaml=yaml.safe_dump(quarterly_report.model_dump(mode="json"), sort_keys=False), title="Quarterly", intent=intent),
        path=_draft(tmp_path),
    )

    conversation = Conversation()
    session_state.restore_conversation(conversation, _dialogue(TextPart("hi")), registry, draft_file=_draft(tmp_path))

    assert conversation.draft.intent == intent


def test_a_draft_kept_before_intents_existed_comes_back_without_one(quarterly_report: Workflow, registry: StepRegistry, tmp_path: Path) -> None:
    """An older kept draft must not refuse the boot, and nothing invents an intent for it."""
    older = {"spec_yaml": yaml.safe_dump(quarterly_report.model_dump(mode="json"), sort_keys=False), "title": "Quarterly"}
    _draft(tmp_path).parent.mkdir(parents=True, exist_ok=True)
    _draft(tmp_path).write_text(json.dumps(older), encoding="utf-8")

    conversation = Conversation()
    session_state.restore_conversation(conversation, _dialogue(TextPart("hi")), registry, draft_file=_draft(tmp_path))

    assert conversation.draft.title == "Quarterly"
    assert conversation.draft.intent is None
