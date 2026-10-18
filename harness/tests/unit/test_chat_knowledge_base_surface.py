"""The chat agent's knowledge base surface: the two reads plus `request_source`, nothing else.

`composition.harness_session` narrows the same `RetrievalKnowledgeBase` a step reaches in full down to
`CHAT_KNOWLEDGE_BASE_PLUGINS` for the `knowledge-base` plugin. Proved here against the same production surface
`tests/support/grants.py` builds for the step-grant tests -- so a rename, or a fifth authoring
tool landing on the knowledge base, cannot silently widen or narrow the chat entry without this failing.
"""

from __future__ import annotations

from pydantic_ai.models.test import TestModel
from pydantic_ai.tools import RunContext
from pydantic_ai.usage import RunUsage

from harness.composition import CHAT_KNOWLEDGE_BASE_PLUGINS
from harness.knowledge_base import KnowledgeBase
from tests.support.grants import app_knowledge_base_surface

OWNER_PLUGINS = frozenset({"source_queue_policy", "write_policy", "source_queue_rows", "source_queue_decide", "decide_page"})


def _ctx() -> RunContext[None]:
    return RunContext(deps=None, model=TestModel(), usage=RunUsage())


def test_the_chat_surface_is_the_two_reads_request_source_and_the_owner_doors() -> None:
    assert frozenset({"search", "fetch", "request_source"}) | OWNER_PLUGINS == CHAT_KNOWLEDGE_BASE_PLUGINS


async def test_the_narrowed_toolset_offers_exactly_that_and_nothing_else() -> None:
    knowledge_base = KnowledgeBase[None](knowledge_base=app_knowledge_base_surface(), only=CHAT_KNOWLEDGE_BASE_PLUGINS)
    toolset = knowledge_base.get_toolset()

    names = set(await toolset.get_tools(_ctx()))

    assert names == {"search", "fetch", "request_source"} | OWNER_PLUGINS


async def test_write_note_list_undigested_and_read_source_stay_off_the_chat_surface() -> None:
    knowledge_base = KnowledgeBase[None](knowledge_base=app_knowledge_base_surface(), only=CHAT_KNOWLEDGE_BASE_PLUGINS)
    toolset = knowledge_base.get_toolset()

    names = set(await toolset.get_tools(_ctx()))

    assert {"write_note", "list_undigested", "read_source"}.isdisjoint(names)
