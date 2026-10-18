"""Run one OKF export: parse the knowledge base, resolve its links, project the concepts and write them.

`ExportOkfHandler` emits three events as it advances.
"""

from __future__ import annotations

import time

from importlib import metadata
from typing import TYPE_CHECKING

from core.cqrs.contracts import narrow_command
from core.observability import get_logger

from engram.export.domain import projector
from engram.export.domain.models import ConceptsProjected, ExportCompleted, ExportOkf, ExportStarted, OkfExportReport
from engram.indexing.contracts import resolve
from engram.kb.contracts import parse as parse_note

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from core.cqrs.contracts import Command, Event

    from engram.export.internal_contracts import BundleWriter, SourceLinkReader
    from engram.indexing.contracts import KnowledgeBaseReader
    from engram.kb.contracts import KnowledgeBaseNote, KnowledgeBaseSchema

_logger = get_logger(__name__)


def _exporter_actor() -> str:
    """The actor `engram/<installed version>`, or `engram/dev` when engram is not installed."""
    try:
        version = metadata.version("engram")
    except metadata.PackageNotFoundError:  # pragma: no cover (engram is installed in its own venv)
        version = "dev"
    return f"engram/{version}"


class ExportOkfHandler:
    """Handles `ExportOkf` and returns the export's `OkfExportReport`."""

    def __init__(
        self,
        kb_reader: KnowledgeBaseReader,
        bundle_writer: BundleWriter,
        *,
        schema: KnowledgeBaseSchema,
        source_links: SourceLinkReader,
        clock: Callable[[], float] = time.monotonic,
        generated_by: str | None = None,
    ) -> None:
        """Create the handler over its readers, its writer and the knowledge base's schema.

        Args:
            kb_reader: Reads the knowledge base's pages.
            bundle_writer: Writes the projected concepts to the export directory.
            schema: The knowledge base's schema. It decides which page types are exported, how ids are
                prefixed and which frontmatter fields name a page's source.
            source_links: Reads the deep link each mirrored source carries. It is read once per export,
                and only when the schema declares a source layer.
            clock: Monotonic seconds for the report's duration.
            generated_by: The actor stamped on every concept's `generated.by` and `verified[].by`. It
                defaults to `engram/<installed version>`.
        """
        self._kb_reader = kb_reader
        self._bundle_writer = bundle_writer
        self._schema = schema
        self._source_links = source_links
        self._clock = clock
        self._generated_by = generated_by if generated_by is not None else _exporter_actor()
        self.events: list[Event] = []

    async def __call__(self, command: Command) -> OkfExportReport:
        """Run `command` from the bus.

        Args:
            command: The dispatched command, which must be an `ExportOkf`.

        Returns:
            The export's report.
        """
        command = narrow_command(command, ExportOkf, handler="ExportOkfHandler")  # pragma: no cover (the bus routes by type)
        return await self.handle(command)

    async def handle(self, command: ExportOkf) -> OkfExportReport:
        """Parse, resolve, project and write one export.

        Args:
            command: The collection to export and the export directory.

        Returns:
            The export's counts and duration.
        """
        started = self._clock()
        self._emit(ExportStarted(knowledge_base_path=self._knowledge_base_path(), out_dir=command.out_dir))

        notes = self._parse_stage()
        resolved = resolve((note for note in notes), relations=self._schema.relations)
        result = projector.project_with_counts(
            resolved.notes,
            collection=command.collection,
            schema=self._schema,
            source_links=await self._load_source_links(),
            generated_by=self._generated_by,
        )
        self._emit(
            ConceptsProjected(
                concepts=len(result.bundle.concepts), skipped_meta=result.skipped_meta, relations=result.relations, dangling=result.dangling
            )
        )

        self._bundle_writer.write_bundle(result.bundle, command.out_dir)

        report = OkfExportReport(
            concepts=len(result.bundle.concepts),
            skipped_meta=result.skipped_meta,
            relations=result.relations,
            dangling=result.dangling,
            files_written=len(result.bundle.concepts) + 1,  # every concept and the root index.md
            duration_seconds=self._clock() - started,
        )
        self._emit(ExportCompleted(report=report))
        return report

    async def _load_source_links(self) -> Mapping[str, str]:
        """Every mirrored source's deep link by key, empty when the schema declares no source layer."""
        sources = self._schema.sources
        if not (sources.enabled and sources.key_field):
            return {}
        return await self._source_links.load_source_links()

    def _parse_stage(self) -> list[KnowledgeBaseNote]:
        """Parse every note, logging and skipping any that fails to parse."""
        notes: list[KnowledgeBaseNote] = []
        for document in self._kb_reader.unit_documents():
            try:
                notes.append(parse_note(document.text, source_path=document.path, spec=self._schema))
            except Exception as exc:  # noqa: BLE001 (one bad note is logged and skipped, never an abort)
                _logger.warning("Skipping unparseable note {path} for OKF export: {reason}", path=str(document.path), reason=str(exc))
        return notes

    def _knowledge_base_path(self) -> str:
        """The knowledge base root reported on `ExportStarted`."""
        return str(self._kb_reader.root)

    def _emit(self, event: Event) -> None:
        """Append an event to the bus-drained collector."""
        self.events.append(event)
