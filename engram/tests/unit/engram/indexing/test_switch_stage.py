r"""Unit tests for the switch-decision stage (`src/engram/indexing/service/switch_stage.py`).

`SwitchStage.run` counts a collection's knowledge_base-shape signals, decides the three retrieval
switches, and persists them on the fingerprint — but only for a `mode: "auto"` spec. The
first block drives it in isolation through `FakeKnowledgeBaseUnitOfWork`'s `signals` + `fingerprint`
recording repos (no database, no handler); the second block drives the real `IndexKnowledgeBaseHandler`
end to end (a real on-disk knowledge base + fakes) to pin exactly when the stage runs inside an index.
Together these pin:

* an auto spec decides from the scripted signals and persists the result;
* a fixed spec returns `None` and never reads the signals, and writes nothing while its stored
  `private` flag already agrees with the spec's declaration;
* a `retrieval.private` declaration the fingerprint does not carry yet is persisted on either
  mode, leaving the decided three switches exactly as they were;
* `floor` forces a named switch on regardless of the scripted signals;
* an empty `ceiling` forces every switch off, distinct from `ceiling=None` (no ceiling at all);
* `ceiling` overrides `floor` when both pin the same switch in opposite directions;
* a value in a switch's wide band carries `current` through unchanged;
* a real FULL-path index with an auto-mode spec persists switches through the handler;
* a real index with a FIXED-mode spec (today's default) persists nothing;
* the INCREMENTAL path (not just full) runs the stage too, and a signal change between two
  indexes changes what gets persisted;
* a `--dry-run` never reaches the stage at all.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from engram.indexing.contracts import (
    FilesystemKnowledgeBaseReader,
    IndexKnowledgeBase,
    IndexKnowledgeBaseHandler,
    ManifestEntry,
    RetrievalSwitches,
    Signals,
)
from engram.indexing.domain.manifest import FileType
from engram.indexing.read_models import CleanPage, StoredFingerprint
from engram.indexing.service.switch_stage import SwitchStage
from engram.kb.domain.schema import RetrievalSpec
from engram.testing.fakes.embedding import FakeEmbedding
from engram.testing.fakes.reader_scopes import FakeReaderScopes
from engram.testing.fakes.uow import FakeKnowledgeBaseUnitOfWork, FakeValidationReportSink
from engram.testing.knowledge_base_builder import KnowledgeBaseBuilder
from engram.testing.specs import research_spec

if TYPE_CHECKING:
    from pathlib import Path

    from engram.kb.contracts import KnowledgeBaseSchema

_TINY_SIGNALS = Signals(notes=10, token_estimate=1_000, page_types=1, edges_per_note=0.0, giant_component=0.0)
_BIG_SIGNALS = Signals(notes=500, token_estimate=300_000, page_types=3, edges_per_note=0.0, giant_component=0.0)
# Inside embeddings' wide band: too big to satisfy the OFF condition, too small to satisfy ON --
# `current` must carry through unchanged.
_BAND_SIGNALS = Signals(notes=150, token_estimate=1_000, page_types=1, edges_per_note=0.0, giant_component=0.0)

# A single well-formed entity page, just enough to give a first full run one real
# `index_manifest` entry -- an empty knowledge base can never satisfy the incremental branch's
# "manifest is non-empty" gate.
_ENTITY = """\
---
id: ent.lightrag
type: entity
title: LightRAG
status: draft
entity_type: technique
---

## Definition

