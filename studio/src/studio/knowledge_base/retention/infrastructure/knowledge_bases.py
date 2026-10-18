"""The lookup from a registry name to the knowledge base retention writes into, read from the settings file."""

from __future__ import annotations

from typing import TYPE_CHECKING

from engram.authoring.contracts import AuthoringKnowledgeBase
from engram.kb.contracts import KnowledgeBaseSchema

from studio.knowledge_base.retention.domain import RetentionRefusal
from studio.knowledge_base.retention.exceptions import RetentionRefusedError
from studio.knowledge_base.settings import read_registry

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path


def knowledge_base_reader(settings_path: Path) -> Callable[[str], AuthoringKnowledgeBase]:
    """Build the lookup from a registry name to its knowledge base, reading the registry and the schema again on every call.

    The lookup raises `RetentionRefusedError` for a name nothing registers or a schema that will not load.

    Args:
        settings_path: The settings file that holds the registry of knowledge bases.

    Returns:
        The lookup.
    """

    def read(name: str) -> AuthoringKnowledgeBase:
        valid, _broken = read_registry(settings_path)
        entry = valid.get(name)
        if entry is None:
            msg = f"no knowledge base named {name!r} is registered"
            raise RetentionRefusedError(RetentionRefusal.UNKNOWN_KNOWLEDGE_BASE, msg)
        schema_path = entry.path / "knowledge-base-schema.json"
        try:
            schema = KnowledgeBaseSchema.from_path(schema_path)
        except (OSError, ValueError) as error:
            msg = f"{name!r} carries no schema this app can read at {schema_path}: {error}"
            raise RetentionRefusedError(RetentionRefusal.UNREADABLE_SCHEMA, msg) from error
        return AuthoringKnowledgeBase(name=name, collection=schema.retrieval.collection, path=entry.path, schema=schema)

    return read
