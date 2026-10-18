"""The `dangling-code-path` rule fires on the incremental path too (not just a full rebuild).

`run_incremental` re-validates only the dirty batch through `_incremental_validate`, which must thread
`IncrementalDeps.path_root` into `validate(...)` exactly as the full-rebuild handler's
`_validate_stage` does. A single body-only edit on a `module`-page_type note (one dirty file, no
identity change, mirroring `test_incremental_handler.py`'s happy-path shape) with a spec-declared
`path` field pointing nowhere proves the wiring end to end: the returned `IndexReport.validation`
carries `dangling-code-path` when `path_root` is set, and none when it is `None` (the check is
skipped, matching `validate`'s own default).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from engram.indexing.contracts import IndexKnowledgeBase, ManifestEntry, content_hash
from engram.indexing.domain.manifest import FileType, identity_hash
from engram.indexing.read_models import CleanPage, StoredFingerprint
from engram.indexing.service.incremental import run_incremental
from engram.kb.contracts import FrontmatterFieldSpec, KnowledgeBaseSchema, PageTypeSpec
from engram.testing.fakes.embedding import FakeEmbedding
from engram.testing.fakes.knowledge_base_reader import FakeKnowledgeBaseReader
from engram.testing.fakes.uow import FakeKnowledgeBaseUnitOfWork

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from pathlib import Path

    from engram.indexing.internal_contracts import IncrementalUnitOfWork

_MODULE_PATH = "Memory/Modules/foo.md"
_MODULE_ID = "mod.foo"


def _module_spec() -> KnowledgeBaseSchema:
    """One `module` page type: a scalar `path` field declared as a `path_field`."""
    return KnowledgeBaseSchema(
        slug="estate",
        page_types=(
            PageTypeSpec(
                page_type="module",
                id_prefix="mod.",
                frontmatter_fields=(FrontmatterFieldSpec(name="path", type="scalar"),),
                path_fields=("path",),
            ),
        ),
    )


def _module_note(*, body: str, path: str) -> str:
    return f"---\nid: {_MODULE_ID}\ntype: module\ntitle: Foo\nstatus: draft\npath: {path}\n---\n\n## Notes\n\n{body}\n"


@dataclass(frozen=True)
class _Deps:
    """A hand-built `IncrementalDeps` scoped to this file's single-note scenario."""

    uow_factory: Callable[[], IncrementalUnitOfWork]
    kb_reader: FakeKnowledgeBaseReader
    embedder: FakeEmbedding
    clean_pages: tuple[CleanPage, ...]
    path_root: Path | None
    path_root_source: str | None = None
    indexer_version: str = "engram-index/1"
    spec_hash: str = "spec-hash-1"
    vocabulary_hash: str = "vocabulary-hash-1"
    spec: KnowledgeBaseSchema = field(default_factory=_module_spec)

    def clean_page_view(self) -> Sequence[CleanPage]:
        return self.clean_pages


def _build_deps(uow: FakeKnowledgeBaseUnitOfWork, *, body: str, path_root: Path | None, path_root_source: str | None = None) -> _Deps:
    """Script a body-only edit on the one module note: content changes, identity does not."""
    v1_text = _module_note(body="original body", path="nope.py")
    v2_text = _module_note(body=body, path="nope.py")
    reader = FakeKnowledgeBaseReader(units=[(_MODULE_PATH, v2_text)])
    embedder = FakeEmbedding()

    uow.manifest.stored = {
        _MODULE_PATH: ManifestEntry(
            path=_MODULE_PATH,
            kind=FileType.NOTE,
            content_hash=content_hash(v1_text),
            identity_hash=identity_hash(stem="foo", id_=_MODULE_ID, title="Foo", aliases=()),
            page_id=_MODULE_ID,
            source_key=None,
        )
    }
    uow.fingerprint.stored = StoredFingerprint(
        indexer_version="engram-index/1", spec_hash="spec-hash-1", vocabulary_hash="vocabulary-hash-1", embed_model=embedder.model_id
    )
    clean = CleanPage(id=_MODULE_ID, slug="foo", title="Foo", aliases=(), path=_MODULE_PATH)
    return _Deps(
        uow_factory=lambda: uow,
        kb_reader=reader,
        embedder=embedder,
        clean_pages=(clean,),
        path_root=path_root,
        path_root_source=path_root_source,
    )


class TestIncrementalPathFieldsLint:
    """`dangling-code-path` on the dirty batch fires only when `path_root` is threaded in."""

    async def test_dangling_path_is_flagged_when_path_root_is_set(self, tmp_path: Path) -> None:
        uow = FakeKnowledgeBaseUnitOfWork()
        deps = _build_deps(uow, body="revised body", path_root=tmp_path)

        report = await run_incremental(IndexKnowledgeBase(collection="research"), deps=deps)

        assert any(issue.rule == "dangling-code-path" and issue.page_id == _MODULE_ID for issue in report.validation.errors)

    async def test_check_is_skipped_when_path_root_is_none(self) -> None:
        uow = FakeKnowledgeBaseUnitOfWork()
        deps = _build_deps(uow, body="revised body", path_root=None)

        report = await run_incremental(IndexKnowledgeBase(collection="research"), deps=deps)

        assert not any(issue.rule == "dangling-code-path" for issue in report.validation.errors)

    async def test_path_root_source_is_threaded_into_the_message(self, tmp_path: Path) -> None:
        uow = FakeKnowledgeBaseUnitOfWork()
        deps = _build_deps(uow, body="revised body", path_root=tmp_path, path_root_source="--knowledge-base")

        report = await run_incremental(IndexKnowledgeBase(collection="research"), deps=deps)

        [issue] = [i for i in report.validation.errors if i.rule == "dangling-code-path"]
        assert "--knowledge-base" in issue.message
