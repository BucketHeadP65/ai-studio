"""The authoring slice's reader of the registry of knowledge bases in a settings file."""

from __future__ import annotations

import json

from pathlib import Path
from typing import Any

from core.observability import get_logger
from pydantic import ValidationError

from engram.authoring.domain import AuthoringKnowledgeBase, RegisteredKnowledgeBases
from engram.kb.contracts import KnowledgeBaseSchema

_logger = get_logger(__name__)


def _registered_schema(name: str, root: Path) -> KnowledgeBaseSchema | None:
    """`root`'s own schema, or `None` when it carries none or fails to load."""
    schema_path = root / "knowledge-base-schema.json"
    if not schema_path.is_file():
        _logger.warning("doc_authoring_knowledge_bases: skipping {knowledge_base!r}, it carries no knowledge-base-schema.json", knowledge_base=name)
        return None
    try:
        return KnowledgeBaseSchema.from_path(schema_path)
    except (OSError, json.JSONDecodeError, ValidationError) as error:
        _logger.warning(
            "doc_authoring_knowledge_bases: skipping {knowledge_base!r}, its knowledge-base-schema.json did not load: {reason}",
            knowledge_base=name,
            reason=str(error),
        )
        return None


def doc_authoring_knowledge_bases(settings_path: Path) -> RegisteredKnowledgeBases:
    """Every registered knowledge base whose own schema opts into both sharing and documentation authoring.

    A row becomes a knowledge base when the row itself does not declare `"private": true`, its
    resolved schema does not declare `retrieval.private: true`, and that same schema sets
    `retrieval.doc_authoring`. A row with no `knowledge-base-schema.json`, a malformed row (not a
    mapping, or no string `path`), or a `knowledge-base-schema.json` that fails to load is skipped
    and logged. It never takes the rest of the registry down with it.

    Args:
        settings_path: The settings JSON holding the registry of knowledge bases (its `knowledge_bases` map).

    Returns:
        Every qualifying knowledge base as an `AuthoringKnowledgeBase`, plus whether the settings file itself was
        reachable (see `RegisteredKnowledgeBases`).
    """
    if not settings_path.is_file():
        return RegisteredKnowledgeBases(knowledge_bases=(), reachable=True)
    try:
        settings: dict[str, Any] = json.loads(settings_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        _logger.warning("doc_authoring_knowledge_bases: {path} exists but could not be read: {reason}", path=str(settings_path), reason=str(error))
        return RegisteredKnowledgeBases(knowledge_bases=(), reachable=False)
    knowledge_bases: list[AuthoringKnowledgeBase] = []
    for name, entry in (settings.get("knowledge_bases") or {}).items():
        if not isinstance(entry, dict) or not isinstance(path := entry.get("path"), str):
            continue
        if entry.get("private") is True:
            continue
        root = Path(path).expanduser()
        schema = _registered_schema(name, root)
        if schema is None or schema.retrieval.private or not schema.retrieval.doc_authoring:
            continue
        knowledge_bases.append(AuthoringKnowledgeBase(name=name, collection=schema.retrieval.collection, path=root, schema=schema))
    return RegisteredKnowledgeBases(knowledge_bases=tuple(knowledge_bases), reachable=True)
