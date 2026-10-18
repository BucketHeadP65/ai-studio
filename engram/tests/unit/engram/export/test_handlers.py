r"""Unit tests for the export handler (`src/engram/export/service.py`).

The handler is the four-stage parse → resolve → project → write orchestrator. These
specs drive it entirely through fakes — :class:`FakeKnowledgeBaseReader` (in-memory knowledge base
documents), :class:`RecordingBundleWriter` (captures the written bundle) and
:class:`FakeSourceLinks` (a scripted source-link map) — so there is no database, no network,
and no real filesystem. Every fixture belongs to the made-up knowledge base this module declares, so
nothing here depends on a bundle's vocabulary. They pin:

* the happy path: the report counts concepts / skipped meta / relations / files, and
  the recorded bundle holds exactly the projected concepts at the right `out_dir`;
* the mirror read: the deep link the source reader supplies is the one that lands in a
  page's `sources` entry, and a knowledge base with no declared source key never reads at all;
* parse fault-tolerance: an unparseable note is skipped and the export still completes;
* the event sequence the bus drains (ExportStarted → ConceptsProjected → ExportCompleted)
  and that the bus routes `ExportOkf` to the handler end to end.
"""

from __future__ import annotations

from core.cqrs.contracts import MessageBus

from engram.export.contracts import ConceptsProjected, ExportCompleted, ExportOkf, ExportOkfHandler, ExportStarted, OkfExportReport
from engram.kb.contracts import KnowledgeBaseSchema
from engram.testing.fakes.bundle_writer import RecordingBundleWriter
from engram.testing.fakes.knowledge_base_reader import FakeKnowledgeBaseReader
from engram.testing.fakes.source_links import FakeSourceLinks

_SPEC = KnowledgeBaseSchema.model_validate(
    {
        "version": "0.1",
        "slug": "fieldguide",
        "page_types": [
            {
                "page_type": "record",
                "id_prefix": "rec.",
                "folder": "Vault/Records",
                "frontmatter_fields": [{"name": "describes", "type": "link-list"}],
                "edge_fields": ["describes"],
            },
            {"page_type": "specimen", "id_prefix": "spc.", "folder": "Vault/Specimens"},
            {"page_type": "meta", "id_prefix": "meta.", "folder": "Vault/_meta"},
        ],
        "sources": {
            "enabled": True,
            "connectors": ["catalogue"],
            "key_field": "catalogue_key",
            "digest_link_field": "catalogued_in",
            "hash_field": "catalogue_hash",
            "digest_types": {"article": "record"},
        },
    }
)

_RECORD = """\
---
id: rec.coastal-survey
type: record
title: Coastal survey
summary: What the tide pools held.
status: synthesized
updated: 2026-06-10
catalogue_key: CAT-1
catalogue_hash: bd72
describes: ["[[limpet]]"]
---

## Overview

The survey counted [[limpet]] colonies.
"""

_SPECIMEN = """\
---
id: spc.limpet
type: specimen
title: Limpet
status: draft
---

## Overview

A grazing marine snail.
"""

_META = """\
---
id: meta.dashboard
type: meta
title: Dashboard
status: verified
---

## Body

Not knowledge.
"""

# A note whose YAML is not a mapping at all: the parser raises, and the handler skips it.
_BROKEN = """\
---
- just
- a
- list
---

## Overview

No frontmatter mapping here.
"""

# A note whose YAML is clean but whose id-prefix is wrong for its page_type (a `spc.` id on a
# `record`): the frontmatter model_validator raises — a NON-grammar failure, so the
# handler's fault-tolerance must catch broadly.
_BAD_PREFIX = """\
---
id: spc.mislabelled
type: record
title: Mislabelled
status: draft
---

## Overview

A record note carrying a specimen-style id.
"""


def _handler(
    reader: FakeKnowledgeBaseReader, writer: RecordingBundleWriter, *, source_links: FakeSourceLinks | None = None, spec: KnowledgeBaseSchema = _SPEC
) -> ExportOkfHandler:
    """Build the handler over the fakes with a deterministic two-tick clock (duration = 2.5s).

    `source_links` defaults to a fake with an empty map, the "mirror carries no link for anything"
    case most of these specs want.
    """
    ticks = iter([10.0, 12.5])
    return ExportOkfHandler(reader, writer, schema=spec, source_links=source_links or FakeSourceLinks(), clock=lambda: next(ticks))


def _knowledge_base() -> FakeKnowledgeBaseReader:
    """A three-note knowledge_base: a record, a specimen, a meta page (skipped), all well-formed."""
    return FakeKnowledgeBaseReader(
        [("Vault/Records/coastal-survey.md", _RECORD), ("Vault/Specimens/limpet.md", _SPECIMEN), ("Vault/_meta/dashboard.md", _META)]
    )


