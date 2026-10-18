"""Unit tests for the `memory note` authoring subcommands (`src/studio/knowledge_base/cli.py`).

The commands run over the shared kernel (`memory.parser` / `vocabulary` / `renderer` /
`payload` / `verify`). These tests drive them through :class:`typer.testing.CliRunner`,
using the `ctx.obj` container-factory seam (no monkeypatching): a test injects a zero-arg
factory returning a :class:`FakeContainer` whose async ``get`` resolves a
:class:`~studio.knowledge_base.settings.KnowledgeBaseSettings` pointing at a ``tmp_path`` mini-knowledge_base built from the
golden fixtures. The commands therefore exercise real parsing, real vocabulary,
and real quote-in-mirror verification, with the only seam being *where the knowledge base lives*.

The contract under test:

* `validate PATH [--knowledge-base P]` — parse + the full single-note validation (the
  `engram.indexing.domain.validator` note-local rule subset: summary length,
  comparative-claim qualifiers, quote-by-kind, anchor uniqueness, vocabulary
  vocabulary), so authoring-time validation equals index-time single-note validation;
  exit 1 on errors, 0 when clean (dangling cross-note links are warnings — printed,
  never failing);
* `build --payload P --out PATH [--verify-quotes] [--force]` — render an
  authoring payload to canonical md and atomically write it; refuse to overwrite
  unless `--force`; the rendered note is run through the same single-note validation
  *before* any write (an error-tier issue exits 1 and writes nothing); with
  `--verify-quotes` every quoted claim is additionally checked against its
  `Sources/` mirror, also before the write;
* `verify PATH` — quote-in-mirror check for every `quote::` claim, resolving
  the mirror from the note's `zotero_key`; exit 1 on any failure;
* `schema --type TYPE` — emit the authoring-payload JSON schema for a page
  type; exit 1 on an unknown type.

The knowledge base is never written to (`--out` points into `tmp_path`).
"""

from __future__ import annotations

import json
import shutil

from typing import TYPE_CHECKING

import pytest

from engram.kb import contracts as kb
from engram.kb.contracts import (
    CanonicalSection,
    ClaimGrammarSpec,
    FacetSpec,
    FrontmatterFieldSpec,
    KnowledgeBaseSchema,
    PageTypeSpec,
    SourcesSpec,
    VocabEntry,
)
from engram.testing.knowledge_base_builder import GOLDEN, GOLDEN_MINI_VOCABULARIES
from engram.testing.specs import research_spec
from typer.testing import CliRunner

from studio.knowledge_base.cli import app
from studio.knowledge_base.cli_note import _find_mirror_text, _quote_failures
from studio.knowledge_base.settings import KnowledgeBaseSettings

if TYPE_CHECKING:
    from pathlib import Path
    from typing import Literal

    from pydantic import JsonValue

runner = CliRunner()

# The golden knowledge base fixtures ship with engram as package data.
_GOLDEN = GOLDEN
_LIGHTRAG_NOTE = _GOLDEN / "lightrag-2025.md"
_MIRROR_EXCERPT = _GOLDEN / "mirror-excerpt.md"
_MINI_VOCABULARY = GOLDEN_MINI_VOCABULARIES

# The golden mirror's join key (frontmatter `zotero_key`), shared by the note.
_LIGHTRAG_ZOTERO_KEY = "8QXFXYMI"


class FakeContainer:
    """Stand-in for the wireup container, resolving only what the note commands need.

    The note commands resolve :class:`KnowledgeBaseSettings` from the container via its
    async `get` (the real `wireup.AsyncContainer` interface). This fake mirrors
    that single method: it hands back a pre-built settings object, or — when
    `knowledge_base` is `None` — raises, standing in for a missing/invalid knowledge base (the
    failure `ApplicationSettings.from_env` would raise in production).
    """

    def __init__(self, knowledge_base: KnowledgeBaseSettings | None) -> None:
        self._knowledge_base = knowledge_base

    async def get(self, kind: type[object]) -> object:
        if kind is KnowledgeBaseSettings:
            if self._knowledge_base is None:
                msg = "KNOWLEDGE_BASE_PATH is not set"
                raise ValueError(msg)
            return self._knowledge_base
        msg = f"FakeContainer cannot resolve {kind!r}"
        raise KeyError(msg)


def _factory(knowledge_base: KnowledgeBaseSettings | None):
    """A zero-arg ``ctx.obj`` factory returning a :class:`FakeContainer`."""

    def build() -> FakeContainer:
        return FakeContainer(knowledge_base)

    return build


def _raising_factory():
    """A `ctx.obj` factory that fails to *build* its container.

    Stands in for production's default factory when `ApplicationSettings.from_env`
    raises because `KNOWLEDGE_BASE_PATH` is missing/invalid — the construction-time failure
    a knowledge base-needing command must still surface cleanly (exit 1, message), not as a
    traceback.
    """

    def build() -> object:
        msg = "KNOWLEDGE_BASE_PATH is not an existing directory"
        raise ValueError(msg)

    return build


def _build_knowledge_base(root: Path, *, with_mirror: bool = True) -> KnowledgeBaseSettings:
    """Materialise a minimal valid knowledge base under `root` from the golden fixtures.

    Lays down the `Memory/` + `_meta/` directories `KnowledgeBaseSettings` requires,
    copies the three real-format mini-vocabulary into `_meta/vocabulary/`, and
    (by default) the golden mirror excerpt into `Sources/` so quote
    verification can resolve it by `zotero_key`.
    """
    (root / "Units" / "Papers").mkdir(parents=True)
    vocabulary_dir = root / "_meta" / "vocabulary"
    vocabulary_dir.mkdir(parents=True)
    for vocabulary in sorted(_MINI_VOCABULARY.glob("*.md")):
        shutil.copy(vocabulary, vocabulary_dir / vocabulary.name)
    sources = root / "Sources"
    sources.mkdir()
    if with_mirror:
        shutil.copy(_MIRROR_EXCERPT, sources / "lightrag-mirror.md")
    (root / "knowledge-base-schema.json").write_text(research_spec().model_dump_json(), encoding="utf-8")
    return KnowledgeBaseSettings.model_validate({"path": str(root)})


def _write_note(root: Path, text: str, *, name: str = "note.md") -> Path:
    """Write `text` as a note file under `root` (outside any knowledge base) and return its path."""
    path = root / name
    path.write_text(text, encoding="utf-8")
    return path


# The golden note declares `modalities: [text]`; the minimal mini-vocabulary fixture
# (a deliberate subset of the live `facets.md`) defines no `modalities` facet, so
# a vocabulary-clean note for these vocabularies simply omits that one line. Every other
# facet/kind/relation the golden note uses IS in the mini-vocabulary.
#
# The golden fixture's claim ^c9 has a comparative `outperforms` edge + `metric` but
# (deliberately, as authored) no `on_dataset`/`on_task` — a real comparative-missing-
# qualifiers error the FULL single-note validation now catches. To keep a genuinely validation-clean
# note for the exit-0 cases (the fixture file is shared by many other tests, so we patch
# only this in-memory copy, not the file), add the missing dataset qualifier to ^c9.
_CLEAN_NOTE_TEXT = (
    _LIGHTRAG_NOTE.read_text(encoding="utf-8")
    .replace("modalities: [text]\n", "", 1)
    .replace("    - metric:: [[index-update-cost]]\n", "    - metric:: [[index-update-cost]]\n    - on_dataset:: [[ultradomain]]\n", 1)
)

# The golden note's (≤400-char) summary value, used by the oversized-summary validation test
# to swap in a >400-char value without disturbing the rest of the note.
_CLEAN_SUMMARY = next(line.removeprefix("summary: ") for line in _CLEAN_NOTE_TEXT.splitlines() if line.startswith("summary: "))

