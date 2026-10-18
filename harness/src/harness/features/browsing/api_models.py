"""The request body the browsing routes read."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from harness.domain.contracts import WorkflowPresentationPatch


class WorkflowCardPatch(BaseModel):
    """What `redescribe` rewrites: the words a person reads to choose a workflow, never the workflow itself.

    Every field is optional and **absent means unchanged**, so a form that owns one box sends that
    box alone and the rest of the card stays exactly as the library holds it. `presentation` is a
    patch one level down, with the same rule: the sentences it names are replaced and the others
    are kept.
    """

    model_config = ConfigDict(extra="forbid", use_attribute_docstrings=True)

    title: str | None = None
    """The title a person reads in the library listing. A supplied title the readiness check refuses stops the write."""
    summary: str | None = None
    """The summary a person reads under the title. A supplied summary the readiness check refuses stops the write."""
    applicability: str | None = None
    """When this workflow applies, in the author's words."""
    presentation: WorkflowPresentationPatch | None = None
    """The author's own words to replace. A supplied sentence the readiness check refuses stops the write."""
