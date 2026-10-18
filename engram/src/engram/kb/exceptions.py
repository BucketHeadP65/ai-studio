"""The errors the note grammar and the knowledge base schema raise."""

from __future__ import annotations


class NoteGrammarError(Exception):
    """A string could not be parsed against the note grammar (e.g. a bad unit link)."""


class FrontmatterError(NoteGrammarError):
    """Frontmatter could not be discriminated or validated (e.g. an unknown `type`)."""


class ClaimParseError(NoteGrammarError):
    """A claim block is structurally present but cannot be represented as a `ClaimBlock`.

    It is raised for a claim without `loc::`, a link-scalar field with more than one link, a multilink field whose
    text holds no `[[page]]` link, and a key the knowledge base's claim grammar does not declare.
    """


class KnowledgeBaseSchemaMissingError(ValueError):
    """A knowledge base carries no `knowledge-base-schema.json`, and there is no default ontology to guess at."""
