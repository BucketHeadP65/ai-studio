r"""Unit tests for the `studio init` CLI subcommand group (`src/studio/knowledge_base/cli.py`).

Tests cover four commands:

* `init scaffold` — pure scaffold (no DB, no container); creates `_meta/`
  files from a spec, idempotent by default, `--force` overwrites prose files.
* `init db` — DB URL resolution unit-tested without a live DB (`resolve_db_url`
  precedence: `--db-url` > env > JSON), plus the failure case when no URL is found.
* `init spec --interactive` — stdin-driven spec builder; a valid spec is written
  to disk (claims-disabled path and claims-enabled path both checked).
* ``load_vocab extraction parity`` — :func:`~engram.indexing.service.vocab.load_vocab`
  called with a :class:`FakeKnowledgeBaseUnitOfWork` produces exactly the rows
  `vocab_rows` yields.

The `init db` / `init all` real-DB smoke lives in the integration tier
(`tests/integration/test_init_db_smoke.py`, skipped when :5433 is down). These unit
tests never touch a live DB.
"""

from __future__ import annotations

import json
import os

from typing import TYPE_CHECKING

import pytest

from core.cqrs.contracts import MessageBus
from engram.indexing.contracts import IndexKnowledgeBase, IndexReport, load_vocab
from engram.indexing.service.vocab import vocab_rows
from engram.init.contracts import resolve_db_url
from engram.testing.fakes.bus_spy import RecordingCommandHandler
from engram.testing.fakes.uow import FakeKnowledgeBaseUnitOfWork
from typer.testing import CliRunner

from studio.knowledge_base import cli_init as cli_init_module
from studio.knowledge_base.cli import app

if TYPE_CHECKING:
    from pathlib import Path

    import typer

    from core.resolver import Resolver

    from studio.knowledge_base.cli_support import ActiveKnowledgeBase
    from studio.knowledge_base.settings import KnowledgeBasePathOrigin

runner = CliRunner()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _minimal_spec_json(slug: str = "test", claims: bool = False) -> str:
    """Return a minimal valid knowledge-base-schema.json string.

    Args:
        slug: The knowledge base slug.
        claims: Whether to add a claims-enabled page type + ClaimGrammarSpec.

    Returns:
        JSON string suitable for writing to a temp file.
    """
    page_types = [{"page_type": "concept", "id_prefix": "concept", "canonical_sections": ["overview"], "claims_enabled": False}]
    if claims:
        page_types.append({"page_type": "paper", "id_prefix": "paper", "canonical_sections": ["findings"], "claims_enabled": True})
    spec: dict[str, object] = {"version": "0.1", "slug": slug, "purpose": "Test knowledge base", "page_types": page_types}
    if claims:
        spec["claims"] = {"section": "findings", "text_scalar_fields": ["result"]}
    return json.dumps(spec)


# ---------------------------------------------------------------------------
# 1.  init scaffold — pure, no DB
# ---------------------------------------------------------------------------


