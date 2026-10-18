"""The partition a knowledge base writes into, resolved once for every writer.

An absent `--collection` must read the knowledge base's own spec. A default that named a literal partition
wrote one knowledge base's rows into another knowledge base's partition, where the incremental indexer deletes them
as vanished, so these pin that the fallback is the spec and never a name.
"""

from __future__ import annotations

from engram.kb.contracts import KnowledgeBaseSchema, PageTypeSpec
from engram.kb.domain.schema import RetrievalSpec

from studio.knowledge_base.cli_support import resolve_partition


def _spec(collection: str) -> KnowledgeBaseSchema:
    """A minimal spec whose only interesting field is the partition it declares."""
    return KnowledgeBaseSchema(
        slug="field-notes",
        purpose="A knowledge base outside the research partition.",
        page_types=(PageTypeSpec(page_type="doc", id_prefix="", folders=("docs",)),),
        retrieval=RetrievalSpec(collection=collection),
    )


def test_an_absent_choice_reads_the_knowledge_base_own_partition() -> None:
    assert resolve_partition(None, _spec("field-notes")) == "field-notes"


def test_an_absent_choice_never_falls_back_to_a_literal_name() -> None:
    # The whole defect in one assertion: a knowledge base declaring its own partition must not be written
    # into any other, whatever the flag's default used to be.
    assert resolve_partition(None, _spec("field-notes")) != "research"


def test_an_explicit_choice_wins_over_the_spec() -> None:
    assert resolve_partition("research", _spec("field-notes")) == "research"


def test_a_research_knowledge_base_still_resolves_to_research() -> None:
    assert resolve_partition(None, _spec("research")) == "research"


def test_init_all_declares_no_default_partition() -> None:
    # The defect was the flag's own default, not the resolver, so pin the flag. `init all` must
    # arrive at the resolver with None whenever the operator did not name a partition.
    import typer

    from studio.knowledge_base.cli_init import init_app

    command = typer.main.get_group(init_app).commands["all"]
    option = next(param for param in command.params if "--collection" in param.opts)
    assert option.default is None
