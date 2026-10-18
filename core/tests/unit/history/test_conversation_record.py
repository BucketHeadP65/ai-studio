from dataclasses import replace
from hashlib import sha256

import pytest

from pydantic_ai.messages import ModelResponse, ToolReturnPart, WorkspaceRef
from pydantic_core import to_jsonable_python

from core.ai.history.conversation_record import STAMP_FIELDS, ConversationRecord, MessageJson, digest_of
from core.ai.history.domain import StepEntry
from tests.unit.history.conftest import CLEARED, INSTRUCTIONS, RESULT, snapshot, turn_messages


def tool_result_content(record: ConversationRecord, *, run_id: str, seq: int) -> object:
    """The content of the tool return the worked example carries as its third message."""
    part = record.messages_at(run_id=run_id, seq=seq)[2].parts[0]
    assert isinstance(part, ToolReturnPart)
    return part.content


def as_stored(messages) -> ConversationRecord:
    """A record that knows the digests of `messages` and holds none of their content, as a read of a written conversation does."""
    written = ConversationRecord()
    written.record(snapshot("run-written", 1, messages), seq=0)
    return ConversationRecord(forms=written.forms, stamps=written.stamps)


class TestRecord:
    def test_first_step_stores_every_message_once(self, messages):
        record = ConversationRecord()

        added = record.record(snapshot("run-a", 1, messages[:2]), seq=0)

        assert added.new_messages == 2
        assert added.new_forms == 0
        assert added.new_instructions == 1
        assert len(record.steps) == 1
        assert [entry.form for entry in record.steps[0].entries] == [0, 0]

    def test_second_step_adds_only_the_new_messages(self, messages):
        record = ConversationRecord()
        record.record(snapshot("run-a", 1, messages[:2]), seq=0)

        added = record.record(snapshot("run-a", 2, messages), seq=1)

        assert added.new_messages == 2
        assert added.new_instructions == 0
        assert len(record.forms) == 4

    def test_reconstructs_each_step_exactly(self, messages):
        record = ConversationRecord()
        record.record(snapshot("run-a", 1, messages[:2]), seq=0)
        record.record(snapshot("run-a", 2, messages), seq=1)

        assert record.messages_json_at(run_id="run-a", seq=0) == [to_jsonable_python(m) for m in messages[:2]]
        assert record.messages_json_at(run_id="run-a", seq=1) == [to_jsonable_python(m) for m in messages]
        assert to_jsonable_python(record.messages_at(run_id="run-a", seq=1)) == to_jsonable_python(messages)

    def test_a_rewritten_tool_result_is_a_new_form_of_the_same_message(self, messages):
        record = ConversationRecord()
        record.record(snapshot("run-a", 2, messages), seq=0)

        added = record.record(snapshot("run-b", 1, turn_messages(cleared=True)), seq=0)

        assert added.new_messages == 0
        assert added.new_forms == 1
        original = tool_result_content(record, run_id="run-a", seq=0)
        rewritten = tool_result_content(record, run_id="run-b", seq=0)
        assert (original, rewritten) == (RESULT, CLEARED)

    def test_instructions_are_stored_once_by_digest(self, messages):
        record = ConversationRecord()

        added = record.record(snapshot("run-a", 2, messages), seq=0)

        assert list(record.new_instructions.values()) == [INSTRUCTIONS]
        assert added.new_instructions == 1
        stamps = next(iter(record.new_stamps.values()))
        assert set(stamps) <= set(STAMP_FIELDS)
        assert isinstance(stamps["instructions"], dict)

    def test_refuses_a_repeated_seq(self, messages):
        record = ConversationRecord()
        record.record(snapshot("run-a", 1, messages[:2]), seq=0)

        with pytest.raises(ValueError, match="seq 0"):
            record.record(snapshot("run-a", 2, messages), seq=0)

    def test_refuses_two_messages_with_one_key(self, messages):
        record = ConversationRecord()

        with pytest.raises(ValueError, match="share a key"):
            record.record(snapshot("run-a", 1, [messages[0], messages[0]]), seq=0)


