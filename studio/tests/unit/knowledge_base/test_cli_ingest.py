r"""Unit tests for the `studio ingest` CLI command (`src/studio/knowledge_base/cli.py`).

Driven by :class:`typer.testing.CliRunner` with the container *factory* injected through
`ctx.obj` (no monkeypatching, no database): a fake factory returns a fake async container whose
`get(MessageBus)` yields a real :class:`~core.cqrs.bus.MessageBus` wired to a scripted handler, so
the command's dispatch -> print -> exit-code path is exercised end to end.

Pinned behaviour:

* a successful ingest prints the row counts and exits 0;
* `IngestValidationError` prints every finding (severity, page, rule, message) and exits 1;
* an `unknown-facet-value` finding additionally prints the `studio vocab propose` fix path; any
  other rule (entity type, claim kind, relation) does not, since vocab governance only covers facets;
* `CollectionIsIndexedError` still refuses with its own message, unchanged by the vocabulary gate.
"""

from __future__ import annotations

import json

from typing import TYPE_CHECKING

from core.cqrs.contracts import JobQueries, MemoryJobs, MessageBus
from engram.indexing.contracts import (
    CollectionIsIndexedError,
    IngestPage,
    IngestResult,
    IngestValidationError,
    KnowledgeBaseReader,
    ValidationIssue,
)
from engram.retrieval.domain import SqlResult
from engram.testing.fakes.bus_spy import RecordingCommandHandler
from engram.testing.fakes.gateway import FakeSqlGateway
from engram.testing.fakes.knowledge_base_reader import FakeKnowledgeBaseReader
from engram.testing.specs import research_spec
from typer.testing import CliRunner

from studio.knowledge_base.cli import app
from studio.knowledge_base.wiring.contracts import AdminSqlGateway

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from core.cqrs.contracts import Command, CommandHandler, Event
    from typer.testing import Result

runner = CliRunner()

_PAGE_ID = "ent.evidence-audit"


def _identity_gateway() -> FakeSqlGateway:
    """A gateway answering the identity read with no revision and no index history."""
    return FakeSqlGateway(
        routed={
            "to_regclass": SqlResult(rows=({"present": False},), columns=("present",)),
            "index_fingerprint": SqlResult(rows=(), columns=("indexed_at",)),
        }
    )


class FakeContainer:
    """Fake async DI container: `get(MessageBus)` returns the bus it was built with."""

    def __init__(self, bus: MessageBus) -> None:
        self._bus = bus
        self._gateway = _identity_gateway()
        self._reader: KnowledgeBaseReader = FakeKnowledgeBaseReader()
        self._jobs = MemoryJobs()

    async def get(self, key: type) -> object:
        if key is MessageBus:
            return self._bus
        if key is AdminSqlGateway:
            return self._gateway
        if key is KnowledgeBaseReader:
            return self._reader
        if key is JobQueries:
            return self._jobs
        msg = f"FakeContainer has no registration for {key!r}"
        raise KeyError(msg)


class _RaisingHandler:
    """A command handler that raises a scripted exception instead of returning a result."""

    def __init__(self, exc: Exception) -> None:
        self.events: list[Event] = []
        self._exc = exc

    async def __call__(self, command: Command) -> object:
        del command
        raise self._exc


class FakeContainerFactory:
    """Zero-arg `ctx.obj` factory returning a container around a scripted `IngestPage` handler."""

    def __init__(self, handler: CommandHandler) -> None:
        self.handler = handler
        self._bus = MessageBus({IngestPage: handler}, {})

    def __call__(self) -> FakeContainer:
        return FakeContainer(self._bus)


def _payload_file(tmp_path: Path) -> str:
    """A well-formed entity payload JSON, the same shape `note build` renders."""
    data = {
        "frontmatter": {
            "id": _PAGE_ID,
            "type": "entity",
            "title": "RunEvidence audit",
            "summary": "A multi-pass audit over a contested claim.",
            "status": "draft",
            "entity_type": "workflow",
        },
        "sections": [{"heading": "Definition", "body": "Audit a contested claim across several retrieval strategies."}],
    }
    path = tmp_path / "payload.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return str(path)


def _invoke(tmp_path: Path, factory: FakeContainerFactory, *, collection: str = "workflows") -> Result:
    # `--knowledge-base` resolves through `KnowledgeBaseSettings`, which needs the path to hold a schema. The payload's
    # own frontmatter validates against a real spec on construction, so the knowledge base carries the research
    # template's own `knowledge-base-schema.json`.
    (tmp_path / "knowledge-base-schema.json").write_text(research_spec().model_dump_json(), encoding="utf-8")
    return runner.invoke(
        app,
        ["ingest", "--payload", _payload_file(tmp_path), "--collection", collection, "--knowledge-base", str(tmp_path)],
        obj=factory,
    )


def _finding(rule: str, message: str) -> ValidationIssue:
    return ValidationIssue(severity="error", rule=rule, page_id=_PAGE_ID, message=message)


class TestSuccessfulIngest:
    def test_prints_the_row_counts_and_exits_zero(self, tmp_path: Path):
        result_obj = IngestResult(page_id=_PAGE_ID, collection="workflows", sections=2, claims=0, edges=0, embedded=3, reused=0)
        factory = FakeContainerFactory(RecordingCommandHandler(result=result_obj))

        result = _invoke(tmp_path, factory)

        assert result.exit_code == 0
        assert f"ingested {_PAGE_ID}" in result.stdout
        assert "2 section(s)" in result.stdout


class TestVocabularyGateRefusal:
    def test_every_finding_is_printed_and_exits_one(self, tmp_path: Path):
        findings: Sequence[ValidationIssue] = (
            _finding("unknown-entity-type", "unregistered entity_type 'workflow'"),
            _finding("unknown-relation", "unregistered relation 'contradicts'"),
        )
        factory = FakeContainerFactory(_RaisingHandler(IngestValidationError(tuple(findings))))

        result = _invoke(tmp_path, factory)

        assert result.exit_code == 1
        assert "unknown-entity-type" in result.stdout
        assert "unregistered entity_type 'workflow'" in result.stdout
        assert "unknown-relation" in result.stdout
        assert "unregistered relation 'contradicts'" in result.stdout

    def test_an_unknown_facet_value_finding_prints_the_propose_hint(self, tmp_path: Path):
        factory = FakeContainerFactory(
            _RaisingHandler(IngestValidationError((_finding("unknown-facet-value", "unregistered tasks value 'teleportation'"),)))
        )

        result = _invoke(tmp_path, factory)

        assert result.exit_code == 1
        assert "studio vocab propose" in result.stdout

    def test_an_entity_type_finding_does_not_print_the_propose_hint(self, tmp_path: Path):
        # There is no `studio vocab propose --entity-type`: vocab governance covers facet values
        # only, so the hint would misdirect the fix for any other unregistered-vocabulary rule.
        factory = FakeContainerFactory(
            _RaisingHandler(IngestValidationError((_finding("unknown-entity-type", "unregistered entity_type 'workflow'"),)))
        )

        result = _invoke(tmp_path, factory)

        assert "studio vocab propose" not in result.stdout


class TestCollectionIsIndexedStillRefuses:
    def test_message_is_unchanged_by_the_vocabulary_gate(self, tmp_path: Path):
        message = "collection 'research' is indexed from a knowledge base (1 file(s) in its manifest): the next index would prune a page written without a file. Ingest into a collection of its own."
        factory = FakeContainerFactory(_RaisingHandler(CollectionIsIndexedError(message)))

        result = _invoke(tmp_path, factory, collection="research")

        assert result.exit_code == 1
        assert message in result.stdout