class TestHappyPath:
    async def test_report_counts_concepts_skipped_meta_and_files(self):
        writer = RecordingBundleWriter()
        handler = _handler(_knowledge_base(), writer)

        report = await handler.handle(ExportOkf(collection="fieldguide", out_dir="out/bundle"))

        assert isinstance(report, OkfExportReport)
        assert report.concepts == 2  # record + specimen; the meta page is skipped
        assert report.skipped_meta == 1
        assert report.relations == 1  # the record's describes → spc.limpet resolves to an exported page
        assert report.files_written == 3  # 2 concepts + the root index.md
        assert report.duration_seconds == 2.5

    async def test_writer_receives_the_projected_bundle_at_out_dir(self):
        writer = RecordingBundleWriter()
        handler = _handler(_knowledge_base(), writer)

        await handler.handle(ExportOkf(collection="fieldguide", out_dir="out/bundle"))

        assert writer.out_dirs == ["out/bundle"]
        files = writer.files()
        assert set(files) == {"records/coastal-survey.md", "specimens/limpet.md", "index.md"}

    async def test_collection_flows_into_the_engram_overlay(self):
        writer = RecordingBundleWriter()
        handler = ExportOkfHandler(_knowledge_base(), writer, schema=_SPEC, source_links=FakeSourceLinks())

        await handler.handle(ExportOkf(out_dir="out/bundle", collection="fieldguide"))

        bundle = writer.bundles[-1]
        record = next(c for c in bundle.concepts if c.path == "records/coastal-survey.md")
        engram = record.frontmatter["okf_x_engram"]
        assert isinstance(engram, dict)
        assert engram["collection"] == "fieldguide"


class TestSourceLinks:
    async def test_the_mirrors_link_lands_in_the_pages_sources_entry(self):
        writer = RecordingBundleWriter()
        links = FakeSourceLinks({"CAT-1": "catalogue://items/CAT-1"})
        handler = _handler(_knowledge_base(), writer, source_links=links)

        await handler.handle(ExportOkf(collection="fieldguide", out_dir="out/bundle"))

        record = next(c for c in writer.bundles[-1].concepts if c.path == "records/coastal-survey.md")
        assert record.frontmatter["sources"] == [
            {"id": "CAT-1", "resource": "catalogue://items/CAT-1", "title": "Coastal survey"}
        ]
        assert links.reads == 1  # read once per export, not once per page

    async def test_a_knowledge_base_with_no_declared_source_key_never_reads_the_mirror(self):
        writer = RecordingBundleWriter()
        links = FakeSourceLinks({"CAT-1": "catalogue://items/CAT-1"})
        sourceless = _SPEC.model_copy(update={"sources": _SPEC.sources.model_copy(update={"enabled": False, "key_field": ""})})
        handler = _handler(_knowledge_base(), writer, source_links=links, spec=sourceless)

        await handler.handle(ExportOkf(collection="fieldguide", out_dir="out/bundle"))

        assert links.reads == 0
        record = next(c for c in writer.bundles[-1].concepts if c.path == "records/coastal-survey.md")
        assert "sources" not in record.frontmatter

    async def test_a_mirror_with_no_link_for_the_key_exports_the_entry_without_a_resource(self):
        writer = RecordingBundleWriter()
        handler = _handler(_knowledge_base(), writer, source_links=FakeSourceLinks())

        await handler.handle(ExportOkf(collection="fieldguide", out_dir="out/bundle"))

        record = next(c for c in writer.bundles[-1].concepts if c.path == "records/coastal-survey.md")
        assert record.frontmatter["sources"] == [{"id": "CAT-1", "title": "Coastal survey"}]


class TestParseFaultTolerance:
    async def test_unparseable_note_is_skipped_and_export_completes(self):
        writer = RecordingBundleWriter()
        reader = FakeKnowledgeBaseReader(
            [
                ("Vault/Records/coastal-survey.md", _RECORD),
                ("Vault/Records/broken.md", _BROKEN),
                ("Vault/Specimens/limpet.md", _SPECIMEN),
            ]
        )
        handler = _handler(reader, writer)

        report = await handler.handle(ExportOkf(collection="fieldguide", out_dir="out/bundle"))

        # The broken note is dropped; the two good notes still export.
        assert report.concepts == 2
        assert set(writer.files()) == {"records/coastal-survey.md", "specimens/limpet.md", "index.md"}

    async def test_non_grammar_parse_failure_is_skipped_not_raised(self):
        # A pydantic ValidationError (here: a wrong id-prefix) is NOT a NoteGrammarError, yet
        # the fault-tolerance contract ("a single malformed page must not sink the bundle")
        # must still skip it — the same broad-catch the indexing handler uses.
        writer = RecordingBundleWriter()
        reader = FakeKnowledgeBaseReader([("Vault/Records/mislabelled.md", _BAD_PREFIX), ("Vault/Specimens/limpet.md", _SPECIMEN)])
        handler = _handler(reader, writer)

        report = await handler.handle(ExportOkf(collection="fieldguide", out_dir="out/bundle"))

        # The id-prefix-invalid note is dropped (not raised); the good specimen still exports.
        assert report.concepts == 1
        assert set(writer.files()) == {"specimens/limpet.md", "index.md"}


class TestEventSequence:
    async def test_emits_events_in_order(self):
        writer = RecordingBundleWriter()
        handler = _handler(_knowledge_base(), writer)

        await handler.handle(ExportOkf(collection="fieldguide", out_dir="out/bundle"))

        assert [type(event) for event in handler.events] == [ExportStarted, ConceptsProjected, ExportCompleted]

    async def test_concepts_projected_event_carries_the_counts(self):
        writer = RecordingBundleWriter()
        handler = _handler(_knowledge_base(), writer)

        await handler.handle(ExportOkf(collection="fieldguide", out_dir="out/bundle"))

        projected = next(event for event in handler.events if isinstance(event, ConceptsProjected))
        assert projected.concepts == 2
        assert projected.skipped_meta == 1
        assert projected.relations == 1

    async def test_bus_routes_export_okf_to_the_handler(self):
        writer = RecordingBundleWriter()
        handler = _handler(_knowledge_base(), writer)
        bus = MessageBus({ExportOkf: handler}, {})

        report = await bus.handle(ExportOkf(collection="fieldguide", out_dir="out/bundle"))

        assert isinstance(report, OkfExportReport)
        assert report.concepts == 2
