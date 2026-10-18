"""Unit tests for the `dangling-code-path` validation rule (spec-declared `PageTypeSpec.path_fields`)."""

from __future__ import annotations

from pathlib import Path

from engram.indexing.contracts import resolve
from engram.indexing.domain.validator import validate
from engram.kb.contracts import (
    FrontmatterFieldSpec,
    KnowledgeBaseNote,
    KnowledgeBaseSchema,
    PageTypeSpec,
    VocabularyIndex,
    parse_frontmatter,
)


def _module_spec() -> KnowledgeBaseSchema:
    """A minimal spec: one 'module' page type with a scalar `path` field declared as a path_field."""
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


def _module_note(*, id_: str, path: str) -> KnowledgeBaseNote:
    """A minimal module note carrying the given `path` frontmatter value."""
    frontmatter = parse_frontmatter({"id": id_, "type": "module", "title": id_, "status": "draft", "path": path}, _module_spec())
    return KnowledgeBaseNote(frontmatter=frontmatter, sections=(), claims=(), source_path=f"Memory/Modules/{id_}.md")


def _registry() -> VocabularyIndex:
    return VocabularyIndex(entity_types={}, relations={}, facets={}, claim_types={})


class TestPathFieldsLint:
    """`dangling-code-path` fires only for a path_field value absent under `path_root`."""

    def test_dangling_path_is_flagged_under_path_root(self, tmp_path: Path) -> None:
        (tmp_path / "real.py").write_text("# real", encoding="utf-8")
        real = _module_note(id_="mod.real", path="real.py")
        dangling = _module_note(id_="mod.dangling", path="nope.py")
        resolved = resolve((real, dangling))

        report = validate(resolved.notes, _registry(), resolve_result=resolved, spec=_module_spec(), path_root=tmp_path)

        rules_by_id = {issue.page_id: issue.rule for issue in report.errors}
        assert rules_by_id.get("mod.dangling") == "dangling-code-path"
        assert "mod.real" not in rules_by_id

    def test_path_root_none_skips_the_check(self, tmp_path: Path) -> None:
        dangling = _module_note(id_="mod.dangling", path="nope.py")
        resolved = resolve((dangling,))

        report = validate(resolved.notes, _registry(), resolve_result=resolved, spec=_module_spec(), path_root=None)

        assert not any(issue.rule == "dangling-code-path" for issue in report.errors)

    def test_absolute_path_is_flagged_even_when_it_exists(self, tmp_path: Path) -> None:
        # An absolute path overrides path_root entirely under `/` (pathlib semantics), so a
        # containment check that skipped straight to `.exists()` would happily accept a real
        # file living anywhere on disk. This one is real (this very test file) but outside
        # `tmp_path`, so it must still be flagged.
        outside = str(Path(__file__).resolve())
        note = _module_note(id_="mod.absolute", path=outside)
        resolved = resolve((note,))

        report = validate(resolved.notes, _registry(), resolve_result=resolved, spec=_module_spec(), path_root=tmp_path)

        rules_by_id = {issue.page_id: issue.rule for issue in report.errors}
        assert rules_by_id.get("mod.absolute") == "dangling-code-path"

    def test_dot_dot_escape_is_flagged_even_when_it_exists(self, tmp_path: Path) -> None:
        # A `..`-relative value that resolves to a real file OUTSIDE path_root must still be
        # flagged — existence alone is not containment.
        outside = tmp_path.parent / f"{tmp_path.name}-escape.py"
        outside.write_text("# outside path_root", encoding="utf-8")
        note = _module_note(id_="mod.escape", path=f"../{outside.name}")
        resolved = resolve((note,))

        report = validate(resolved.notes, _registry(), resolve_result=resolved, spec=_module_spec(), path_root=tmp_path)

        rules_by_id = {issue.page_id: issue.rule for issue in report.errors}
        assert rules_by_id.get("mod.escape") == "dangling-code-path"


class TestPathFieldsMessageProvenance:
    """The `dangling-code-path` message names the root's spec-chosen page_type and, when given, its source."""

    def test_message_names_the_spec_path_root_kind(self, tmp_path: Path) -> None:
        dangling = _module_note(id_="mod.dangling", path="nope.py")
        resolved = resolve((dangling,))

        report = validate(resolved.notes, _registry(), resolve_result=resolved, spec=_module_spec(), path_root=tmp_path)

        [issue] = [i for i in report.errors if i.rule == "dangling-code-path"]
        assert "parent" in issue.message  # _module_spec() leaves path_root at its "parent" default

    def test_message_names_the_source_when_given(self, tmp_path: Path) -> None:
        dangling = _module_note(id_="mod.dangling", path="nope.py")
        resolved = resolve((dangling,))

        report = validate(
            resolved.notes, _registry(), resolve_result=resolved, spec=_module_spec(), path_root=tmp_path, path_root_source="--knowledge-base"
        )

        [issue] = [i for i in report.errors if i.rule == "dangling-code-path"]
        assert "--knowledge-base" in issue.message

    def test_message_omits_the_source_when_not_given(self, tmp_path: Path) -> None:
        dangling = _module_note(id_="mod.dangling", path="nope.py")
        resolved = resolve((dangling,))

        report = validate(resolved.notes, _registry(), resolve_result=resolved, spec=_module_spec(), path_root=tmp_path)

        [issue] = [i for i in report.errors if i.rule == "dangling-code-path"]
        assert "--knowledge-base" not in issue.message
        assert "application_settings.json" not in issue.message
