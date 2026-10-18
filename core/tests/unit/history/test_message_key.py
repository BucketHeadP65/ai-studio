from pydantic_ai.messages import ModelRequest, ModelResponse, TextPart, ToolAvailabilityDeltaPart, ToolReturnPart, UserPromptPart

from core.ai.history.message_key import message_key
from tests.unit.history.conftest import AT, turn_messages


class TestMessageKey:
    def test_is_32_hex_characters(self, messages):
        key = message_key(messages[0])

        assert len(key) == 32
        assert int(key, 16) >= 0

    def test_survives_a_rewrite_of_the_content(self):
        original = turn_messages()[2]
        cleared = turn_messages(cleared=True)[2]

        assert message_key(original) == message_key(cleared)

    def test_differs_by_tool_call_id(self):
        one = ModelRequest(parts=[ToolReturnPart(tool_name="t", content="x", tool_call_id="call_1", timestamp=AT)])
        two = ModelRequest(parts=[ToolReturnPart(tool_name="t", content="x", tool_call_id="call_2", timestamp=AT)])

        assert message_key(one) != message_key(two)

    def test_ignores_a_request_timestamp_and_run_id(self):
        base = ModelRequest(parts=[UserPromptPart(content="hi", timestamp=AT)])
        restamped = ModelRequest(parts=[UserPromptPart(content="hi", timestamp=AT)], run_id="run-9")

        assert message_key(base) == message_key(restamped)

    def test_a_part_id_names_a_part_that_carries_one(self):
        original = ModelResponse(parts=[TextPart(content="a", id="part_1")], timestamp=AT)
        rewritten = ModelResponse(parts=[TextPart(content="b", id="part_1")], timestamp=AT)
        other = ModelResponse(parts=[TextPart(content="a", id="part_2")], timestamp=AT)

        assert message_key(original) == message_key(rewritten)
        assert message_key(original) != message_key(other)

    def test_a_part_with_no_name_is_known_by_its_position(self):
        one = ModelRequest(parts=[UserPromptPart(content="hi", timestamp=AT), ToolAvailabilityDeltaPart(tools_added=["lookup_city"])])
        two = ModelRequest(parts=[UserPromptPart(content="hi", timestamp=AT), ToolAvailabilityDeltaPart(tools_added=["other_tool"])])

        assert message_key(one) == message_key(two)

    def test_a_message_with_nothing_stable_falls_back_to_content(self):
        one = ModelRequest(parts=[ToolAvailabilityDeltaPart(tools_added=["lookup_city"])])
        two = ModelRequest(parts=[ToolAvailabilityDeltaPart(tools_added=["other_tool"])])

        assert message_key(one) != message_key(two)
