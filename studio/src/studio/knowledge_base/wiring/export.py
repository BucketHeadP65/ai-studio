"""What the export slice registers: the export handler, the writer of the OKF Knowledge Bundle, and the source-link reader.

The composition root routes the export command to the handler.
"""

from __future__ import annotations

from engram.export.contracts import BundleWriter, ExportOkfHandler, SourceLinkReader
from engram.export.infrastructure.writer import FilesystemBundleWriter
from engram.indexing.contracts import KnowledgeBaseReader
from engram.kb.contracts import KnowledgeBaseSchema
from engram.sources.infrastructure.mirror import ObsidianMirrorStore
from wireup import injectable

from studio.knowledge_base.settings import KnowledgeBaseSettings


@injectable
def export_handler_provider(
    reader: KnowledgeBaseReader, writer: BundleWriter, schema: KnowledgeBaseSchema, source_links: SourceLinkReader
) -> ExportOkfHandler:
    """Provide the export handler, over the same knowledge base reader and schema the index uses.

    Args:
        reader: The reader of the knowledge base's notes.
        writer: Writes the export directory.
        schema: The knowledge base's schema.
        source_links: Reads what a page's `sources` entry links to.

    Returns:
        The export handler.
    """
    return ExportOkfHandler(reader, writer, schema=schema, source_links=source_links)


@injectable
def source_link_reader_provider(knowledge_base: KnowledgeBaseSettings) -> SourceLinkReader:
    """Provide the knowledge base's mirror store as the export's source-link reader.

    Args:
        knowledge_base: The knowledge base whose mirror store is read.

    Returns:
        The source-link reader.
    """
    return ObsidianMirrorStore(knowledge_base)


@injectable
def bundle_writer_provider() -> BundleWriter:
    """Provide the filesystem writer of the OKF Knowledge Bundle, the only port of the export that writes files.

    Returns:
        The writer.
    """
    return FilesystemBundleWriter()


REGISTRATIONS: list[object] = [export_handler_provider, source_link_reader_provider, bundle_writer_provider]
"""Everything this slice puts in the container, each under the type its consumers declare."""