# A quote passage present verbatim in the golden mirror excerpt (claim ^c1's quote);
# the excerpt is a partial mirror, so quote tests target passages it actually contains.
_MIRROR_QUOTE = "LightRAG consistently outperforms GraphRAG, particularly in larger datasets with complex language contexts"


def _verifiable_note_text() -> str:
    """A paper note with a single claim whose quote appears in the golden mirror excerpt."""
    quote_line = f'    - quote:: "{_MIRROR_QUOTE}"'
    return (
        f"---\nid: paper.lightrag-2025\ntype: paper\ntitle: LightRAG\nstatus: draft\n"
        f"paper_type: method\nyear: 2025\nzotero_key: {_LIGHTRAG_ZOTERO_KEY}\nsource_hash: h\n---\n\n"
        f"## Findings\n\n- LightRAG wins. ^c1\n    - loc:: §3\n{quote_line}\n    - kind:: benchmark-result\n"
    )


# Two passages present verbatim in the golden mirror excerpt — packed into one
# `quote::` value as a table-derived claim would store them (several `"..."`
# snippets in one value). The whole value is NOT a contiguous mirror substring, so
# only per-snippet matching (quotes_in_mirror) can verify it.
_MIRROR_SNIPPET_A = "LightRAG consistently outperforms GraphRAG"
_MIRROR_SNIPPET_B = "graph-based RAG systems like LightRAG and GraphRAG consistently outperform"


def _multi_snippet_note_text(snippet_a: str, snippet_b: str) -> str:
    """A paper note whose single claim packs two `"..."` snippets into one `quote::` value."""
    quote_line = f'    - quote:: "{snippet_a}" vs "{snippet_b}"'
    return (
        f"---\nid: paper.lightrag-2025\ntype: paper\ntitle: LightRAG\nstatus: draft\n"
        f"paper_type: method\nyear: 2025\nzotero_key: {_LIGHTRAG_ZOTERO_KEY}\nsource_hash: h\n---\n\n"
        f"## Findings\n\n- LightRAG wins. ^c1\n    - loc:: §3\n{quote_line}\n    - kind:: benchmark-result\n"
    )


def _paper_payload(**frontmatter_overrides: JsonValue) -> dict[str, JsonValue]:
    """A minimal valid `paper` authoring payload as an extractor would emit it."""
    frontmatter: dict[str, JsonValue] = {
        "id": "paper.sample-2025",
        "type": "paper",
        "title": "Sample",
        "status": "draft",
        "paper_type": "method",
        "year": 2025,
        "zotero_key": _LIGHTRAG_ZOTERO_KEY,
        "source_hash": "h",
    }
    frontmatter.update(frontmatter_overrides)
    return {"frontmatter": frontmatter, "sections": [{"heading": "TL;DR", "body": "A one-line summary."}], "claims": []}


def _write_payload(path: Path, payload: dict[str, JsonValue]) -> Path:
    """Serialise an authoring payload to a JSON file and return its path."""
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


# ----- validate ---------------------------------------------------------------


class TestValidate:
    def test_clean_note_exits_zero(self, tmp_path: Path):
        # The golden lightrag note parses and uses only registered vocabulary.
        knowledge_base = _build_knowledge_base(tmp_path / "knowledge_base")
        note = _write_note(tmp_path, _CLEAN_NOTE_TEXT, name="lightrag.md")

        result = runner.invoke(app, ["note", "validate", str(note), "--knowledge-base", str(knowledge_base.path)], obj=_factory(knowledge_base))

        assert result.exit_code == 0, result.stdout

    def test_unknown_facet_value_exits_one(self, tmp_path: Path):
        # A task facet value absent from the vocabulary is a vocabulary error → exit 1.
        knowledge_base = _build_knowledge_base(tmp_path / "knowledge_base")
        text = _LIGHTRAG_NOTE.read_text(encoding="utf-8").replace("tasks: [long-doc-qa, sensemaking-qa]", "tasks: [not-a-real-task]")
        note = _write_note(tmp_path, text, name="bad-facet.md")

        result = runner.invoke(app, ["note", "validate", str(note), "--knowledge-base", str(knowledge_base.path)], obj=_factory(knowledge_base))

        assert result.exit_code == 1
        assert "not-a-real-task" in result.stdout

    def test_unknown_claim_kind_exits_one(self, tmp_path: Path):
        # A claim `kind::` value outside the vocabulary's claim_types is an error.
        knowledge_base = _build_knowledge_base(tmp_path / "knowledge_base")
        text = _LIGHTRAG_NOTE.read_text(encoding="utf-8").replace("kind:: benchmark-result", "kind:: not-a-real-kind", 1)
        note = _write_note(tmp_path, text, name="bad-kind.md")

        result = runner.invoke(app, ["note", "validate", str(note), "--knowledge-base", str(knowledge_base.path)], obj=_factory(knowledge_base))

        assert result.exit_code == 1
        assert "not-a-real-kind" in result.stdout

    def test_parse_error_exits_one(self, tmp_path: Path):
        # A claim missing the grammar-required loc:: cannot be represented → parse error.
        knowledge_base = _build_knowledge_base(tmp_path / "knowledge_base")
        text = "---\nid: paper.x-2025\ntype: paper\ntitle: X\nstatus: draft\npaper_type: method\nyear: 2025\nzotero_key: K\nsource_hash: h\n---\n\n## Findings\n\n- A claim with no locator. ^c1\n    - kind:: observation\n"
        note = _write_note(tmp_path, text, name="no-loc.md")

        result = runner.invoke(app, ["note", "validate", str(note), "--knowledge-base", str(knowledge_base.path)], obj=_factory(knowledge_base))

        assert result.exit_code == 1

    def test_knowledge_base_option_overrides_container(self, tmp_path: Path):
        # --knowledge-base points vocabulary resolution at a different root than the container's
        # (here the container resolves NO knowledge base, proving --knowledge-base is what's used).
        override_knowledge_base = _build_knowledge_base(tmp_path / "override")
        note = _write_note(tmp_path, _CLEAN_NOTE_TEXT, name="lightrag.md")

        result = runner.invoke(app, ["note", "validate", str(note), "--knowledge-base", str(override_knowledge_base.path)], obj=_factory(None))

        assert result.exit_code == 0, result.stdout

    def test_vault_option_behaves_like_knowledge_base_and_warns(self, tmp_path: Path):
        # --vault is the deprecated alias: same resolution as --knowledge-base, plus a warning.
        override_knowledge_base = _build_knowledge_base(tmp_path / "override")
        note = _write_note(tmp_path, _CLEAN_NOTE_TEXT, name="lightrag.md")

        result = runner.invoke(app, ["note", "validate", str(note), "--vault", str(override_knowledge_base.path)], obj=_factory(None))

        assert result.exit_code == 0, result.stdout
        assert "--vault is deprecated" in result.stderr
        assert "--knowledge-base" in result.stderr

    def test_missing_knowledge_base_errors_cleanly(self, tmp_path: Path):
        # No --knowledge-base and the container cannot resolve one → exit 1 with a helpful message, not a traceback.
        note = shutil.copy(_LIGHTRAG_NOTE, tmp_path / "lightrag.md")

        result = runner.invoke(app, ["note", "validate", str(note)], obj=_factory(None))

        assert result.exit_code == 1
        assert "knowledge base" in result.stdout.lower()

    def test_container_build_failure_errors_cleanly(self, tmp_path: Path):
        # The container itself failing to build (the production KNOWLEDGE_BASE_PATH-missing case)
        # must surface as the same clean knowledge base error, NOT a pydantic traceback.
        note = shutil.copy(_LIGHTRAG_NOTE, tmp_path / "lightrag.md")

        result = runner.invoke(app, ["note", "validate", str(note)], obj=_raising_factory())

        assert result.exit_code == 1
        assert "knowledge base" in result.stdout.lower()

    def test_oversized_summary_warns_and_passes(self, tmp_path: Path):
        # The full single-note validation (= index-stage single-note validation) flags a >400-char
        # summary at authoring time as a warning: named, never a refusal.
        knowledge_base = _build_knowledge_base(tmp_path / "knowledge_base")
        oversized = "x" * 401
        text = _CLEAN_NOTE_TEXT.replace(f"summary: {_CLEAN_SUMMARY}", f"summary: {oversized}", 1)
        note = _write_note(tmp_path, text, name="oversized.md")

        result = runner.invoke(app, ["note", "validate", str(note), "--knowledge-base", str(knowledge_base.path)], obj=_factory(knowledge_base))

        assert result.exit_code == 0
        assert "OK (" in result.stdout
        assert "oversized-summary" in result.stdout

    def test_comparative_missing_qualifiers_exits_one(self, tmp_path: Path):
        # A comparative edge (outperforms) stripped of its metric:: qualifier is a
        # single-note validation error the laxer parse+vocabulary path never saw.
        knowledge_base = _build_knowledge_base(tmp_path / "knowledge_base")
        text = _CLEAN_NOTE_TEXT.replace("    - metric:: [[llm-judged-win-rate]]\n", "", 1)
        note = _write_note(tmp_path, text, name="bad-comparative.md")

        result = runner.invoke(app, ["note", "validate", str(note), "--knowledge-base", str(knowledge_base.path)], obj=_factory(knowledge_base))

        assert result.exit_code == 1
        assert "comparative-missing-qualifiers" in result.stdout

    def test_dangling_cross_note_link_is_a_warning_not_failure(self, tmp_path: Path):
        # The clean note's claims point at other notes (e.g. [[graphrag]]); with no
        # full-knowledge base index every such link dangles, but at single-note level those are
        # warnings — they print but do NOT flip the exit code. Multiple dangling links
        # collapse into ONE notice naming the count, not one line per link.
        knowledge_base = _build_knowledge_base(tmp_path / "knowledge_base")
        note = _write_note(tmp_path, _CLEAN_NOTE_TEXT, name="lightrag.md")

        result = runner.invoke(app, ["note", "validate", str(note), "--knowledge-base", str(knowledge_base.path)], obj=_factory(knowledge_base))

        assert result.exit_code == 0, result.stdout
        dangling_lines = [line for line in result.stdout.splitlines() if "dangling-link" in line]
        assert len(dangling_lines) == 1, result.stdout
        assert "not checked here" in dangling_lines[0]
        assert "resolve at index" in dangling_lines[0]


