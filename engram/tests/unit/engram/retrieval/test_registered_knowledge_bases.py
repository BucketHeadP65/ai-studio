"""`doc_authoring_knowledge_bases`: every registered knowledge base that opts into sharing and documentation authoring.

The scan a host builds its documentation-authoring `AuthoringTools` from when it wants to mount
every REGISTERED knowledge base that allows it, not only the knowledge base it was started for: the shape a
multi-knowledge-base MCP surface reads its knowledge base set from. It degrades soft on every kind of trouble (a
missing settings file, a malformed row, a knowledge base whose own schema fails to load) so one broken entry
never takes the rest of the registry down with it, and a genuinely unreadable settings file is
reported through `reachable` rather than raised.
"""

from __future__ import annotations

import json

from typing import TYPE_CHECKING

from engram.authoring.contracts import AuthoringKnowledgeBase, doc_authoring_knowledge_bases

if TYPE_CHECKING:
    from pathlib import Path


def _write_settings(path: Path, knowledge_bases: dict[str, object]) -> None:
    path.write_text(json.dumps({"knowledge_bases": knowledge_bases}), encoding="utf-8")


def _write_spec(root: Path, *, doc_authoring: bool = True, private: bool = False, collection: str = "research") -> None:
    root.mkdir(parents=True, exist_ok=True)
    spec = {
        "slug": root.name,
        "page_types": [{"page_type": "note"}],
        "retrieval": {"doc_authoring": doc_authoring, "private": private, "collection": collection},
    }
    (root / "knowledge-base-schema.json").write_text(json.dumps(spec), encoding="utf-8")


def test_a_missing_settings_file_is_reachable_with_no_targets(tmp_path: Path) -> None:
    scan = doc_authoring_knowledge_bases(tmp_path / "nonexistent.json")

    assert scan.knowledge_bases == ()
    assert scan.reachable is True


def test_a_malformed_settings_file_is_unreachable(tmp_path: Path) -> None:
    settings_path = tmp_path / "application_settings.json"
    settings_path.write_text("not valid json", encoding="utf-8")

    scan = doc_authoring_knowledge_bases(settings_path)

    assert scan.knowledge_bases == ()
    assert scan.reachable is False


def test_a_qualifying_knowledge_base_becomes_a_target(tmp_path: Path) -> None:
    root = tmp_path / "alpha"
    _write_spec(root, collection="alpha-collection")
    settings_path = tmp_path / "application_settings.json"
    _write_settings(settings_path, {"alpha": {"path": str(root)}})

    scan = doc_authoring_knowledge_bases(settings_path)

    assert scan.reachable is True
    assert scan.knowledge_bases == (AuthoringKnowledgeBase(name="alpha", collection="alpha-collection", path=root, schema=scan.knowledge_bases[0].schema),)


def test_a_row_declaring_private_is_skipped(tmp_path: Path) -> None:
    root = tmp_path / "private"
    _write_spec(root)  # the spec itself allows sharing, the registry row does not
    settings_path = tmp_path / "application_settings.json"
    _write_settings(settings_path, {"private": {"path": str(root), "private": True}})

    scan = doc_authoring_knowledge_bases(settings_path)

    assert scan.knowledge_bases == ()
    assert scan.reachable is True


def test_a_spec_declaring_private_is_skipped(tmp_path: Path) -> None:
    root = tmp_path / "private"
    _write_spec(root, private=True)
    settings_path = tmp_path / "application_settings.json"
    _write_settings(settings_path, {"private": {"path": str(root)}})

    scan = doc_authoring_knowledge_bases(settings_path)

    assert scan.knowledge_bases == ()


def test_a_spec_without_doc_authoring_is_skipped(tmp_path: Path) -> None:
    root = tmp_path / "readonly"
    _write_spec(root, doc_authoring=False)
    settings_path = tmp_path / "application_settings.json"
    _write_settings(settings_path, {"readonly": {"path": str(root)}})

    scan = doc_authoring_knowledge_bases(settings_path)

    assert scan.knowledge_bases == ()


def test_a_row_with_no_knowledge_base_schema_is_never_a_target(tmp_path: Path) -> None:
    root = tmp_path / "bare"
    root.mkdir()
    settings_path = tmp_path / "application_settings.json"
    _write_settings(settings_path, {"bare": {"path": str(root)}})

    scan = doc_authoring_knowledge_bases(settings_path)

    # A row with no knowledge-base-schema.json to load carries no spec at all, so it is skipped like any other unloadable row.
    assert scan.knowledge_bases == ()
    assert scan.reachable is True


def test_a_row_missing_a_path_is_skipped_softly(tmp_path: Path) -> None:
    settings_path = tmp_path / "application_settings.json"
    _write_settings(settings_path, {"broken": {"description": "no path key"}})

    scan = doc_authoring_knowledge_bases(settings_path)

    assert scan.knowledge_bases == ()
    assert scan.reachable is True


def test_a_knowledge_base_schema_that_fails_to_load_is_skipped_softly(tmp_path: Path) -> None:
    root = tmp_path / "corrupt"
    root.mkdir()
    (root / "knowledge-base-schema.json").write_text("not valid json", encoding="utf-8")
    settings_path = tmp_path / "application_settings.json"
    _write_settings(settings_path, {"corrupt": {"path": str(root)}})

    scan = doc_authoring_knowledge_bases(settings_path)

    assert scan.knowledge_bases == ()
    assert scan.reachable is True  # the settings file itself is fine, only this row's spec is broken


def test_several_qualifying_rows_all_become_targets(tmp_path: Path) -> None:
    alpha, beta = tmp_path / "alpha", tmp_path / "beta"
    _write_spec(alpha, collection="alpha-collection")
    _write_spec(beta, collection="beta-collection")
    settings_path = tmp_path / "application_settings.json"
    _write_settings(settings_path, {"alpha": {"path": str(alpha)}, "beta": {"path": str(beta)}})

    scan = doc_authoring_knowledge_bases(settings_path)

    assert {knowledge_base.name for knowledge_base in scan.knowledge_bases} == {"alpha", "beta"}
