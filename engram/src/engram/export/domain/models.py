"""The export's command, the events it emits and the concepts it projects.

OKF v0.2 is the target format. A Knowledge Bundle is a directory of UTF-8 Markdown files with YAML
frontmatter. A concept's identity is its file path minus `.md`, and its one required frontmatter field
is a non-empty `type`. Relations are ordinary Markdown links. A consumer must tolerate unknown keys, so
the `okf_x_*` overlay keeps the export directory conformant and lossless.
"""

from __future__ import annotations

from core.cqrs.contracts import Command, Event
from pydantic import BaseModel, ConfigDict, JsonValue

__all__ = [
    "ConceptsProjected",
    "ExportCompleted",
    "ExportOkf",
    "ExportStarted",
    "OkfBundle",
    "OkfConcept",
    "OkfExportReport",
]


class ExportOkf(Command):
    """Project one collection into an OKF v0.2 Knowledge Bundle on disk."""

    collection: str
    """The knowledge base partition to export, the same partition key `IndexKnowledgeBase` carries."""
    out_dir: str
    """The root of the export directory the writer writes under."""


class ExportStarted(Event):
    """An export began, naming the knowledge base root and the export directory."""

    knowledge_base_path: str
    out_dir: str


class ConceptsProjected(Event):
    """The projection finished, with how many concepts it produced and how many meta pages it skipped."""

    concepts: int
    skipped_meta: int
    relations: int
    """How many page-level typed edges landed in some concept's `okf_x_relations`, counting only edges whose target was exported."""
    dangling: int
    """How many body unit links resolved to nothing exported and were flattened to plain text."""


class ExportCompleted(Event):
    """The export finished, carrying its full `OkfExportReport`."""

    report: OkfExportReport


class OkfConcept(BaseModel):
    """One projected OKF concept, a single `.md` file in the export directory."""

    model_config = ConfigDict(frozen=True)

    path: str
    """The file path relative to the export directory, with the `.md` suffix, for example `topics/example.md`. The concept id is this path minus `.md`."""
    frontmatter: dict[str, JsonValue]
    """The YAML block the writer writes. It always carries a non-empty `type`."""
    body: str
    """The Markdown body: the canonical sections with their unit links rewritten, then the `## Relations` section."""


class OkfBundle(BaseModel):
    """A whole OKF v0.2 Knowledge Bundle: the concept files and the root `index.md`."""

    model_config = ConfigDict(frozen=True)

    concepts: tuple[OkfConcept, ...] = ()
    index: OkfConcept
    """The root `index.md`, whose frontmatter is `okf_version: "0.2"` alone and whose body lists every concept by folder."""


class OkfExportReport(BaseModel):
    """The counts and duration of one export, which the CLI prints."""

    model_config = ConfigDict(frozen=True)

    concepts: int = 0
    skipped_meta: int = 0
    relations: int = 0
    dangling: int = 0
    files_written: int = 0
    """The concept count plus one, for the root `index.md`."""
    duration_seconds: float = 0.0