class TestInitScaffold:
    """`init scaffold` creates knowledge base files from a KnowledgeBaseSchema JSON."""

    def test_scaffold_creates_meta_files(self, tmp_path: Path) -> None:
        spec_path = tmp_path / "knowledge-base-schema.json"
        spec_path.write_text(_minimal_spec_json(), encoding="utf-8")
        knowledge_base = tmp_path / "knowledge_base"
        knowledge_base.mkdir()

        result = runner.invoke(app, ["init", "scaffold", "--spec", str(spec_path), "--knowledge-base", str(knowledge_base)])

        assert result.exit_code == 0, result.output
        assert (knowledge_base / "_meta" / "vocabulary" / "entity-types.md").exists()
        assert (knowledge_base / "_meta" / "vocabulary" / "relation-types.md").exists()
        assert (knowledge_base / "_meta" / "vocabulary" / "facets.md").exists()
        assert (knowledge_base / "knowledge-base-schema.json").exists()

    def test_scaffold_creates_template_for_non_meta_type(self, tmp_path: Path) -> None:
        spec_path = tmp_path / "knowledge-base-schema.json"
        spec_path.write_text(_minimal_spec_json(), encoding="utf-8")
        knowledge_base = tmp_path / "knowledge_base"
        knowledge_base.mkdir()

        result = runner.invoke(app, ["init", "scaffold", "--spec", str(spec_path), "--knowledge-base", str(knowledge_base)])

        assert result.exit_code == 0, result.output
        assert (knowledge_base / "_meta" / "templates" / "tpl-concept.md").exists()

    def test_scaffold_is_idempotent_by_default(self, tmp_path: Path) -> None:
        spec_path = tmp_path / "knowledge-base-schema.json"
        spec_path.write_text(_minimal_spec_json(), encoding="utf-8")
        knowledge_base = tmp_path / "knowledge_base"
        knowledge_base.mkdir()

        runner.invoke(app, ["init", "scaffold", "--spec", str(spec_path), "--knowledge-base", str(knowledge_base)])
        result = runner.invoke(app, ["init", "scaffold", "--spec", str(spec_path), "--knowledge-base", str(knowledge_base)])

        assert result.exit_code == 0, result.output
        # second run reports skipped files (prose/template), merged for vocabularies
        assert "skipped" in result.output or "merged" in result.output or "nothing to do" in result.output

    def test_scaffold_force_overwrites_prose(self, tmp_path: Path) -> None:
        spec_path = tmp_path / "knowledge-base-schema.json"
        spec_path.write_text(_minimal_spec_json(), encoding="utf-8")
        knowledge_base = tmp_path / "knowledge_base"
        knowledge_base.mkdir()

        runner.invoke(app, ["init", "scaffold", "--spec", str(spec_path), "--knowledge-base", str(knowledge_base)])
        result = runner.invoke(app, ["init", "scaffold", "--spec", str(spec_path), "--knowledge-base", str(knowledge_base), "--force"])

        assert result.exit_code == 0, result.output
        assert "replaced" in result.output

    def test_scaffold_defaults_to_knowledge_base_knowledge_base_schema_json(self, tmp_path: Path) -> None:
        """When `--spec` is omitted, uses `<knowledge_base>/knowledge-base-schema.json` if present."""
        knowledge_base = tmp_path / "knowledge_base"
        knowledge_base.mkdir()
        (knowledge_base / "knowledge-base-schema.json").write_text(_minimal_spec_json(), encoding="utf-8")

        result = runner.invoke(app, ["init", "scaffold", "--knowledge-base", str(knowledge_base)])

        assert result.exit_code == 0, result.output

    def test_scaffold_fails_when_no_spec(self, tmp_path: Path) -> None:
        """`_fail` when neither `--spec` nor `<knowledge_base>/knowledge-base-schema.json` exists."""
        knowledge_base = tmp_path / "knowledge_base"
        knowledge_base.mkdir()

        result = runner.invoke(app, ["init", "scaffold", "--knowledge-base", str(knowledge_base)])

        assert result.exit_code == 1

    def test_scaffold_prints_summary(self, tmp_path: Path) -> None:
        spec_path = tmp_path / "knowledge-base-schema.json"
        spec_path.write_text(_minimal_spec_json(), encoding="utf-8")
        knowledge_base = tmp_path / "knowledge_base"
        knowledge_base.mkdir()

        result = runner.invoke(app, ["init", "scaffold", "--spec", str(spec_path), "--knowledge-base", str(knowledge_base)])

        assert result.exit_code == 0, result.output
        assert "created" in result.output


# ---------------------------------------------------------------------------
# 1b.  init --template: bootstrap a fresh knowledge base from a named template
# ---------------------------------------------------------------------------


def _template_dir(templates_root: Path, slug: str, *, purpose: str = "") -> Path:
    """A fake template directory under `templates_root`: `knowledge-base-schema.json`, a titled README, and optional `_meta/purpose.md`."""
    template_path = templates_root / slug
    template_path.mkdir(parents=True)
    (template_path / "knowledge-base-schema.json").write_text(_minimal_spec_json(slug), encoding="utf-8")
    (template_path / "README.md").write_text(f"# {slug}\n", encoding="utf-8")
    if purpose:
        meta = template_path / "_meta"
        meta.mkdir()
        (meta / "purpose.md").write_text(purpose, encoding="utf-8")
    return template_path


