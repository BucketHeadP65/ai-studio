"""The schema of the agent-memory knowledge base, read from a file bundled with engram."""

from functools import lru_cache
from importlib import resources

from engram.kb.contracts import KnowledgeBaseSchema


@lru_cache(maxsize=1)
def agent_memory_schema() -> KnowledgeBaseSchema:
    """Return the schema of the agent-memory knowledge base: one page type the agent writes, with a live and retired lifecycle.

    Returns:
        The schema, read once and cached.
    """
    text = resources.files("engram.agent_memory").joinpath("agent-memory.json").read_text(encoding="utf-8")
    return KnowledgeBaseSchema.from_json(text)
