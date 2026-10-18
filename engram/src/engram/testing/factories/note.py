"""Polyfactory factories for the note grammar models.

Each factory builds a valid instance of its model. The frontmatter factories pin `id` and `type`
to a matching pair, so the schema's id prefix check passes. The `UnitLink` factory builds a bare
slug-shaped target.

Every frontmatter factory builds `BaseFrontmatter` through `parse_frontmatter`. Read a
type-specific field (for example `fm.year`) off `fm.model_extra`.
"""

from __future__ import annotations

from polyfactory.factories.pydantic_factory import ModelFactory

from engram.kb.contracts import BaseFrontmatter, ClaimBlock, KnowledgeBaseNote, Section, UnitLink
from engram.testing.specs import research_spec


class UnitLinkFactory(ModelFactory[UnitLink]):
    """A `UnitLink` with a bare target: no alias, heading or block."""

    alias = None
    heading = None
    block = None


class PaperFrontmatterFactory(ModelFactory[BaseFrontmatter]):
    """Builds a valid `paper` frontmatter through `parse_frontmatter`."""

    id = "paper.sample-2025"
    type = "paper"
    status = "draft"

    @classmethod
    def build(cls, **kwargs: object) -> BaseFrontmatter:  # type: ignore[override]
        """Parse a `paper` frontmatter from minimal paper data against the research schema.

        Args:
            **kwargs: Frontmatter fields that replace or add to the defaults.

        Returns:
            The parsed frontmatter.
        """
        from engram.kb.contracts import parse_frontmatter

        data: dict[str, object] = {
            "id": "paper.sample-2025",
            "type": "paper",
            "title": "Sample Paper",
            "status": "draft",
            "paper_type": "method",
            "year": 2025,
            "zotero_key": "K",
            "source_hash": "h",
        }
        data.update(kwargs)
        return parse_frontmatter(data, research_spec())


class EntityFrontmatterFactory(ModelFactory[BaseFrontmatter]):
    """Builds a valid `entity` frontmatter through `parse_frontmatter`."""

    @classmethod
    def build(cls, **kwargs: object) -> BaseFrontmatter:  # type: ignore[override]
        """Parse an `entity` frontmatter from minimal data against the research schema.

        Args:
            **kwargs: Frontmatter fields that replace or add to the defaults.

        Returns:
            The parsed frontmatter.
        """
        from engram.kb.contracts import parse_frontmatter

        data: dict[str, object] = {
            "id": "ent.sample",
            "type": "entity",
            "title": "Sample Entity",
            "status": "draft",
            "entity_type": "technique",
        }
        data.update(kwargs)
        return parse_frontmatter(data, research_spec())


class ComparisonFrontmatterFactory(ModelFactory[BaseFrontmatter]):
    """Builds a valid `comparison` frontmatter through `parse_frontmatter`."""

    @classmethod
    def build(cls, **kwargs: object) -> BaseFrontmatter:  # type: ignore[override]
        """Parse a `comparison` frontmatter from minimal data against the research schema.

        Args:
            **kwargs: Frontmatter fields that replace or add to the defaults.

        Returns:
            The parsed frontmatter.
        """
        from engram.kb.contracts import parse_frontmatter

        data: dict[str, object] = {"id": "cmp.sample", "type": "comparison", "title": "Sample Comparison", "status": "draft"}
        data.update(kwargs)
        return parse_frontmatter(data, research_spec())


class QuestionFrontmatterFactory(ModelFactory[BaseFrontmatter]):
    """Builds a valid `question` frontmatter through `parse_frontmatter`."""

    @classmethod
    def build(cls, **kwargs: object) -> BaseFrontmatter:  # type: ignore[override]
        """Parse a `question` frontmatter from minimal data against the research schema.

        Args:
            **kwargs: Frontmatter fields that replace or add to the defaults.

        Returns:
            The parsed frontmatter.
        """
        from engram.kb.contracts import parse_frontmatter

        data: dict[str, object] = {"id": "q.sample", "type": "question", "title": "Sample Question?", "status": "draft"}
        data.update(kwargs)
        return parse_frontmatter(data, research_spec())


class ClaimBlockFactory(ModelFactory[ClaimBlock]):
    """A `ClaimBlock` anchored at `^c1` with an `observation` page type."""

    anchor = "^c1"
    loc = "§1.1"
    page_type = "observation"


class SectionFactory(ModelFactory[Section]):
    """A level-two `Section`."""

    level = 2


class KnowledgeBaseNoteFactory(ModelFactory[KnowledgeBaseNote]):
    """A `KnowledgeBaseNote` whose frontmatter is a valid `paper` frontmatter."""

    @classmethod
    def frontmatter(cls) -> BaseFrontmatter:
        """Build the note's frontmatter.

        Returns:
            A valid `paper` frontmatter.
        """
        return PaperFrontmatterFactory.build()
