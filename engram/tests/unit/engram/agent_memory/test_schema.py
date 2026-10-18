"""The agent-memory grammar: one note page_type, category and source fields, live/retired lifecycle."""

from engram.agent_memory.contracts import agent_memory_schema
from engram.kb.contracts import AuthoringPayload


def test_schema_declares_the_memory_page_type() -> None:
    schema = agent_memory_schema()
    assert schema.slug == "agent-memory"
    assert schema.retrieval.collection == "agent-memory"
    (page_type,) = schema.page_types
    assert page_type.page_type == "memory"
    assert page_type.id_prefix == "mem."
    assert page_type.status_values == ("live", "retired")


def test_a_memory_note_validates_under_the_schema() -> None:
    payload = AuthoringPayload.model_validate(
        {
            "frontmatter": {
                "id": "mem.global.main.world.postgres-port",
                "type": "memory",
                "title": "Local Postgres port",
                "summary": "The local Postgres answers on 5433.",
                "status": "live",
                "category": "world",
                "source": "session",
            },
            "sections": [{"heading": "Note", "body": "The local Postgres answers on 5433, not the default 5432."}],
            "knowledge_base_schema": agent_memory_schema(),
        }
    )
    assert payload.frontmatter["category"] == "world"


def test_a_wrong_category_is_refused() -> None:
    import pytest

    with pytest.raises(ValueError, match="category"):
        AuthoringPayload.model_validate(
            {
                "frontmatter": {
                    "id": "mem.x",
                    "type": "memory",
                    "title": "x",
                    "summary": "x",
                    "status": "live",
                    "category": "gossip",
                },
                "knowledge_base_schema": agent_memory_schema(),
            }
        )