A graph-indexed retrieval technique.
"""


def _spec(
    *,
    mode: Literal["fixed", "auto"] = "fixed",
    floor: tuple[str, ...] = (),
    ceiling: tuple[str, ...] | None = None,
    private: bool = False,
) -> KnowledgeBaseSchema:
    base = research_spec()
    return base.model_copy(update={"retrieval": RetrievalSpec(mode=mode, floor=floor, ceiling=ceiling, private=private)})


class TestAutoMode:
    async def test_decides_from_signals_and_persists(self):
        uow = FakeKnowledgeBaseUnitOfWork()
        uow.signals.signals = _BIG_SIGNALS
        stage = SwitchStage(lambda: uow)

        decided = await stage.run(collection="research", spec=_spec(mode="auto"))

        assert decided == RetrievalSwitches(embeddings=True, sql=False, graph=False)
        assert uow.fingerprint.set_switches_calls == [("research", decided)]

    async def test_tiny_knowledge_base_decides_all_off(self):
        uow = FakeKnowledgeBaseUnitOfWork()
        uow.signals.signals = _TINY_SIGNALS
        stage = SwitchStage(lambda: uow)

        decided = await stage.run(collection="research", spec=_spec(mode="auto"))

        assert decided == RetrievalSwitches()

    async def test_floor_forces_a_switch_on_over_the_signals(self):
        uow = FakeKnowledgeBaseUnitOfWork()
        uow.signals.signals = _TINY_SIGNALS
        stage = SwitchStage(lambda: uow)

        decided = await stage.run(collection="research", spec=_spec(mode="auto", floor=("embeddings",)))

        assert decided == RetrievalSwitches(embeddings=True)

    async def test_empty_ceiling_forces_everything_off(self):
        uow = FakeKnowledgeBaseUnitOfWork()
        uow.signals.signals = _BIG_SIGNALS
        stage = SwitchStage(lambda: uow)

        decided = await stage.run(collection="research", spec=_spec(mode="auto", ceiling=()))

        assert decided == RetrievalSwitches()

    async def test_ceiling_overrides_floor_on_the_same_switch(self):
        # `RetrievalSpec`'s own validator forbids `floor` naming anything outside `ceiling`
        # (Task 1's `_check_switch_pins`), so this exact combination can never arrive through the
        # normal, validated construction path -- `model_construct` bypasses that validator here
        # deliberately, to pin that `SwitchStage` itself threads `decide_switches`'s conflict order
        # (floor first, ceiling last) correctly, rather than relying solely on the spec-level guard
        # to keep the two pins from ever disagreeing.
        uow = FakeKnowledgeBaseUnitOfWork()
        uow.signals.signals = _TINY_SIGNALS
        stage = SwitchStage(lambda: uow)
        retrieval = RetrievalSpec.model_construct(
            profile="full", collection="research", disclosure_mode="strict", group_by=None, mode="auto", floor=("embeddings",), ceiling=("sql",)
        )
        spec = research_spec().model_copy(update={"retrieval": retrieval})

        decided = await stage.run(collection="research", spec=spec)

        assert decided is not None
        assert decided.embeddings is False
        assert decided == RetrievalSwitches()

    async def test_a_value_inside_the_band_carries_current_through_unchanged(self):
        uow = FakeKnowledgeBaseUnitOfWork()
        uow.signals.signals = _BAND_SIGNALS
        uow.fingerprint.stored_switches = RetrievalSwitches(embeddings=True)
        stage = SwitchStage(lambda: uow)

        decided = await stage.run(collection="research", spec=_spec(mode="auto"))

        assert decided == RetrievalSwitches(embeddings=True)

    async def test_the_privacy_declaration_rides_along_with_the_decision(self):
        uow = FakeKnowledgeBaseUnitOfWork()
        uow.signals.signals = _BIG_SIGNALS
        stage = SwitchStage(lambda: uow)

        decided = await stage.run(collection="field-notes", spec=_spec(mode="auto", private=True))

        assert decided == RetrievalSwitches(embeddings=True, private=True)
        assert uow.fingerprint.set_switches_calls == [("field-notes", decided)]

    async def test_reads_and_writes_the_command_collection(self):
        uow = FakeKnowledgeBaseUnitOfWork()
        uow.signals.signals = _TINY_SIGNALS
        stage = SwitchStage(lambda: uow)

        await stage.run(collection="workdocs", spec=_spec(mode="auto"))

        assert uow.signals.measured_collections == ["workdocs"]
        assert uow.fingerprint.set_switches_calls[0][0] == "workdocs"


class TestReaderScopes:
    """After it stores the switches, the stage gives a private knowledge base its reader role on either mode."""

    async def test_a_fixed_private_knowledge_base_is_scoped_as_private(self):
        scopes = FakeReaderScopes()
        stage = SwitchStage(FakeKnowledgeBaseUnitOfWork, scopes)

        await stage.run(collection="field-notes", spec=_spec(mode="fixed", private=True))

        assert scopes.calls == [("field-notes", True)]

    async def test_an_auto_shared_knowledge_base_is_scoped_as_shared(self):
        uow = FakeKnowledgeBaseUnitOfWork()
        uow.signals.signals = _BIG_SIGNALS
        scopes = FakeReaderScopes()
        stage = SwitchStage(lambda: uow, scopes)

        await stage.run(collection="research", spec=_spec(mode="auto"))

        assert scopes.calls == [("research", False)]

    async def test_an_unchanged_flag_is_scoped_again(self):
        uow = FakeKnowledgeBaseUnitOfWork()
        uow.fingerprint.stored_switches = RetrievalSwitches(private=True)
        scopes = FakeReaderScopes()
        stage = SwitchStage(lambda: uow, scopes)

        await stage.run(collection="field-notes", spec=_spec(mode="fixed", private=True))

        assert uow.fingerprint.set_switches_calls == []
        assert scopes.calls == [("field-notes", True)]


class TestFixedMode:
    async def test_returns_none_and_persists_nothing(self):
        uow = FakeKnowledgeBaseUnitOfWork()
        stage = SwitchStage(lambda: uow)

        decided = await stage.run(collection="research", spec=_spec(mode="fixed"))

        assert decided is None
        assert uow.fingerprint.set_switches_calls == []
        assert uow.signals.measured_collections == []

    async def test_a_private_declaration_lands_on_the_fingerprint(self):
        uow = FakeKnowledgeBaseUnitOfWork()
        stage = SwitchStage(lambda: uow)

        decided = await stage.run(collection="field-notes", spec=_spec(mode="fixed", private=True))

        assert decided is None
        assert uow.fingerprint.set_switches_calls == [("field-notes", RetrievalSwitches(private=True))]
        assert uow.signals.measured_collections == []

    async def test_the_private_write_leaves_the_decided_three_alone(self):
        uow = FakeKnowledgeBaseUnitOfWork()
        uow.fingerprint.stored_switches = RetrievalSwitches(embeddings=True, sql=True, graph=True)
        stage = SwitchStage(lambda: uow)

        await stage.run(collection="field-notes", spec=_spec(mode="fixed", private=True))

        assert uow.fingerprint.set_switches_calls == [("field-notes", RetrievalSwitches(embeddings=True, sql=True, graph=True, private=True))]

    async def test_a_declaration_the_fingerprint_already_carries_writes_nothing(self):
        uow = FakeKnowledgeBaseUnitOfWork()
        uow.fingerprint.stored_switches = RetrievalSwitches(private=True)
        stage = SwitchStage(lambda: uow)

        await stage.run(collection="field-notes", spec=_spec(mode="fixed", private=True))

        assert uow.fingerprint.set_switches_calls == []

    async def test_a_knowledge_base_that_rejoins_the_library_writes_the_flag_back(self):
        uow = FakeKnowledgeBaseUnitOfWork()
        uow.fingerprint.stored_switches = RetrievalSwitches(private=True)
        stage = SwitchStage(lambda: uow)

        await stage.run(collection="field-notes", spec=_spec(mode="fixed", private=False))

        assert uow.fingerprint.set_switches_calls == [("field-notes", RetrievalSwitches(private=False))]


def _handler(knowledge_base_root: Path, *, uow: FakeKnowledgeBaseUnitOfWork, spec: KnowledgeBaseSchema) -> IndexKnowledgeBaseHandler:
    """Build the real handler over an on-disk knowledge base + fakes (the real `SwitchStage` is built inside)."""
    return IndexKnowledgeBaseHandler(FilesystemKnowledgeBaseReader(knowledge_base_root), FakeEmbedding(), lambda: uow, FakeValidationReportSink(), spec=spec)


def _tiny_knowledge_base(tmp_path: Path) -> Path:
    """A knowledge base with no unit documents -- enough to run the full path, nothing to embed."""
    KnowledgeBaseBuilder(tmp_path).build_minimal()
    return tmp_path


def _seed_incremental_state(uow: FakeKnowledgeBaseUnitOfWork) -> list[CleanPage]:
    """Promote a completed full run's manifest + fingerprint WRITES into the fake's READ view.

    `_can_incremental` reads `manifest.load` / `fingerprint.get`, which read the fake's separate
    `stored` attributes, not what a prior run wrote -- copying the writes into `stored` makes the
    *next* `handle()` call take the incremental branch. Returns the clean-page view a real
    `handler.set_clean_page_view` would be fed (mirrors `test_compile_summaries_flag.py`'s helper
    of the same name/shape).
    """
    stored: dict[str, ManifestEntry] = {}
    for batch in uow.manifest.manifest_upserts:
        for entry in batch:
            path = str(entry["path"])
            page_id = entry["page_id"]
            source_key = entry["source_key"]
            stored[path] = ManifestEntry(
                path=path,
                kind=FileType(str(entry["kind"])),
                content_hash=str(entry["content_hash"]),
                identity_hash=str(entry["identity_hash"]),
                page_id=str(page_id) if page_id is not None else None,
                source_key=str(source_key) if source_key is not None else None,
            )
    uow.manifest.stored = stored
    written = uow.fingerprint.fingerprint
    assert written is not None
    uow.fingerprint.stored = StoredFingerprint(**{key: str(value) for key, value in written.items() if key not in {"spec", "collection"}})

    path_by_page = {page_id: path for path, entry in stored.items() if (page_id := entry.page_id) is not None}
    clean: list[CleanPage] = []
    for row in uow.pages.upserted_rows:
        page_id = str(row["id"])
        if page_id not in path_by_page:
            continue
        aliases = row["aliases"]
        source_id = row.get("source_id")
        clean.append(
            CleanPage(
                id=page_id,
                slug=str(row["slug"]),
                title=str(row["title"]),
                aliases=tuple(str(alias) for alias in aliases) if isinstance(aliases, list | tuple) else (),
                path=path_by_page[page_id],
                source_id=str(source_id) if source_id else None,
            )
        )
    return clean


class TestHandlerWiring:
    """`IndexKnowledgeBaseHandler` runs the real `SwitchStage` after the FULL path, never on a dry run."""

    async def test_full_path_auto_mode_persists_switches(self, tmp_path: Path):
        knowledge_base = _tiny_knowledge_base(tmp_path)
        uow = FakeKnowledgeBaseUnitOfWork()
        uow.signals.signals = _BIG_SIGNALS
        handler = _handler(knowledge_base, uow=uow, spec=_spec(mode="auto"))

        await handler.handle(IndexKnowledgeBase(collection="research", embed=False))

        assert uow.fingerprint.set_switches_calls == [("research", RetrievalSwitches(embeddings=True, sql=False, graph=False))]

    async def test_full_path_fixed_mode_persists_nothing(self, tmp_path: Path):
        knowledge_base = _tiny_knowledge_base(tmp_path)
        uow = FakeKnowledgeBaseUnitOfWork()
        uow.signals.signals = _BIG_SIGNALS
        handler = _handler(knowledge_base, uow=uow, spec=_spec(mode="fixed"))

        await handler.handle(IndexKnowledgeBase(collection="research", embed=False))

        assert uow.fingerprint.set_switches_calls == []
        assert uow.signals.measured_collections == []

    async def test_full_path_fixed_mode_still_lands_a_private_declaration(self, tmp_path: Path):
        knowledge_base = _tiny_knowledge_base(tmp_path)
        uow = FakeKnowledgeBaseUnitOfWork()
        uow.signals.signals = _BIG_SIGNALS
        handler = _handler(knowledge_base, uow=uow, spec=_spec(mode="fixed", private=True))

        await handler.handle(IndexKnowledgeBase(collection="research", embed=False))

        assert uow.fingerprint.set_switches_calls == [("research", RetrievalSwitches(private=True))]
        assert uow.signals.measured_collections == []

    async def test_dry_run_never_reaches_the_stage(self, tmp_path: Path):
        knowledge_base = _tiny_knowledge_base(tmp_path)
        uow = FakeKnowledgeBaseUnitOfWork()
        uow.signals.signals = _BIG_SIGNALS
        handler = _handler(knowledge_base, uow=uow, spec=_spec(mode="auto"))

        await handler.handle(IndexKnowledgeBase(collection="research", embed=False, dry_run=True))

        assert uow.fingerprint.set_switches_calls == []
        assert uow.signals.measured_collections == []

    async def test_incremental_path_also_runs_the_stage_and_can_change_the_persisted_switches(self, tmp_path: Path):
        builder = KnowledgeBaseBuilder(tmp_path)
        builder.add_unit_document("Entities", "lightrag", _ENTITY)
        builder.build_minimal()
        uow = FakeKnowledgeBaseUnitOfWork()
        uow.signals.signals = _TINY_SIGNALS
        handler = _handler(tmp_path, uow=uow, spec=_spec(mode="auto"))

        # Seed run: cold start (empty manifest) forces the full path.
        first = await handler.handle(IndexKnowledgeBase(collection="research", embed=False))
        assert first.mode == "full"
        assert uow.fingerprint.stored_switches == RetrievalSwitches()

        clean = _seed_incremental_state(uow)
        handler.set_clean_page_view(lambda: clean)
        uow.signals.signals = _BIG_SIGNALS  # the knowledge base "grew" between indexes

        second = await handler.handle(IndexKnowledgeBase(collection="research", embed=False))

        assert second.mode == "incremental"
        assert uow.fingerprint.stored_switches == RetrievalSwitches(embeddings=True, sql=False, graph=False)