class TestDiffByDigest:
    def test_the_digests_are_the_ones_stored_beside_the_content(self, messages):
        record = ConversationRecord()

        record.record(snapshot("run-a", 1, messages[:2]), seq=0)

        assert record.forms == {key: [digest_of(record.new_forms[key, 0])] for key, _ in record.new_forms}
        assert record.stamps == {key: [digest_of(record.new_stamps[key, 0])] for key, _ in record.new_stamps}

    def test_a_content_the_conversation_holds_stays_at_its_form(self, messages):
        record = as_stored(messages)

        added = record.record(snapshot("run-a", 1, messages), seq=0)

        assert [entry.form for entry in record.steps[0].entries] == [0, 0, 0, 0]
        assert [entry.stamp for entry in record.steps[0].entries] == [0, 0, 0, 0]
        assert record.new_forms == {}
        assert record.new_stamps == {}
        assert (added.new_messages, added.new_forms) == (0, 0)

    def test_a_changed_content_takes_the_next_form_of_the_message(self, messages):
        record = as_stored(messages)

        added = record.record(snapshot("run-a", 1, turn_messages(cleared=True)), seq=0)

        assert [entry.form for entry in record.steps[0].entries] == [0, 0, 1, 0]
        assert [form for _, form in record.new_forms] == [1]
        assert (added.new_messages, added.new_forms) == (0, 1)

    def test_changed_stamps_take_the_next_version_of_the_message(self, messages):
        record = as_stored(messages)
        restamped = [replace(message, run_id="run-b") for message in messages]

        record.record(snapshot("run-b", 1, restamped), seq=0)

        assert [entry.stamp for entry in record.steps[0].entries] == [1, 1, 1, 1]
        assert [version for _, version in record.new_stamps] == [1, 1, 1, 1]
        assert record.new_forms == {}

    def test_a_key_the_conversation_does_not_hold_is_a_new_message(self, messages):
        record = as_stored(messages[:2])

        added = record.record(snapshot("run-a", 1, messages), seq=0)

        assert added.new_messages == 2
        assert [form for _, form in record.new_forms] == [0, 0]
        assert len(record.forms) == 4

    def test_one_instructions_text_is_counted_once_across_the_messages_carrying_it(self, messages):
        record = ConversationRecord()

        added = record.record(snapshot("run-a", 2, messages), seq=0)

        assert added.new_instructions == 1
        assert len(record.new_instructions) == 1

    def test_only_the_content_it_added_is_in_memory(self, messages):
        record = as_stored(messages)

        record.record(snapshot("run-a", 1, messages), seq=0)

        with pytest.raises(KeyError):
            record.messages_json_at(run_id="run-a", seq=0)


class TestExactness:
    def test_refuses_a_split_the_message_does_not_come_back_from(self, messages):
        class Drifting(ConversationRecord):
            """A record whose split drops a field of the message, so the pieces no longer merge back into it."""

            def _split(self, message: MessageJson) -> tuple[MessageJson, MessageJson]:
                content, stamps = super()._split(message)
                return {name: value for name, value in content.items() if name != "parts"}, stamps

        record = Drifting()

        with pytest.raises(ValueError, match="does not reconstruct"):
            record.record(snapshot("run-a", 1, messages[:2]), seq=0)


class TestLatestStep:
    def test_skips_interrupted_steps_unless_asked(self, messages):
        record = ConversationRecord()
        record.record(snapshot("run-a", 1, messages[:2]), seq=0)
        record.record(snapshot("run-a", 2, messages[:3], state="interrupted"), seq=1)

        complete = record.latest_step(run_id="run-a")
        interrupted = record.latest_step(run_id="run-a", include_interrupted=True)

        assert complete is not None
        assert interrupted is not None
        assert (complete.seq, interrupted.seq) == (0, 1)
        assert record.latest_step(run_id="run-none") is None


class TestDigestOf:
    def test_writes_non_ascii_text_as_escapes(self):
        assert digest_of({"text": "café"}) == sha256(b'{"text":"caf\\u00e9"}').hexdigest()


class TestWorkspaceRef:
    def test_a_response_loads_with_no_workspace_ref(self, messages):
        ran_elsewhere = [
            replace(message, workspace_ref=WorkspaceRef(provider="local", id="/somewhere/else")) if isinstance(message, ModelResponse) else message
            for message in messages
        ]
        record = ConversationRecord()
        record.record(snapshot("run-a", 1, ran_elsewhere), seq=0)
        assert [m.workspace_ref for m in ran_elsewhere if isinstance(m, ModelResponse)] == [
            WorkspaceRef(provider="local", id="/somewhere/else")
        ] * 2

        loaded = record.messages_at(run_id="run-a", seq=0)

        assert [m.workspace_ref for m in loaded if isinstance(m, ModelResponse)] == [None, None]

    def test_a_response_stored_with_no_workspace_ref_key_stays_on_its_form_when_sent_with_a_null_one(self, messages):
        class BeforeWorkspaceRefs(ConversationRecord):
            """A record that stores messages as they were before pydantic-ai wrote `workspace_ref` on every response."""

            def _place(self, key: str, message: MessageJson) -> StepEntry:
                return super()._place(key, {name: value for name, value in message.items() if name != "workspace_ref"})

        written = BeforeWorkspaceRefs()
        written.record(snapshot("run-written", 1, messages), seq=0)
        record = ConversationRecord(forms=written.forms, stamps=written.stamps)
        resent = [replace(message, workspace_ref=None) if isinstance(message, ModelResponse) else message for message in messages]

        record.record(snapshot("run-b", 1, resent), seq=0)

        assert record.new_forms == {}
        assert [entry.form for entry in record.steps[0].entries] == [0, 0, 0, 0]
        assert [entry.stamp for entry in record.steps[0].entries] == [0, 1, 0, 1]