class TestInitTemplate:
    """`studio init --template <slug>` copies a template's files into a fresh knowledge base, then runs the scaffold pipeline."""

    def test_copies_the_template_schema_and_scaffolds(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        templates_root = tmp_path / "templates"
        _template_dir(templates_root, "widgets", purpose="# Widgets purpose\n\nTemplate text.\n")
        monkeypatch.setenv("ENGRAM_TEMPLATES", str(templates_root))
        knowledge_base = tmp_path / "knowledge_base"
        knowledge_base.mkdir()

        result = runner.invoke(app, ["init", "--template", "widgets", "--knowledge-base", str(knowledge_base)])

        assert result.exit_code == 0, result.output
        from engram.kb.contracts import KnowledgeBaseSchema

        spec = KnowledgeBaseSchema.from_path(knowledge_base / "knowledge-base-schema.json")
        assert spec.slug == "widgets"
        # The template's own purpose.md survives the scaffold write (skip-existing on a non-vocabulary file).
        assert (knowledge_base / "_meta" / "purpose.md").read_text(encoding="utf-8") == "# Widgets purpose\n\nTemplate text.\n"
        # The scaffold pipeline still ran: vocabulary files not shipped by the template are generated fresh.
        assert (knowledge_base / "_meta" / "vocabulary" / "entity-types.md").exists()

    def test_refuses_an_existing_knowledge_base_schema_json(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        templates_root = tmp_path / "templates"
        _template_dir(templates_root, "widgets")
        monkeypatch.setenv("ENGRAM_TEMPLATES", str(templates_root))
        knowledge_base = tmp_path / "knowledge_base"
        knowledge_base.mkdir()
        (knowledge_base / "knowledge-base-schema.json").write_text(_minimal_spec_json("other"), encoding="utf-8")

        result = runner.invoke(app, ["init", "--template", "widgets", "--knowledge-base", str(knowledge_base)])

        assert result.exit_code == 1
        from engram.kb.contracts import KnowledgeBaseSchema

        spec = KnowledgeBaseSchema.from_path(knowledge_base / "knowledge-base-schema.json")
        assert spec.slug == "other"

    def test_slug_names_the_new_knowledge_base_and_its_collection(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        templates_root = tmp_path / "templates"
        _template_dir(templates_root, "widgets")
        monkeypatch.setenv("ENGRAM_TEMPLATES", str(templates_root))
        knowledge_base = tmp_path / "knowledge_base"
        knowledge_base.mkdir()

        result = runner.invoke(app, ["init", "--template", "widgets", "--slug", "gadgets", "--knowledge-base", str(knowledge_base)])

        assert result.exit_code == 0, result.output
        from engram.kb.contracts import KnowledgeBaseSchema

        spec = KnowledgeBaseSchema.from_path(knowledge_base / "knowledge-base-schema.json")
        assert spec.slug == "gadgets"
        assert spec.retrieval.collection == "gadgets"
        assert json.loads((templates_root / "widgets" / "knowledge-base-schema.json").read_text(encoding="utf-8"))["slug"] == "widgets"

    def test_refuses_a_slug_the_schema_would_refuse(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        templates_root = tmp_path / "templates"
        _template_dir(templates_root, "widgets")
        monkeypatch.setenv("ENGRAM_TEMPLATES", str(templates_root))
        knowledge_base = tmp_path / "knowledge_base"
        knowledge_base.mkdir()

        result = runner.invoke(app, ["init", "--template", "widgets", "--slug", "Not A Slug", "--knowledge-base", str(knowledge_base)])

        assert result.exit_code == 1
        assert not (knowledge_base / "knowledge-base-schema.json").exists()

    def test_unknown_template_names_the_known_slugs(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        templates_root = tmp_path / "templates"
        _template_dir(templates_root, "widgets")
        monkeypatch.setenv("ENGRAM_TEMPLATES", str(templates_root))
        knowledge_base = tmp_path / "knowledge_base"
        knowledge_base.mkdir()

        result = runner.invoke(app, ["init", "--template", "nonexistent", "--knowledge-base", str(knowledge_base)])

        assert result.exit_code == 1
        assert "widgets" in result.output

    def test_a_subcommand_runs_unaffected_by_the_groups_own_callback(self, tmp_path: Path) -> None:
        spec_path = tmp_path / "knowledge-base-schema.json"
        spec_path.write_text(_minimal_spec_json(), encoding="utf-8")
        knowledge_base = tmp_path / "knowledge_base"
        knowledge_base.mkdir()

        result = runner.invoke(app, ["init", "scaffold", "--spec", str(spec_path), "--knowledge-base", str(knowledge_base)])

        assert result.exit_code == 0, result.output
        assert (knowledge_base / "knowledge-base-schema.json").exists()


# ---------------------------------------------------------------------------
# 2.  init db — resolve_db_url unit tests (no live DB)
# ---------------------------------------------------------------------------


class TestResolveDbUrl:
    """Unit-test `resolve_db_url` precedence without a live database."""

    def test_override_wins_over_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("APP_INFRASTRUCTURE__DATABASE__URL", "postgresql+asyncpg://env/db")
        url = resolve_db_url("postgresql+asyncpg://explicit/db")
        assert url == "postgresql+asyncpg://explicit/db"

    def test_env_wins_over_json(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("APP_INFRASTRUCTURE__DATABASE__URL", "postgresql+asyncpg://env/db")
        # Even if the JSON file exists with a URL, env wins
        url = resolve_db_url(None)
        assert url == "postgresql+asyncpg://env/db"

    def test_json_fallback_when_no_env(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        import engram.init.infrastructure.db as db_mod

        monkeypatch.delenv("APP_INFRASTRUCTURE__DATABASE__URL", raising=False)
        json_cfg = tmp_path / "application_settings.json"
        json_cfg.write_text(json.dumps({"infrastructure": {"database": {"url": "postgresql+asyncpg://json/db"}}}), encoding="utf-8")
        monkeypatch.setattr(db_mod, "_CONFIG_JSON", json_cfg)

        assert resolve_db_url(None) == "postgresql+asyncpg://json/db"

    def test_raises_when_nothing_resolved(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        import engram.init.infrastructure.db as db_mod

        monkeypatch.delenv("APP_INFRASTRUCTURE__DATABASE__URL", raising=False)
        # Point config JSON at a non-existent file so JSON lookup returns None
        monkeypatch.setattr(db_mod, "_CONFIG_JSON", tmp_path / "nonexistent.json")

        with pytest.raises(ValueError, match="No database URL"):
            resolve_db_url(None)


# ---------------------------------------------------------------------------
# 3.  init spec --interactive — stdin-driven spec builder
# ---------------------------------------------------------------------------


class TestInitSpec:
    """`init spec --interactive` writes a valid KnowledgeBaseSchema to disk."""

    def test_spec_interactive_no_claims_writes_valid_spec(self, tmp_path: Path) -> None:
        """Claims-disabled path: no claim-grammar prompts; ClaimGrammarSpec omitted."""
        out = tmp_path / "knowledge-base-schema.json"
        # Inputs: slug, purpose, page type (kind, id_prefix, sections, claims enabled),
        # done adding types, no relations, no entity_types, no claim_types, no facets.
        inputs = (
            "my-knowledge-base\n"  # slug
            "A test knowledge base\n"  # purpose
            "concept\n"  # page type kind
            "concept\n"  # id_prefix
            "overview,background\n"  # sections CSV
            "n\n"  # claims enabled?
            "n\n"  # add another page type?
            "n\n"  # add relation?
            "n\n"  # add entity type?
            "n\n"  # add claim kind?
            "n\n"  # add facet?
            ""  # done
        )

        result = runner.invoke(app, ["init", "spec", "--interactive", "--out", str(out)], input=inputs)

        assert result.exit_code == 0, result.output
        assert out.exists()
        from engram.kb.contracts import KnowledgeBaseSchema

        spec = KnowledgeBaseSchema.from_path(out)
        assert spec.slug == "my-knowledge-base"
        assert len(spec.page_types) == 1
        assert spec.page_types[0].page_type == "concept"
        assert spec.claims is None

    def test_spec_interactive_claims_enabled_writes_valid_spec(self, tmp_path: Path) -> None:
        """Claims-enabled path: claim-grammar section prompted; spec passes IFF invariant."""
        out = tmp_path / "knowledge-base-schema.json"
        # Page type with claims + ClaimGrammarSpec section
        inputs = (
            "papers\n"  # slug
            "A claims knowledge base\n"  # purpose
            "paper\n"  # page type kind
            "paper\n"  # id_prefix
            "findings,background\n"  # sections CSV
            "y\n"  # claims enabled?
            "n\n"  # add another page type?
            "n\n"  # add relation?
            "n\n"  # add entity type?
            "n\n"  # add claim kind?
            "n\n"  # add facet?
            "findings\n"  # claim section heading slug
            ""  # done
        )

        result = runner.invoke(app, ["init", "spec", "--interactive", "--out", str(out)], input=inputs)

        assert result.exit_code == 0, result.output
        assert out.exists()
        from engram.kb.contracts import KnowledgeBaseSchema

        spec = KnowledgeBaseSchema.from_path(out)
        assert spec.slug == "papers"
        # The IFF invariant: claims_enabled → claims must not be None
        assert any(pt.claims_enabled for pt in spec.page_types)
        assert spec.claims is not None
        assert spec.claims.section == "findings"


# ---------------------------------------------------------------------------
# 4.  load_vocab extraction parity
# ---------------------------------------------------------------------------


class TestLoadVocabExtraction:
    """Verify the extracted :func:`load_vocab` produces the same rows as ``vocab_rows``."""

    async def test_load_vocab_upserts_all_registry_rows(self) -> None:
        """`load_vocab` calls `uow.vocab.upsert_many` with exactly `vocab_rows(vocabulary)`."""
        from engram.init.contracts import scaffold
        from engram.kb.contracts import parse_vocabulary
        from engram.testing.specs import research_spec

        spec = research_spec()
        files = scaffold(spec)
        vocabulary_docs = [
            ("entity-types.md", files["_meta/vocabulary/entity-types.md"]),
            ("relation-types.md", files["_meta/vocabulary/relation-types.md"]),
            ("facets.md", files["_meta/vocabulary/facets.md"]),
        ]
        vocabulary = parse_vocabulary(vocabulary_docs).index

        expected_rows = list(vocab_rows(vocabulary, collection="research"))

        uow = FakeKnowledgeBaseUnitOfWork()

        def factory():
            return uow

        count = await load_vocab(factory, vocabulary, collection="research")

        assert count == len(expected_rows)
        assert uow.vocab.upserted_rows == expected_rows
        assert uow.committed is True

    async def test_load_vocab_commits(self) -> None:
        """`load_vocab` commits the unit of work on success."""
        from engram.init.contracts import scaffold
        from engram.kb.contracts import parse_vocabulary
        from engram.testing.specs import research_spec

        spec = research_spec()
        files = scaffold(spec)
        vocabulary_docs = [("entity-types.md", files["_meta/vocabulary/entity-types.md"])]
        vocabulary = parse_vocabulary(vocabulary_docs).index

        uow = FakeKnowledgeBaseUnitOfWork()

        await load_vocab(lambda: uow, vocabulary, collection="research")

        assert uow.committed is True

    async def test_load_vocab_returns_row_count(self) -> None:
        """`load_vocab` returns the number of rows upserted."""
        from engram.init.contracts import scaffold
        from engram.kb.contracts import parse_vocabulary
        from engram.testing.specs import research_spec

        spec = research_spec()
        files = scaffold(spec)
        vocabulary_docs = [
            ("entity-types.md", files["_meta/vocabulary/entity-types.md"]),
            ("relation-types.md", files["_meta/vocabulary/relation-types.md"]),
            ("facets.md", files["_meta/vocabulary/facets.md"]),
        ]
        vocabulary = parse_vocabulary(vocabulary_docs).index
        expected = len(list(vocab_rows(vocabulary, collection="research")))

        uow = FakeKnowledgeBaseUnitOfWork()
        count = await load_vocab(lambda: uow, vocabulary, collection="research")

        assert count == expected
        assert count > 0

    async def test_empty_registry_upserts_zero_rows(self) -> None:
        """An empty vocabulary yields zero vocab rows."""
        from engram.kb.contracts import parse_vocabulary

        vocabulary = parse_vocabulary([]).index
        uow = FakeKnowledgeBaseUnitOfWork()

        count = await load_vocab(lambda: uow, vocabulary, collection="research")

        assert count == 0
        # upsert_many was called once with an empty list
        assert uow.vocab.upserts == [[]]


# ---------------------------------------------------------------------------
# 5.  init all — index targets the --knowledge-base path, not the live configured knowledge base
# ---------------------------------------------------------------------------


class _EnvCapturingContainer:
    """Fake async DI container that captures the env at the moment it's built.

    ``get(MessageBus)`` returns a bus backed by a :class:`RecordingCommandHandler`
    that returns a clean `IndexReport`; the env snapshot is stored on the
    factory instance so the test can assert which knowledge base+DB were pinned before
    `_build_container` was called.
    """

    def __init__(self, handler: RecordingCommandHandler) -> None:
        self._bus = MessageBus({IndexKnowledgeBase: handler}, {})

    async def get(self, key: type) -> object:
        if key is MessageBus:
            return self._bus
        msg = f"_EnvCapturingContainer has no registration for {key!r}"
        raise KeyError(msg)


class _EnvCapturingFactory:
    """Zero-arg `ctx.obj` factory that snapshots the env when it is first called.

    `settings_at_call` is filled separately, by a spy on `_build_container` wired in the test
    that calls this same factory (see `TestInitAllKnowledgeBaseTarget`): the settings that reached the
    container-building seam, since the zero-arg `ctx.obj()` call itself never receives them.
    """

    def __init__(self) -> None:
        self.env_at_call: dict[str, str] = {}
        self.settings_at_call: ActiveKnowledgeBase | None = None
        self.handler = RecordingCommandHandler(result=IndexReport(pages=0, sections=0, claims=0, edges=0, pruned=0, embedded=0, reused=0))

    def __call__(self) -> _EnvCapturingContainer:
        self.env_at_call = dict(os.environ)
        return _EnvCapturingContainer(self.handler)


class TestInitAllKnowledgeBaseTarget:
    """`init all --knowledge-base X` must index X, not the live configured knowledge_base."""

    def test_init_all_indexes_the_flag_knowledge_base_not_a_configured_one(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """The index stage uses the --knowledge-base path, not whatever application_settings.json says.

        Patches the DB-touching stages (upgrade_schema, load_vocabulary_sync) so the unit test
        runs without a live DB, then passes an env-capturing factory as ctx.obj. A spy on
        _build_container records the settings it is handed at the moment Stage 3 builds the
        container, onto that same factory.
        """
        spec_path = tmp_path / "knowledge-base-schema.json"
        spec_path.write_text(_minimal_spec_json("new-knowledge-base"), encoding="utf-8")
        knowledge_base = tmp_path / "my-knowledge-base"
        knowledge_base.mkdir()

        # Patch DB-touching functions so no live DB is needed (the init group lives in
        # `studio.knowledge_base.cli_init`, so patch the names where `init all` looks them up — in that module's namespace).
        monkeypatch.setattr("studio.knowledge_base.cli_init.upgrade_schema", lambda _url: None)
        monkeypatch.setattr("studio.knowledge_base.cli_init.load_vocabulary_sync", lambda _url, _reg, _collection: 0)

        monkeypatch.setenv("APP_INFRASTRUCTURE__DATABASE__URL", "postgresql+asyncpg://dummy/test")
        monkeypatch.setenv("APP_INFRASTRUCTURE__DATABASE__READER_URL", "postgresql+asyncpg://dummy/test")

        factory = _EnvCapturingFactory()
        real_build_container = cli_init_module._build_container

        def _spy_build_container(
            ctx: typer.Context, settings: ActiveKnowledgeBase | None = None, knowledge_base_origin: KnowledgeBasePathOrigin | None = None
        ) -> Resolver:
            factory.settings_at_call = settings
            return real_build_container(ctx, settings, knowledge_base_origin)

        monkeypatch.setattr(cli_init_module, "_build_container", _spy_build_container)

        runner.invoke(
            app,
            ["init", "all", "--spec", str(spec_path), "--knowledge-base", str(knowledge_base), "--db-url", "postgresql+asyncpg://dummy/test"],
            obj=factory,
        )

        assert factory.settings_at_call is not None, "factory was never called — _build_container was not reached"
        assert factory.settings_at_call.knowledge_base.path == knowledge_base.resolve(), (
            f"the index stage's settings named {factory.settings_at_call.knowledge_base.path!r}, expected the tmp knowledge base {knowledge_base.resolve()!r}."
        )
        assert factory.env_at_call.get("APP_INFRASTRUCTURE__DATABASE__URL") == "postgresql+asyncpg://dummy/test"
