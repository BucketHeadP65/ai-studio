"""What the chat panel is shown, and why it has to be what the model is sent.

This file exists because of a bug that made a working agent look like it was hallucinating. The
app resumes the last run's messages at boot -- that is the entire point of persisting steps -- but
the page started with an empty log. So the person saw a fresh conversation while the model held
yesterday's, and asked "tell me what workflows exist" it volunteered that none of them target RAG
or GraphRAG. Nothing was wrong with the model: a question about retrieval-augmented generation was
two messages up in the history it was sent. A UI that hides the history cannot be debugged, because
every symptom of it looks like a model defect.

So the claim under test is one sentence: **`/api/conversation` shows exactly the dialogue the next
turn will carry.** A reply is prose, so the panel shows a text part exactly as the model wrote it.
"""

from __future__ import annotations

import json

from pydantic_ai.messages import ModelRequest, ModelResponse, TextPart, ToolCallPart, ToolReturnPart, UserPromptPart

from harness.features.authoring.controller import _turns


def _answered(text: str) -> ModelResponse:
    """A finished turn's own text part, exactly as the transcript holds it."""
    return ModelResponse(parts=[TextPart(text)])


def _asked(text: str) -> ModelRequest:
    return ModelRequest(parts=[UserPromptPart(content=text)])


def test_the_dialogue_survives_the_round_trip() -> None:
    """Both sides, in order, labelled the way the panel labels them."""
    history = [
        _asked("what is in the library?"),
        _answered("The store holds **4 workflows**."),
        _asked("tell me what workflows exist"),
    ]
    assert _turns(history) == [
        {"who": "you", "text": "what is in the library?"},
        {"who": "agent", "text": "The store holds **4 workflows**."},
        {"who": "you", "text": "tell me what workflows exist"},
    ]


def test_prose_that_is_not_json_is_shown_as_itself() -> None:
    """A mid-turn narration is a plain text part, and is not dropped."""
    assert _turns([ModelResponse(parts=[TextPart("Let me check the store first.")])]) == [
        {"who": "agent", "text": "Let me check the store first."}
    ]


def test_a_json_object_is_shown_verbatim() -> None:
    """A model that writes JSON is showing the owner what it wrote, whatever keys the object carries."""
    for literal in (
        '{"message": "hi", "other": 42}',
        json.dumps({"message": "Here is a draft.", "spec_yaml": "name: dig\nversion: 1\n", "title": "Dig once", "summary": "", "applicability": ""}),
    ):
        assert _turns([_answered(literal)]) == [{"who": "agent", "text": literal}]


def test_tool_traffic_stays_out_of_the_panel() -> None:
    """Forty `search` calls would bury the conversation this endpoint exists to make legible.

    Logfire is where the calls are reviewed. The panel answers a different question: what does this
    agent think we have been talking about.
    """
    history = [
        _asked("what is in the store?"),
        ModelResponse(parts=[ToolCallPart(tool_name="run_sql", args={"target": "workflows", "sql": "select id from api.cards"})]),
        ModelRequest(parts=[ToolReturnPart(tool_name="run_sql", content="wf.one, wf.two", tool_call_id="c1")]),
        _answered("Two of them."),
    ]
    assert _turns(history) == [{"who": "you", "text": "what is in the store?"}, {"who": "agent", "text": "Two of them."}]


def test_an_empty_reply_is_not_an_empty_bubble() -> None:
    """A turn that ended with nothing to say should leave no message, not a blank one."""
    assert _turns([_answered("   ")]) == []


def test_a_first_boot_has_nothing_to_show() -> None:
    """No history is the normal state of a fresh install, and it is not an error."""
    assert _turns([]) == []
