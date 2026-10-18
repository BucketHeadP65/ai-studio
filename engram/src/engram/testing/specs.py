"""Fixture `KnowledgeBaseSchema`s for test suites, the two sources a test ever needs.

The runtime library refuses to guess a spec (`engram.kb.contracts.resolve_schema`,
`load_knowledge_base_schema`): a real knowledge base always carries its own `knowledge-base-schema.json`. A test that needs a
concrete spec takes one from here rather than hand-rolling page types or reaching into a template's
JSON file directly.
"""

from __future__ import annotations

from functools import lru_cache

from engram.init.contracts import load_template
from engram.kb.contracts import KnowledgeBaseSchema


@lru_cache(maxsize=1)
def minimal_spec() -> KnowledgeBaseSchema:
    """A small, self-contained spec: two generic page types, no sources, no claims.

    Built in code so a test exercising the note grammar against a domain-neutral knowledge base never
    depends on any template's JSON.

    Returns:
        A validated `KnowledgeBaseSchema` with `note` and `entry` page_types.
    """
    return KnowledgeBaseSchema.model_validate(
        {
            "slug": "minimal",
            "page_types": [
                {"page_type": "note", "id_prefix": "note", "folder": "Units/Notes"},
                {"page_type": "entry", "id_prefix": "entry", "folder": "Units/Entries"},
            ],
        }
    )


@lru_cache(maxsize=1)
def research_spec() -> KnowledgeBaseSchema:
    """The `research` template's spec, read from its own `knowledge-base-schema.json`.

    Goes through `engram.init.contracts.load_template` rather than a hardcoded path, so this tracks
    the template wherever `ENGRAM_TEMPLATES` or the repo layout puts it.

    Returns:
        The `research` template's validated `KnowledgeBaseSchema`, with its `_meta` guidance pages merged
        in, for parity with a knowledge base actually started from this template.
    """
    template = load_template("research")
    return KnowledgeBaseSchema.from_path(template.path / "knowledge-base-schema.json")
