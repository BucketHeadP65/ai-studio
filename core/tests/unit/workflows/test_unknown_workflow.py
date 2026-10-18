"""An unknown workflow is one error on every library backend.

The folder and the collection answer the same not-found condition with the same exception, so a
caller written against one backend catches it on the other. The error is both a `KeyError`, as the
port documents, and a `LibraryError`, which every harness route catches.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

from core.ai.workflows.contracts import LibraryCollection, LibraryError
from core.ai.workflows.infrastructure.folder import FolderLibrary

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from pathlib import Path


class FakeRows:
    """Answers every read with the one page value a test scripted, and no rows."""

    def __init__(self, *, value: Mapping[str, Any] | None = None) -> None:
        self.value = value

    async def fetch(self, sql: str, /, *args: object) -> Sequence[Mapping[str, Any]]:
        return ()

    async def fetchval(self, sql: str, /, *args: object) -> Mapping[str, Any] | None:
        return self.value


class FakeIngest:
    """Accepts every payload and keeps nothing."""

    async def __call__(self, payload: Mapping[str, Any], *, collection: str) -> None:
        return None


async def test_the_collection_answers_an_unknown_workflow_with_a_key_error() -> None:
    with pytest.raises(KeyError) as missing:
        await LibraryCollection(FakeRows(), FakeIngest()).fetch_workflow("wf.nope.v1")
    assert isinstance(missing.value, LibraryError)
    assert str(missing.value) == "no workflow 'wf.nope.v1' in collection 'workflows'"


async def test_the_collection_answers_a_page_without_a_workflow_with_a_key_error() -> None:
    rows = FakeRows(value={"id": "wf.prose-only.v1", "type": "workflow"})
    with pytest.raises(KeyError) as missing:
        await LibraryCollection(rows, FakeIngest()).fetch_workflow("wf.prose-only.v1")
    assert isinstance(missing.value, LibraryError)
    assert "carries no spec" in str(missing.value)


async def test_the_folder_answers_an_unknown_workflow_with_a_library_error(tmp_path: Path) -> None:
    with pytest.raises(LibraryError) as missing:
        await FolderLibrary(tmp_path).fetch_workflow("nope@1")
    assert isinstance(missing.value, KeyError)
    assert str(missing.value) == f"no workflow 'nope@1' in {tmp_path}"


async def test_the_collection_answers_an_unknown_fingerprint_with_a_key_error() -> None:
    with pytest.raises(KeyError) as missing:
        await LibraryCollection(FakeRows(), FakeIngest()).fetch_fingerprint("wf.nope.v1")
    assert isinstance(missing.value, LibraryError)
    assert str(missing.value) == "no workflow 'wf.nope.v1' in collection 'workflows'"


async def test_the_collection_answers_an_unknown_lineage_with_a_key_error() -> None:
    with pytest.raises(KeyError) as missing:
        await LibraryCollection(FakeRows(), FakeIngest()).fetch_lineage("wf.nope.v1")
    assert isinstance(missing.value, LibraryError)
    assert str(missing.value) == "no workflow 'wf.nope.v1' in collection 'workflows'"


async def test_the_folder_answers_an_unknown_fingerprint_with_a_library_error(tmp_path: Path) -> None:
    with pytest.raises(LibraryError) as missing:
        await FolderLibrary(tmp_path).fetch_fingerprint("nope@1")
    assert isinstance(missing.value, KeyError)
    assert str(missing.value) == f"no workflow 'nope@1' in {tmp_path}"


async def test_the_folder_answers_an_unknown_lineage_with_a_library_error(tmp_path: Path) -> None:
    with pytest.raises(LibraryError) as missing:
        await FolderLibrary(tmp_path).fetch_lineage("nope@1")
    assert isinstance(missing.value, KeyError)
    assert str(missing.value) == f"no workflow 'nope@1' in {tmp_path}"