def _write_module_spec(knowledge_base_root: Path, *, path_root: Literal["parent", "knowledge_base"] = "parent") -> Path:
    """Write a minimal knowledge-base-schema.json with one `module` page type carrying a path_field."""
    spec = KnowledgeBaseSchema(
        slug="estate",
        purpose="A code-plane knowledge base",
        path_root=path_root,
        page_types=(
            PageTypeSpec(
                page_type="module",
                id_prefix="mod.",
                frontmatter_fields=(FrontmatterFieldSpec(name="path", type="scalar", required=True),),
                path_fields=("path",),
            ),
        ),
    )
    spec_path = knowledge_base_root / "knowledge-base-schema.json"
    spec_path.write_text(spec.model_dump_json(indent=2), encoding="utf-8")
    return spec_path


def _build_module_knowledge_base(root: Path) -> Path:
    """A minimal code-plane knowledge base: a `module` knowledge-base-schema.json + an empty vocabulary dir."""
    root.mkdir(parents=True, exist_ok=True)
    _write_module_spec(root)
    (root / "_meta" / "vocabulary").mkdir(parents=True)
    return root


class TestValidateModulePathField:
    """`note validate` on a `module`-kind note threads `path_root` into `validate_note` (the index parity)."""

    def test_dangling_path_reports_dangling_code_path(self, tmp_path: Path) -> None:
        # The knowledge base root sits one level under path_root (mirrors `studio index`'s
        # `KnowledgeBaseSettings.path.parent` convention), so a module note's `path` frontmatter
        # resolves against the repo root the knowledge base is checked out under.
        knowledge_base = _build_module_knowledge_base(tmp_path / "estate" / "docs")
        note_text = "---\nid: mod.foo\ntype: module\ntitle: Foo\nstatus: draft\npath: nope.py\n---\n\n## Notes\n\nSomething.\n"
        note = _write_note(tmp_path, note_text, name="foo.md")

        result = runner.invoke(app, ["note", "validate", str(note), "--knowledge-base", str(knowledge_base)], obj=_factory(None))

        assert result.exit_code == 1
        assert "dangling-code-path" in result.stdout
        assert "--knowledge-base" in result.stdout  # names the root's origin, not just the root itself

    def test_dangling_path_names_the_working_directory_when_knowledge_base_flag_is_absent(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # No --knowledge-base: the working directory supplies the knowledge base, so the message's origin is
        # "working directory" — the other half of `knowledge_base_path_origin`.
        knowledge_base_root = _build_module_knowledge_base(tmp_path / "estate" / "docs")
        note_text = "---\nid: mod.foo\ntype: module\ntitle: Foo\nstatus: draft\npath: nope.py\n---\n\n## Notes\n\nSomething.\n"
        note = _write_note(tmp_path, note_text, name="foo2.md")
        monkeypatch.chdir(knowledge_base_root)

        result = runner.invoke(app, ["note", "validate", str(note)], obj=_factory(None))

        assert result.exit_code == 1
        assert "dangling-code-path" in result.stdout
        assert "working directory" in result.stdout


class TestValidateModulePathFieldKnowledgeBaseRoot:
    """A spec declaring `path_root="knowledge_base"` resolves `path_fields` against the knowledge base root itself, not its parent."""

    def test_a_path_that_exists_under_the_knowledge_base_root_passes(self, tmp_path: Path) -> None:
        knowledge_base_dir = tmp_path / "vault"
        knowledge_base_dir.mkdir(parents=True)
        _write_module_spec(knowledge_base_dir, path_root="knowledge_base")
        (knowledge_base_dir / "_meta" / "vocabulary").mkdir(parents=True)
        (knowledge_base_dir / "real.py").write_text("# real\n", encoding="utf-8")
        note_text = "---\nid: mod.foo\ntype: module\ntitle: Foo\nstatus: draft\npath: real.py\n---\n\n## Notes\n\nSomething.\n"
        note = _write_note(tmp_path, note_text, name="foo.md")

        result = runner.invoke(app, ["note", "validate", str(note), "--knowledge-base", str(knowledge_base_dir)], obj=_factory(None))

        assert result.exit_code == 0, result.stdout

    def test_a_path_that_only_exists_beside_the_knowledge_base_root_is_dangling(self, tmp_path: Path) -> None:
        # Proves the base actually moved: the same relative name that would resolve fine under
        # the default `path_root="parent"` (living beside the knowledge base dir) dangles once the spec
        # declares `path_root="knowledge_base"` — the reverse of `TestValidateModulePathField`'s case.
        knowledge_base_dir = tmp_path / "vault"
        knowledge_base_dir.mkdir(parents=True)
        _write_module_spec(knowledge_base_dir, path_root="knowledge_base")
        (knowledge_base_dir / "_meta" / "vocabulary").mkdir(parents=True)
        (tmp_path / "beside.py").write_text("# beside\n", encoding="utf-8")
        note_text = "---\nid: mod.foo\ntype: module\ntitle: Foo\nstatus: draft\npath: beside.py\n---\n\n## Notes\n\nSomething.\n"
        note = _write_note(tmp_path, note_text, name="foo.md")

        result = runner.invoke(app, ["note", "validate", str(note), "--knowledge-base", str(knowledge_base_dir)], obj=_factory(None))

        assert result.exit_code == 1
        assert "dangling-code-path" in result.stdout


# ----- build ------------------------------------------------------------------


class TestBuild:
    def test_happy_path_writes_a_parseable_note(self, tmp_path: Path):
        knowledge_base = _build_knowledge_base(tmp_path / "knowledge_base")
        payload = _write_payload(tmp_path / "p.json", _paper_payload())
        out = tmp_path / "out.md"

        result = runner.invoke(
            app, ["note", "build", "--payload", str(payload), "--out", str(out), "--knowledge-base", str(knowledge_base.path)], obj=_factory(knowledge_base)
        )

        assert result.exit_code == 0, result.stdout
        assert out.exists()
        text = out.read_text(encoding="utf-8")
        assert "id: paper.sample-2025" in text
        assert "## TL;DR" in text

    def test_refuses_to_overwrite_without_force(self, tmp_path: Path):
        knowledge_base = _build_knowledge_base(tmp_path / "knowledge_base")
        payload = _write_payload(tmp_path / "p.json", _paper_payload())
        out = tmp_path / "out.md"
        out.write_text("ORIGINAL", encoding="utf-8")

        result = runner.invoke(app, ["note", "build", "--payload", str(payload), "--out", str(out)], obj=_factory(knowledge_base))

        assert result.exit_code == 1
        assert out.read_text(encoding="utf-8") == "ORIGINAL"  # untouched

    def test_force_overwrites(self, tmp_path: Path):
        knowledge_base = _build_knowledge_base(tmp_path / "knowledge_base")
        payload = _write_payload(tmp_path / "p.json", _paper_payload())
        out = tmp_path / "out.md"
        out.write_text("ORIGINAL", encoding="utf-8")

        result = runner.invoke(
            app,
            ["note", "build", "--payload", str(payload), "--out", str(out), "--force", "--knowledge-base", str(knowledge_base.path)],
            obj=_factory(knowledge_base),
        )

        assert result.exit_code == 0, result.stdout
        assert "id: paper.sample-2025" in out.read_text(encoding="utf-8")

    def test_invalid_payload_exits_one_without_writing(self, tmp_path: Path):
        # A payload whose frontmatter fails validation (wrong id prefix) must not write.
        knowledge_base = _build_knowledge_base(tmp_path / "knowledge_base")
        payload = _write_payload(tmp_path / "p.json", _paper_payload(id="ent.sample"))
        out = tmp_path / "out.md"

        result = runner.invoke(
            app, ["note", "build", "--payload", str(payload), "--out", str(out), "--knowledge-base", str(knowledge_base.path)], obj=_factory(knowledge_base)
        )

        assert result.exit_code == 1
        assert not out.exists()

    def test_verify_quotes_pass_writes_the_file(self, tmp_path: Path):
        # A claim whose quote appears verbatim in the golden mirror passes → file written.
        knowledge_base = _build_knowledge_base(tmp_path / "knowledge_base")
        good_quote = "LightRAG consistently outperforms GraphRAG, particularly in larger datasets with complex language contexts"
        payload = _paper_payload()
        payload["claims"] = [{"text": "LightRAG wins", "loc": "§3", "quote": good_quote, "kind": "benchmark-result"}]
        payload_path = _write_payload(tmp_path / "p.json", payload)
        out = tmp_path / "out.md"

        result = runner.invoke(
            app,
            ["note", "build", "--payload", str(payload_path), "--out", str(out), "--verify-quotes", "--knowledge-base", str(knowledge_base.path)],
            obj=_factory(knowledge_base),
        )

        assert result.exit_code == 0, result.stdout
        assert out.exists()

    def test_verify_quotes_fail_writes_nothing(self, tmp_path: Path):
        # A quote absent from the mirror fails verification → exit 1 AND no file written
        # (verification happens before the write).
        knowledge_base = _build_knowledge_base(tmp_path / "knowledge_base")
        payload = _paper_payload()
        payload["claims"] = [
            {"text": "fabricated", "loc": "§3", "quote": "this exact sentence is not in the mirror", "kind": "benchmark-result"}
        ]
        payload_path = _write_payload(tmp_path / "p.json", payload)
        out = tmp_path / "out.md"

        result = runner.invoke(
            app, ["note", "build", "--payload", str(payload_path), "--out", str(out), "--verify-quotes"], obj=_factory(knowledge_base)
        )

        assert result.exit_code == 1
        assert not out.exists()

    def test_verify_quotes_needs_a_knowledge_base(self, tmp_path: Path):
        # --verify-quotes with no resolvable knowledge base → exit 1, helpful message, no write.
        payload = _paper_payload()
        payload["claims"] = [{"text": "x", "loc": "§3", "quote": "anything", "kind": "benchmark-result"}]
        payload_path = _write_payload(tmp_path / "p.json", payload)
        out = tmp_path / "out.md"

        result = runner.invoke(
            app, ["note", "build", "--payload", str(payload_path), "--out", str(out), "--verify-quotes"], obj=_factory(None)
        )

        assert result.exit_code == 1
        assert not out.exists()
        assert "knowledge base" in result.stdout.lower()

    def test_oversized_summary_warns_and_still_writes(self, tmp_path: Path):
        # build runs the full single-note validation on the rendered note BEFORE writing; a
        # >400-char summary is a warning-tier issue → printed, and the note is written.
        knowledge_base = _build_knowledge_base(tmp_path / "knowledge_base")
        payload = _write_payload(tmp_path / "p.json", _paper_payload(summary="x" * 401))
        out = tmp_path / "out.md"

        result = runner.invoke(
            app, ["note", "build", "--payload", str(payload), "--out", str(out), "--knowledge-base", str(knowledge_base.path)], obj=_factory(knowledge_base)
        )

        assert result.exit_code == 0
        assert out.exists()
        assert "oversized-summary" in result.stdout

    def test_comparative_missing_qualifiers_refused_without_writing(self, tmp_path: Path):
        # A comparative edge (outperforms) with no metric:: is an error-tier validation issue →
        # build refuses and writes nothing.
        knowledge_base = _build_knowledge_base(tmp_path / "knowledge_base")
        payload = _paper_payload()
        payload["claims"] = [
            {
                "text": "LightRAG wins",
                "loc": "§3",
                "quote": "q",
                "kind": "benchmark-result",
                "edges": [["outperforms", "[[graphrag]]"]],
                "on_dataset": ["[[ultradomain]]"],
            }
        ]
        payload_path = _write_payload(tmp_path / "p.json", payload)
        out = tmp_path / "out.md"

        result = runner.invoke(
            app, ["note", "build", "--payload", str(payload_path), "--out", str(out), "--knowledge-base", str(knowledge_base.path)], obj=_factory(knowledge_base)
        )

        assert result.exit_code == 1
        assert not out.exists()
        assert "comparative-missing-qualifiers" in result.stdout

    def test_build_needs_a_knowledge_base_for_validation(self, tmp_path: Path):
        # build now always validates (vocabulary-backed), so it needs a resolvable knowledge base even
        # without --verify-quotes; an unresolvable knowledge base is a clean exit-1, no write.
        payload = _write_payload(tmp_path / "p.json", _paper_payload())
        out = tmp_path / "out.md"

        result = runner.invoke(app, ["note", "build", "--payload", str(payload), "--out", str(out)], obj=_factory(None))

        assert result.exit_code == 1
        assert not out.exists()
        assert "knowledge base" in result.stdout.lower()


# ----- verify -----------------------------------------------------------------


def _comparative_without_metric(payload: dict[str, JsonValue]) -> dict[str, JsonValue]:
    """`payload` with one comparative claim stripped of its metric qualifier: an error-tier validation issue."""
    payload["claims"] = [
        {
            "text": "LightRAG wins",
            "loc": "§3",
            "quote": "q",
            "kind": "benchmark-result",
            "edges": [["outperforms", "[[graphrag]]"]],
            "on_dataset": ["[[ultradomain]]"],
        }
    ]
    return payload


class TestBuildBatch:
    """`note build-batch` builds many payloads in one pass — clean ones write, failures are reported, exit 1 if any failed."""

    def test_clean_batch_builds_all_and_exits_zero(self, tmp_path: Path):
        knowledge_base = _build_knowledge_base(tmp_path / "knowledge_base")
        out_a, out_b = tmp_path / "out" / "a.md", tmp_path / "out" / "b.md"
        manifest = tmp_path / "m.json"
        manifest.write_text(
            json.dumps(
                [
                    {"out": str(out_a), "payload": _paper_payload(id="paper.a-2025")},
                    {"out": str(out_b), "payload": _paper_payload(id="paper.b-2025")},
                ]
            ),
            encoding="utf-8",
        )

        result = runner.invoke(
            app, ["note", "build-batch", "--manifest", str(manifest), "--knowledge-base", str(knowledge_base.path)], obj=_factory(knowledge_base)
        )

        assert result.exit_code == 0
        assert out_a.exists()
        assert out_b.exists()
        assert "built 2/2" in result.stdout

    def test_accepts_vault_alias_identically_and_warns(self, tmp_path: Path):
        knowledge_base = _build_knowledge_base(tmp_path / "knowledge_base")
        out_a = tmp_path / "out" / "a.md"
        manifest = tmp_path / "m.json"
        manifest.write_text(json.dumps([{"out": str(out_a), "payload": _paper_payload(id="paper.a-2025")}]), encoding="utf-8")

        result = runner.invoke(app, ["note", "build-batch", "--manifest", str(manifest), "--vault", str(knowledge_base.path)], obj=_factory(None))

        assert result.exit_code == 0, result.stdout
        assert out_a.exists()
        assert "--vault is deprecated" in result.stderr

    def test_one_failing_entry_does_not_block_the_rest(self, tmp_path: Path):
        # The failing entry carries an error-tier issue (a comparative claim with no metric).
        knowledge_base = _build_knowledge_base(tmp_path / "knowledge_base")
        good, bad = tmp_path / "out" / "good.md", tmp_path / "out" / "bad.md"
        manifest = tmp_path / "m.json"
        manifest.write_text(
            json.dumps(
                [
                    {"out": str(good), "payload": _paper_payload(id="paper.good-2025")},
                    {"out": str(bad), "payload": _comparative_without_metric(_paper_payload(id="paper.bad-2025"))},
                ]
            ),
            encoding="utf-8",
        )

        result = runner.invoke(
            app, ["note", "build-batch", "--manifest", str(manifest), "--knowledge-base", str(knowledge_base.path)], obj=_factory(knowledge_base)
        )

        assert result.exit_code == 1
        assert good.exists()  # the clean note still built
        assert not bad.exists()  # the failing note wrote nothing (per-note fail-before-write)
        assert "built 1/2" in result.stdout
        assert "FAILED" in result.stdout

    def test_malformed_manifest_exits_one(self, tmp_path: Path):
        knowledge_base = _build_knowledge_base(tmp_path / "knowledge_base")
        manifest = tmp_path / "m.json"
        manifest.write_text(json.dumps({"not": "a list"}), encoding="utf-8")

        result = runner.invoke(
            app, ["note", "build-batch", "--manifest", str(manifest), "--knowledge-base", str(knowledge_base.path)], obj=_factory(knowledge_base)
        )

        assert result.exit_code == 1
        assert "must be a JSON array" in result.stdout


class TestVerify:
    def test_all_quotes_found_exits_zero(self, tmp_path: Path):
        # A claim whose quote appears verbatim in the golden mirror excerpt verifies clean.
        knowledge_base = _build_knowledge_base(tmp_path / "knowledge_base")
        note = _write_note(tmp_path, _verifiable_note_text(), name="lightrag.md")

        result = runner.invoke(app, ["note", "verify", str(note), "--knowledge-base", str(knowledge_base.path)], obj=_factory(knowledge_base))

        assert result.exit_code == 0, result.stdout

    def test_unfound_quote_exits_one(self, tmp_path: Path):
        knowledge_base = _build_knowledge_base(tmp_path / "knowledge_base")
        text = _verifiable_note_text().replace(_MIRROR_QUOTE, "a sentence that is definitely not present in the mirror text", 1)
        note = _write_note(tmp_path, text, name="bad-quote.md")

        result = runner.invoke(app, ["note", "verify", str(note), "--knowledge-base", str(knowledge_base.path)], obj=_factory(knowledge_base))

        assert result.exit_code == 1

    def test_missing_mirror_exits_one(self, tmp_path: Path):
        # Knowledge base with no matching mirror in Sources/ → the quote can't be verified → exit 1.
        knowledge_base = _build_knowledge_base(tmp_path / "knowledge_base", with_mirror=False)
        note = _write_note(tmp_path, _verifiable_note_text(), name="lightrag.md")

        result = runner.invoke(app, ["note", "verify", str(note), "--knowledge-base", str(knowledge_base.path)], obj=_factory(knowledge_base))

        assert result.exit_code == 1

    def test_multi_snippet_quote_all_present_exits_zero(self, tmp_path: Path):
        # A table-derived quote packing two "..." snippets, both verbatim in the mirror,
        # verifies clean — the whole value is no contiguous mirror substring, so this
        # passes only because each snippet is matched independently (quotes_in_mirror).
        knowledge_base = _build_knowledge_base(tmp_path / "knowledge_base")
        note = _write_note(tmp_path, _multi_snippet_note_text(_MIRROR_SNIPPET_A, _MIRROR_SNIPPET_B), name="multi.md")

        result = runner.invoke(app, ["note", "verify", str(note), "--knowledge-base", str(knowledge_base.path)], obj=_factory(knowledge_base))

        assert result.exit_code == 0, result.stdout

    def test_multi_snippet_quote_one_absent_exits_one(self, tmp_path: Path):
        # Same packed value but the second snippet is absent from the mirror → exit 1.
        # A whole-value substring match would also miss, but the point is that one
        # unverifiable snippet fails the claim even when the other is present.
        knowledge_base = _build_knowledge_base(tmp_path / "knowledge_base")
        text = _multi_snippet_note_text(_MIRROR_SNIPPET_A, "this exact snippet is not in the mirror")
        note = _write_note(tmp_path, text, name="multi-bad.md")

        result = runner.invoke(app, ["note", "verify", str(note), "--knowledge-base", str(knowledge_base.path)], obj=_factory(knowledge_base))

        assert result.exit_code == 1


def _paper_spec(*, key_field: str) -> KnowledgeBaseSchema:
    """A minimal `paper`-kind spec matching `_verifiable_note_text`'s shape, `sources.key_field` set to `key_field`.

    Self-contained rather than built off any shipped default: only the fields
    `_verifiable_note_text` actually writes (`paper_type`, `year`, `zotero_key`, `source_hash`),
    one canonical section (`findings`, matching the note's `## Findings` heading), and the claim
    grammar its one claim's `loc::`/`quote::`/`kind::` bullets need.
    """
    return KnowledgeBaseSchema(
        slug="quote-test",
        page_types=(
            PageTypeSpec(
                page_type="paper",
                id_prefix="paper.",
                frontmatter_fields=(
                    FrontmatterFieldSpec(name="paper_type", type="scalar", required=True),
                    FrontmatterFieldSpec(name="year", type="int", required=True),
                    FrontmatterFieldSpec(name="zotero_key", type="scalar", required=True),
                    FrontmatterFieldSpec(name="source_hash", type="scalar", required=True),
                ),
                canonical_sections=(CanonicalSection(slug="findings"),),
                claims_enabled=True,
            ),
        ),
        claims=ClaimGrammarSpec(section="findings", plain_fields=("loc", "quote", "kind")),
        sources=SourcesSpec(key_field=key_field),
    )


class TestQuoteVerificationUsesTheSpecOwnFieldNames:
    """The page side of the join is `spec.sources.key_field`; the mirror side is the mirror's own key, via `MirrorDoc`."""

    def test_find_mirror_text_matches_the_mirrors_own_key(self, tmp_path: Path) -> None:
        sources = tmp_path / "Sources"
        sources.mkdir()
        # A mirror carrying some other knowledge base's page-side field name only: invisible to this lookup,
        # which reads the mirror's own key, never the field a page happens to cite it under.
        (sources / "other-field.md").write_text(f"---\nzotero_key: {_LIGHTRAG_ZOTERO_KEY}\n---\n\nWrong mirror.\n", encoding="utf-8")
        (sources / "keyed.md").write_text("---\nsource_key: NEW-1\n---\n\nRight mirror.\n", encoding="utf-8")

        found = _find_mirror_text(tmp_path, "NEW-1")

        assert found is not None
        assert "Right mirror." in found
        assert _find_mirror_text(tmp_path, _LIGHTRAG_ZOTERO_KEY) is None

    def test_find_mirror_text_falls_back_to_the_filename_stem(self, tmp_path: Path) -> None:
        """A mirror naming no key answers under its own filename stem, the same rule `MirrorDoc` applies."""
        sources = tmp_path / "Sources"
        sources.mkdir()
        (sources / "unkeyed-mirror.md").write_text("---\ntitle: T\n---\n\nRight mirror.\n", encoding="utf-8")

        found = _find_mirror_text(tmp_path, "unkeyed-mirror")

        assert found is not None
        assert "Right mirror." in found

    def test_quote_failures_resolves_the_mirror_through_the_spec_own_key_field(self, tmp_path: Path) -> None:
        spec = _paper_spec(key_field="source_key")
        sources = tmp_path / "Sources"
        sources.mkdir()
        # A decoy mirror that would match under the old, hardcoded `zotero_key` field but
        # carries none of the quoted text -- if the lookup ever fell back to that literal,
        # this decoy would be the one found, and the quote check below would fail.
        (sources / "old-literal.md").write_text(f"---\nzotero_key: {_LIGHTRAG_ZOTERO_KEY}\n---\n\nUnrelated text.\n", encoding="utf-8")
        (sources / "renamed.md").write_text(f"---\nsource_key: NEW-1\n---\n\n{_MIRROR_QUOTE}\n", encoding="utf-8")
        note_text = _verifiable_note_text().replace("---\n\n## Findings", "source_key: NEW-1\n---\n\n## Findings", 1)
        note = kb.parse(note_text, source_path="note.md", spec=spec)

        failures = _quote_failures(note, tmp_path, spec)

        assert failures == []

    def test_quote_failures_reports_no_mirror_when_only_the_old_literal_field_is_present(self, tmp_path: Path) -> None:
        spec = _paper_spec(key_field="source_key")
        sources = tmp_path / "Sources"
        sources.mkdir()
        (sources / "old-literal.md").write_text(f"---\nzotero_key: {_LIGHTRAG_ZOTERO_KEY}\n---\n\n{_MIRROR_QUOTE}\n", encoding="utf-8")
        note_text = _verifiable_note_text().replace("---\n\n## Findings", "source_key: NEW-1\n---\n\n## Findings", 1)
        note = kb.parse(note_text, source_path="note.md", spec=spec)

        failures = _quote_failures(note, tmp_path, spec)

        assert len(failures) == 1
        assert "source_key" in failures[0]
        assert "no Sources/ mirror found" in failures[0]


# ----- schema -----------------------------------------------------------------


class TestSchema:
    def test_paper_schema_is_valid_json(self, tmp_path: Path):
        # No injected factory: schema is a pure kernel function and must NOT touch the
        # container/settings, only the knowledge base's own spec (the default-factory path).
        knowledge_base = _build_knowledge_base(tmp_path / "knowledge_base")
        result = runner.invoke(app, ["note", "schema", "--type", "paper", "--knowledge-base", str(knowledge_base.path)])

        assert result.exit_code == 0, result.stdout
        schema = json.loads(result.stdout)
        assert isinstance(schema, dict)
        assert "properties" in schema

    def test_schema_carries_the_type_distinguishing_fields(self, tmp_path: Path):
        knowledge_base = _build_knowledge_base(tmp_path / "knowledge_base")
        result = runner.invoke(app, ["note", "schema", "--type", "paper", "--knowledge-base", str(knowledge_base.path)])

        # The paper frontmatter's distinguishing keys must surface somewhere in the schema text.
        assert "zotero_key" in result.stdout
        assert "year" in result.stdout

    def test_unknown_type_exits_one(self, tmp_path: Path):
        knowledge_base = _build_knowledge_base(tmp_path / "knowledge_base")
        result = runner.invoke(app, ["note", "schema", "--type", "not-a-type", "--knowledge-base", str(knowledge_base.path)])

        assert result.exit_code == 1

    def test_a_knowledge_base_with_no_spec_exits_one_naming_the_fix(self, tmp_path: Path):
        bare = tmp_path / "bare"
        bare.mkdir()
        result = runner.invoke(app, ["note", "schema", "--type", "paper", "--knowledge-base", str(bare)])

        assert result.exit_code == 1
        assert "studio init --template" in result.stdout

    def test_help_shows_the_knowledge_base_default_dot(self):
        # `--knowledge-base`'s literal default is "." (unlike the other five note subcommands) so an
        # operator sees it right in --help, not just in prose — pins the marker so a future
        # refactor (e.g. switching to a None default for --vault composability) can't drop it silently.
        result = runner.invoke(app, ["note", "schema", "--help"])

        assert "[default: .]" in result.stdout


@pytest.mark.parametrize("subcommand", ["validate", "build", "build-batch", "verify", "normalize", "schema"])
def test_subcommand_help_exits_zero(subcommand: str):
    result = runner.invoke(app, ["note", subcommand, "--help"])

    assert result.exit_code == 0


@pytest.mark.parametrize("subcommand", ["validate", "build", "build-batch", "verify", "normalize", "schema"])
def test_subcommand_help_hides_vault_flag(subcommand: str):
    # --vault is a deprecated alias of --knowledge-base on every one of these, kept out of --help.
    result = runner.invoke(app, ["note", subcommand, "--help"])

    assert "--vault" not in result.stdout


# ---------------------------------------------------------------------------
# note schema --type <kind> honors the knowledge base's spec
# ---------------------------------------------------------------------------


def _write_cooking_spec(knowledge_base_root: Path) -> Path:
    """Write a minimal cooking knowledge-base-schema.json to `knowledge_base_root` and return the path.

    `recipe`'s claim grammar deliberately differs from the research template's schema (own edge
    relations, own field subset, own quote-required kind, own claim-kind vocabulary) so a schema
    test can tell the cooking knowledge base's own grammar apart from the research default.
    """
    spec = KnowledgeBaseSchema(
        slug="cooking",
        purpose="A cooking knowledge knowledge base",
        page_types=(PageTypeSpec(page_type="recipe", id_prefix="recipe.", canonical_sections=(CanonicalSection(slug="overview"), CanonicalSection(slug="method")), claims_enabled=True),),
        facets=(FacetSpec(key="technique", seed=("roasting", "frying")),),
        claim_types=(
            VocabEntry(value="technique-tip", definition="A technique worth calling out"),
            VocabEntry(value="substitution", definition="An ingredient swap"),
        ),
        claims=ClaimGrammarSpec(
            section="findings",
            edge_relations=("pairs_well_with",),
            multilink_fields=("on_dataset",),
            link_scalar_fields=("subject",),
            text_scalar_fields=("baseline",),
            plain_fields=("loc", "quote", "kind"),
            quote_required_types=("technique-tip",),
        ),
    )
    spec_path = knowledge_base_root / "knowledge-base-schema.json"
    spec_path.write_text(spec.model_dump_json(indent=2), encoding="utf-8")
    return spec_path


class TestSchemaNoteSchema:
    """`note schema --type X` must respect the knowledge base's spec.

    The command loads the knowledge base's knowledge-base-schema.json and resolves the type against that spec's
    actual kinds, never a hardcoded research kind set — so `recipe` works for a cooking knowledge base
    and `paper` still works for the default research knowledge_base.
    """

    def test_recipe_schema_emitted_for_cooking_knowledge_base(self, tmp_path: Path):
        # Point --knowledge-base at a directory containing a minimal cooking knowledge-base-schema.json.
        # The command must exit 0 and emit a JSON schema with recipe-specific shape.
        knowledge_base = tmp_path / "cooking-knowledge-base"
        knowledge_base.mkdir()
        _write_cooking_spec(knowledge_base)

        result = runner.invoke(app, ["note", "schema", "--type", "recipe", "--knowledge-base", str(knowledge_base)])

        assert result.exit_code == 0, result.stdout
        schema = json.loads(result.stdout)
        assert isinstance(schema, dict)
        assert schema.get("title") == "AuthoringPayload[recipe]"

        # The cooking knowledge base's own claim grammar, not the research template's, reaches
        # the CLI's emitted claims.items.
        claim_props = schema["properties"]["claims"]["items"]["properties"]
        assert "subject" in claim_props  # cooking's own link_scalar_fields
        assert "baseline" in claim_props  # cooking's own text_scalar_fields
        assert "on_dataset" in claim_props  # cooking's own multilink_fields
        assert "on_task" not in claim_props  # not declared by the cooking grammar
        assert set(claim_props["kind"]["enum"]) == {"technique-tip", "substitution"}
        assert "technique-tip: A technique worth calling out" in claim_props["kind"]["description"]
        assert "technique-tip" in claim_props["quote"]["description"]
        relation_slot = schema["properties"]["claims"]["items"]["properties"]["edges"]["items"]["prefixItems"][0]
        assert relation_slot["enum"] == ["pairs_well_with"]

    def test_recipe_schema_via_vault_alias_matches_knowledge_base_and_warns(self, tmp_path: Path):
        # --vault resolves the same knowledge base spec as --knowledge-base, plus the deprecation warning.
        knowledge_base = tmp_path / "cooking-knowledge-base"
        knowledge_base.mkdir()
        _write_cooking_spec(knowledge_base)

        knowledge_base_result = runner.invoke(app, ["note", "schema", "--type", "recipe", "--knowledge-base", str(knowledge_base)])
        vault_result = runner.invoke(app, ["note", "schema", "--type", "recipe", "--vault", str(knowledge_base)])

        assert vault_result.exit_code == 0, vault_result.stdout
        assert json.loads(vault_result.stdout) == json.loads(knowledge_base_result.stdout)
        assert "--vault is deprecated" in vault_result.stderr

    def test_explicit_knowledge_base_wins_over_vault_when_both_given(self, tmp_path: Path):
        # note schema's --knowledge-base keeps a literal "." default (not None, so --help keeps
        # showing it) — an explicit --knowledge-base still wins over --vault when both are given.
        knowledge_base = tmp_path / "cooking-knowledge-base"
        knowledge_base.mkdir()
        _write_cooking_spec(knowledge_base)
        other = tmp_path / "empty-knowledge-base"
        other.mkdir()

        result = runner.invoke(app, ["note", "schema", "--type", "recipe", "--knowledge-base", str(knowledge_base), "--vault", str(other)])

        assert result.exit_code == 0, result.stdout
        schema = json.loads(result.stdout)
        assert schema["title"] == "AuthoringPayload[recipe]"  # resolved against --knowledge-base, not --vault

    def test_explicit_knowledge_base_dot_still_wins_over_vault(self, tmp_path: Path):
        # The regression this pins: "." is both the default value AND a value someone can type
        # explicitly. --knowledge-base . is an explicit choice, not an omission, so it must still win over
        # --vault (Click's parameter-source tracking tells the two apart, a string comparison can't).
        vault_knowledge_base = tmp_path / "cooking-knowledge-base"
        vault_knowledge_base.mkdir()
        _write_cooking_spec(vault_knowledge_base)

        result = runner.invoke(app, ["note", "schema", "--type", "recipe", "--knowledge-base", ".", "--vault", str(vault_knowledge_base)])

        # --knowledge-base . resolves against the cwd, which carries no knowledge-base-schema.json (refused) — this
        # only fails naming "." if --knowledge-base actually won over the vault's cooking spec.
        assert result.exit_code == 1, result.stdout
        assert "knowledge-base-schema.json at ." in result.stdout
        assert "--vault is deprecated" in result.stderr  # the warning still fires even though vault loses

    def test_unknown_type_for_cooking_knowledge_base_exits_one(self, tmp_path: Path):
        # "paper" is not a kind in the cooking knowledge base → must exit 1 with a clear message.
        knowledge_base = tmp_path / "cooking-knowledge-base"
        knowledge_base.mkdir()
        _write_cooking_spec(knowledge_base)

        result = runner.invoke(app, ["note", "schema", "--type", "paper", "--knowledge-base", str(knowledge_base)])

        assert result.exit_code == 1
        assert "recipe" in result.stdout  # valid kinds listed in the error message

    def test_no_knowledge_base_flag_refuses_without_a_knowledge_base_schema(self):
        # No --knowledge-base → defaults to "." → the test runner's cwd, which carries no
        # knowledge-base-schema.json refused, naming a template to start from.
        result = runner.invoke(app, ["note", "schema", "--type", "paper"])

        assert result.exit_code == 1
        assert "studio init --template" in result.stdout

    def test_an_empty_knowledge_base_dir_refuses_without_a_knowledge_base_schema(self, tmp_path: Path):
        # An empty knowledge base dir (no knowledge-base-schema.json) is refused, same as the cwd default.
        knowledge_base = tmp_path / "empty-knowledge-base"
        knowledge_base.mkdir()

        result = runner.invoke(app, ["note", "schema", "--type", "paper", "--knowledge-base", str(knowledge_base)])

        assert result.exit_code == 1
        assert "studio init --template" in result.stdout


# ---------------------------------------------------------------------------
# Authoring gaps regression: note build / verify honor --knowledge-base and load the
# knowledge base's OWN spec — so the plugin authors into any knowledge base by CWD (--knowledge-base .)
# with no spec embedded in the payload.
# ---------------------------------------------------------------------------


def _build_cooking_knowledge_base(root: Path) -> Path:
    """A minimal non-research knowledge base: a cooking `knowledge-base-schema.json` + an empty vocabulary dir."""
    root.mkdir(parents=True, exist_ok=True)
    _write_cooking_spec(root)
    (root / "_meta" / "vocabulary").mkdir(parents=True)
    return root


def _recipe_payload() -> dict[str, JsonValue]:
    """A plain `recipe` payload as an extractor emits it — note the absent `spec` key."""
    frontmatter: dict[str, JsonValue] = {"id": "recipe.pancakes", "type": "recipe", "title": "Pancakes", "status": "draft"}
    sections: list[JsonValue] = [{"heading": "Overview", "body": "A fluffy stack."}, {"heading": "Method", "body": "Mix, then fry."}]
    return {"frontmatter": frontmatter, "sections": sections, "claims": []}


class TestAuthoringHonorsKnowledgeBaseFlag:
    """`note build`/`verify` resolve spec + vocabulary from `--knowledge-base` (the plugin's CWD path).

    With `--knowledge-base` the container is never consulted and the knowledge base's own `knowledge-base-schema.json`
    validates the frontmatter, with no full `spec` embedded in the payload required to author against a
    non-research knowledge base.
    """

    def test_build_uses_knowledge_base_spec_and_bypasses_the_container(self, tmp_path: Path):
        # No container knowledge base (factory(None)); --knowledge-base supplies a cooking knowledge_base. A plain
        # recipe payload (NO embedded spec) builds clean because the CLI injects the
        # cooking spec — proving both workarounds are unnecessary.
        knowledge_base = _build_cooking_knowledge_base(tmp_path / "cooking")
        payload = _write_payload(tmp_path / "p.json", _recipe_payload())
        out = tmp_path / "recipe.md"

        result = runner.invoke(
            app, ["note", "build", "--payload", str(payload), "--out", str(out), "--knowledge-base", str(knowledge_base)], obj=_factory(None)
        )

        assert result.exit_code == 0, result.stdout
        text = out.read_text(encoding="utf-8")
        assert "id: recipe.pancakes" in text
        assert "type: recipe" in text

    def test_build_accepts_vault_alias_identically_and_warns(self, tmp_path: Path):
        # --vault behaves exactly like --knowledge-base for build, plus the deprecation warning.
        knowledge_base = _build_cooking_knowledge_base(tmp_path / "cooking")
        payload = _write_payload(tmp_path / "p.json", _recipe_payload())
        out = tmp_path / "recipe.md"

        result = runner.invoke(
            app, ["note", "build", "--payload", str(payload), "--out", str(out), "--vault", str(knowledge_base)], obj=_factory(None)
        )

        assert result.exit_code == 0, result.stdout
        text = out.read_text(encoding="utf-8")
        assert "id: recipe.pancakes" in text
        assert "type: recipe" in text
        assert "--vault is deprecated" in result.stderr

    def test_verify_uses_knowledge_base_flag_and_bypasses_the_container(self, tmp_path: Path):
        # A recipe note with no quote claims verifies clean; --knowledge-base resolves the cooking
        # spec so the note parses, with the container resolving no knowledge base at all.
        knowledge_base = _build_cooking_knowledge_base(tmp_path / "cooking")
        note_text = (
            "---\nid: recipe.pancakes\ntype: recipe\ntitle: Pancakes\nstatus: draft\n---\n\n## Overview\n\nFluffy.\n\n## Method\n\nFry.\n"
        )
        note = _write_note(tmp_path, note_text, name="recipe.md")

        result = runner.invoke(app, ["note", "verify", str(note), "--knowledge-base", str(knowledge_base)], obj=_factory(None))

        assert result.exit_code == 0, result.stdout

    def test_verify_accepts_vault_alias_identically_and_warns(self, tmp_path: Path):
        knowledge_base = _build_cooking_knowledge_base(tmp_path / "cooking")
        note_text = (
            "---\nid: recipe.pancakes\ntype: recipe\ntitle: Pancakes\nstatus: draft\n---\n\n## Overview\n\nFluffy.\n\n## Method\n\nFry.\n"
        )
        note = _write_note(tmp_path, note_text, name="recipe.md")

        result = runner.invoke(app, ["note", "verify", str(note), "--vault", str(knowledge_base)], obj=_factory(None))

        assert result.exit_code == 0, result.stdout
        assert "--vault is deprecated" in result.stderr


class TestNormalize:
    """`note normalize` merges engram frontmatter into a note IN PLACE, preserving its body."""

    def test_adds_frontmatter_and_preserves_body(self, tmp_path: Path):
        # No container knowledge base; --knowledge-base supplies the cooking knowledge_base. A raw note (no frontmatter)
        # gains id/type while its body is kept verbatim.
        knowledge_base = _build_cooking_knowledge_base(tmp_path / "cooking")
        note = _write_note(tmp_path, "Crispy edges, fluffy middle.\n", name="pancakes.md")
        fm: dict[str, JsonValue] = {"id": "recipe.pancakes", "type": "recipe", "title": "Pancakes", "status": "draft"}
        fm_path = _write_payload(tmp_path / "fm.json", fm)

        result = runner.invoke(
            app, ["note", "normalize", str(note), "--frontmatter", str(fm_path), "--knowledge-base", str(knowledge_base)], obj=_factory(None)
        )

        assert result.exit_code == 0, result.stdout
        text = note.read_text(encoding="utf-8")
        assert "id: recipe.pancakes" in text
        assert "type: recipe" in text
        assert "Crispy edges, fluffy middle." in text  # body preserved verbatim

    def test_accepts_vault_alias_identically_and_warns(self, tmp_path: Path):
        knowledge_base = _build_cooking_knowledge_base(tmp_path / "cooking")
        note = _write_note(tmp_path, "Crispy edges, fluffy middle.\n", name="pancakes.md")
        fm: dict[str, JsonValue] = {"id": "recipe.pancakes", "type": "recipe", "title": "Pancakes", "status": "draft"}
        fm_path = _write_payload(tmp_path / "fm.json", fm)

        result = runner.invoke(
            app, ["note", "normalize", str(note), "--frontmatter", str(fm_path), "--vault", str(knowledge_base)], obj=_factory(None)
        )

        assert result.exit_code == 0, result.stdout
        text = note.read_text(encoding="utf-8")
        assert "id: recipe.pancakes" in text
        assert "--vault is deprecated" in result.stderr

    def test_preserves_existing_non_engram_keys(self, tmp_path: Path):
        knowledge_base = _build_cooking_knowledge_base(tmp_path / "cooking")
        note = _write_note(tmp_path, "---\ncreated: 2024-01-01\n---\n\nMethod body.\n", name="r.md")
        fm: dict[str, JsonValue] = {"id": "recipe.r", "type": "recipe", "title": "R", "status": "draft"}
        fm_path = _write_payload(tmp_path / "fm.json", fm)

        result = runner.invoke(
            app, ["note", "normalize", str(note), "--frontmatter", str(fm_path), "--knowledge-base", str(knowledge_base)], obj=_factory(None)
        )

        assert result.exit_code == 0, result.stdout
        text = note.read_text(encoding="utf-8")
        assert "created: 2024-01-01" in text  # the user's existing key survives the merge
        assert "id: recipe.r" in text
        assert "Method body." in text

    def test_fails_before_write_on_unparseable_result(self, tmp_path: Path):
        # A type absent from the knowledge base spec makes the merged note unparseable → exit 1, file untouched.
        knowledge_base = _build_cooking_knowledge_base(tmp_path / "cooking")
        original = "Original body.\n"
        note = _write_note(tmp_path, original, name="bad.md")
        fm: dict[str, JsonValue] = {"id": "x.y", "type": "not-a-kind", "title": "X", "status": "draft"}
        fm_path = _write_payload(tmp_path / "fm.json", fm)

        result = runner.invoke(
            app, ["note", "normalize", str(note), "--frontmatter", str(fm_path), "--knowledge-base", str(knowledge_base)], obj=_factory(None)
        )

        assert result.exit_code == 1
        assert note.read_text(encoding="utf-8") == original  # untouched (fail-before-write)
