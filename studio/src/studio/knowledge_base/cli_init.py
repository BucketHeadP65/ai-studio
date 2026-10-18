"""The `studio init` command group: start a new knowledge base from its schema.

`scaffold` writes the `_meta/` files from the schema with no database, and `db` migrates the database schema to
head and seeds the vocabulary. `all` runs both, then indexes the knowledge base once. `spec` builds a schema from
interactive prompts, with no model involved. A bare `studio init --template <slug>` copies a template's schema and
guidance pages into a fresh knowledge base, then scaffolds it as `init scaffold` does. The entry module adds
`init_app` to the root `app`, so each command runs as `studio init <name>`.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil

from pathlib import Path
from typing import TYPE_CHECKING

import typer

from engram.indexing.contracts import IndexKnowledgeBase
from engram.init.contracts import load_template, load_vocabulary_sync, resolve_db_url, scaffold, upgrade_schema, write_scaffold
from engram.kb.contracts import CanonicalSection, ClaimGrammarSpec, FacetSpec, KnowledgeBaseSchema, PageTypeSpec, RelationDecl, VocabEntry
from pydantic import JsonValue, TypeAdapter, ValidationError

from studio.knowledge_base.cli_support import (
    ActiveKnowledgeBase,
    _build_container,
    _dispatch_index,
    _fail,
    _load_vocabulary,
    _print_index_report,
    announce_identity,
    announce_knowledge_base,
    resolve_partition,
)
from studio.knowledge_base.settings import KnowledgeBaseSettings, fetch_application_settings
from studio.knowledge_base.template_guidance import copy_template_guidance

if TYPE_CHECKING:
    from core.resolver import Resolver
    from engram.indexing.contracts import IndexReport
    from engram.init.contracts import InitReport, Template


init_app = typer.Typer(
    name="init", help="Initialise a new knowledge base: scaffold _meta/ files, migrate the schema, seed vocab.", no_args_is_help=True
)


def _load_schema(spec_override: str | None, knowledge_base_path: Path) -> KnowledgeBaseSchema:
    """Load the schema at `spec_override`, or the knowledge base's own `knowledge-base-schema.json` when it is None."""
    if spec_override:
        schema_path = Path(spec_override)
    else:
        schema_path = knowledge_base_path / "knowledge-base-schema.json"
        if not schema_path.is_file():
            _fail(
                f"No spec found: pass --spec PATH or put knowledge-base-schema.json in {knowledge_base_path} "
                "(create one with 'studio init spec --interactive')."
            )
    try:
        return KnowledgeBaseSchema.from_path(schema_path)
    except (OSError, ValueError) as exc:
        _fail(f"Could not load spec from {schema_path}: {exc}")


@init_app.command(name="scaffold")
def init_scaffold_cmd(
    spec: str | None = typer.Option(default=None, help="Path to knowledge-base-schema.json. Defaults to <knowledge base>/knowledge-base-schema.json."),
    knowledge_base: str = typer.Option(default=".", help="Knowledge base root directory to scaffold into."),
    *,
    force: bool = typer.Option(default=False, help="Overwrite existing prose/template files."),
) -> None:
    """Write the knowledge base's `_meta/` files from its schema. Needs no database.

    Reads the schema from `--spec`, or from the knowledge base's `knowledge-base-schema.json` when `--spec` is
    not given. Running it again is safe. Vocabulary files are merged with the ones on disk, and their existing
    definitions win. Other files that already exist are skipped unless `--force` is given.
    """
    knowledge_base_path = Path(knowledge_base)
    resolved_schema = _load_schema(spec, knowledge_base_path)
    files = scaffold(resolved_schema)
    report = write_scaffold(knowledge_base_path, files, force=force)
    announce_knowledge_base(knowledge_base_path)
    typer.echo("Scaffold complete:")
    typer.echo(report.summary())


_SCHEMA_DOCUMENT = TypeAdapter(dict[str, JsonValue])


def _renamed_schema(schema_text: str, slug: str) -> str:
    """The template's schema text with `slug` as its slug, and as its collection when the collection followed the template's slug."""
    document = _SCHEMA_DOCUMENT.validate_json(schema_text)
    template_slug = document.get("slug")
    document["slug"] = slug
    retrieval = document.get("retrieval")
    if isinstance(retrieval, dict) and retrieval.get("collection") == template_slug:
        retrieval["collection"] = slug
    try:
        KnowledgeBaseSchema.model_validate(document)
    except ValidationError as exc:
        _fail(f"--slug {slug!r} is refused: {exc}")
    return json.dumps(document, indent=2, ensure_ascii=False) + "\n"


def _copy_template_files(template: Template, knowledge_base_path: Path, *, slug: str | None) -> None:
    """Copy the template's schema and `_meta/*.md` guidance pages into the knowledge base, refusing one that has a schema."""
    target_schema_path = knowledge_base_path / "knowledge-base-schema.json"
    if target_schema_path.exists():
        _fail(f"{target_schema_path} already exists. --template refuses to overwrite an existing knowledge-base-schema.json.")
    template_schema_path = template.path / "knowledge-base-schema.json"
    renamed = _renamed_schema(template_schema_path.read_text(encoding="utf-8"), slug) if slug is not None else None
    knowledge_base_path.mkdir(parents=True, exist_ok=True)
    if renamed is None:
        shutil.copy2(template_schema_path, target_schema_path)
    else:
        target_schema_path.write_text(renamed, encoding="utf-8")
    copy_template_guidance(template, knowledge_base_path)


def _init_from_template(template_slug: str, knowledge_base_path: Path, *, slug: str | None) -> None:
    """Bootstrap `knowledge_base_path` from the template named `template_slug`: copy its files, then scaffold."""
    try:
        resolved_template = load_template(template_slug)
    except LookupError as exc:
        _fail(str(exc))
    _copy_template_files(resolved_template, knowledge_base_path, slug=slug)
    resolved_schema = _load_schema(None, knowledge_base_path)
    files = scaffold(resolved_schema)
    report = write_scaffold(knowledge_base_path, files)
    announce_knowledge_base(knowledge_base_path)
    typer.echo(f"Initialised from template {template_slug!r}:")
    typer.echo(report.summary())


@init_app.callback(invoke_without_command=True)
def init_callback(
    ctx: typer.Context,
    template: str | None = typer.Option(
        default=None, help="Template slug to bootstrap a fresh knowledge base from (see 'templates/' or ENGRAM_TEMPLATES)."
    ),
    knowledge_base: str = typer.Option(default=".", help="Knowledge base root directory to initialise from the template."),
    slug: str | None = typer.Option(
        default=None, help="The new knowledge base's slug, which also names its collection. Defaults to the template's own slug."
    ),
) -> None:
    """`studio init --template <slug>` starts a fresh knowledge base from a named template.

    Copies the template's `knowledge-base-schema.json` and `_meta/*.md` guidance pages into `--knowledge-base`,
    then scaffolds from the copied schema as `init scaffold` does. `--slug` renames the copy, so two knowledge bases
    from one template index into separate collections. Every subcommand (`scaffold`, `db`, `all`, `spec`) runs as
    usual, and `--template` acts only when no subcommand is given.
    """
    if ctx.invoked_subcommand is not None or template is None:
        return
    _init_from_template(template, Path(knowledge_base), slug=slug)


@init_app.command(name="db")
def init_db_cmd(
    knowledge_base: str = typer.Option(default=".", help="Knowledge base root (used to locate _meta/vocabulary/ for vocab seed)."),
    db_url: str | None = typer.Option(default=None, help="Database URL (asyncpg). Falls back to env/JSON config."),
) -> None:
    """Migrate the database schema to head and seed the vocabulary from the knowledge base.

    Runs `alembic upgrade head`, then upserts the vocabulary rows from `<knowledge_base>/_meta/vocabulary/*.md`,
    stamped with the `retrieval.collection` value of the knowledge base's schema.
    The knowledge base must already have its `_meta/vocabulary/` folder and its schema on disk. Run `init scaffold`
    first, or use `init all` to run both. That value comes from the schema, so this command takes no
    `--collection` option (`init all` does).

    `--db-url` wins over the `APP_INFRASTRUCTURE__DATABASE__URL` environment variable, which wins over the
    `infrastructure.database.url` application setting.
    """
    announce_knowledge_base(Path(knowledge_base).resolve())
    try:
        resolved_url = resolve_db_url(db_url)
    except ValueError as exc:
        _fail(str(exc))

    typer.echo(f"Migrating schema at {resolved_url} …")
    upgrade_schema(resolved_url)
    typer.echo("Schema up to date.")

    knowledge_base_path = Path(knowledge_base)
    vocabulary = _load_vocabulary(knowledge_base_path)
    collection = _load_schema(None, knowledge_base_path).retrieval.collection
    typer.echo("Seeding vocab …")
    count = load_vocabulary_sync(resolved_url, vocabulary, collection)
    typer.echo(f"Vocab: {count} row(s) upserted.")


async def _index_smoke_after_identity(
    container: Resolver, settings: ActiveKnowledgeBase, command: IndexKnowledgeBase, *, collection: str
) -> IndexReport:
    """Announce the identity, then dispatch the first index, one event loop for both."""
    await announce_identity(container, settings, collection=collection)
    return await _dispatch_index(container, command)


@init_app.command(name="all")
def init_all_cmd(
    ctx: typer.Context,
    spec: str | None = typer.Option(default=None, help="Path to knowledge-base-schema.json. Defaults to <knowledge base>/knowledge-base-schema.json."),
    knowledge_base: str = typer.Option(default=".", help="Knowledge base root directory to initialise."),
    collection: str | None = typer.Option(default=None, help="Collection to index into. Defaults to the one this knowledge base's spec declares."),
    db_url: str | None = typer.Option(default=None, help="Database URL (asyncpg). Falls back to env/JSON config."),
    *,
    embed: bool = typer.Option(default=False, help="Embed during the index smoke. Off by default to keep the bootstrap fast."),
) -> None:
    """Set up a knowledge base in one command: scaffold, migrate the database, seed the vocabulary and index.

    Runs `init scaffold`, then `init db`, then one index of the knowledge base. If scaffold or db fails, the
    command stops and says which stage failed. Embedding is off unless `--embed` is given, so the first index
    stays fast on a fresh knowledge base.
    """
    knowledge_base_path = Path(knowledge_base)

    # Stage 1: scaffold
    resolved_schema = _load_schema(spec, knowledge_base_path)
    partition = resolve_partition(collection, resolved_schema)
    files = scaffold(resolved_schema)
    report: InitReport = write_scaffold(knowledge_base_path, files)
    typer.echo("Scaffold complete:")
    typer.echo(report.summary())

    # Stage 2: db (schema + vocab)
    try:
        resolved_url = resolve_db_url(db_url)
    except ValueError as exc:
        _fail(f"Stage 2 (db) failed: {exc}")

    typer.echo(f"Migrating schema at {resolved_url} …")
    upgrade_schema(resolved_url)
    typer.echo("Schema up to date.")

    vocabulary = _load_vocabulary(knowledge_base_path)
    typer.echo("Seeding vocab …")
    vocab_count = load_vocabulary_sync(resolved_url, vocabulary, partition)
    typer.echo(f"Vocab: {vocab_count} row(s) upserted.")

    # Stage 3: index. The settings built below read the database just migrated, not the one the
    # application settings name.
    os.environ["APP_INFRASTRUCTURE__DATABASE__URL"] = resolved_url
    os.environ["APP_INFRASTRUCTURE__DATABASE__READER_URL"] = resolved_url
    # The knowledge base settings require Units/, which a new knowledge base may lack. An empty
    # Units/ indexes to 0 pages.
    (knowledge_base_path / "Units").mkdir(parents=True, exist_ok=True)
    typer.echo(f"Running index smoke ({'--embed' if embed else '--no-embed'}) …")
    smoke_settings = ActiveKnowledgeBase(
        application=fetch_application_settings(), knowledge_base=KnowledgeBaseSettings(path=knowledge_base_path.resolve())
    )
    container = _build_container(ctx, smoke_settings)
    index_report = asyncio.run(
        _index_smoke_after_identity(
            container, smoke_settings, IndexKnowledgeBase(collection=partition, embed=embed, collection_named=collection is not None), collection=partition
        )
    )
    _print_index_report(index_report)
    if index_report.validation.n_errors > 0:
        raise typer.Exit(code=1)


def _prompt_page_types() -> tuple[tuple[PageTypeSpec, ...], bool]:
    """Prompt for one or more page types, returning them and whether any of them enables claims."""
    page_types: list[PageTypeSpec] = []
    any_claims = False
    while True:
        page_type = typer.prompt("Page type kind (e.g. recipe, note, clause)")
        id_prefix = typer.prompt(f"  id_prefix for {page_type!r}")
        sections_csv = typer.prompt(f"  Canonical sections CSV for {page_type!r} (e.g. overview,background)", default="")
        canonical = tuple(CanonicalSection(slug=s.strip()) for s in sections_csv.split(",") if s.strip())
        claims_enabled = typer.confirm(f"  Enable claims on {page_type!r}?", default=False)
        if claims_enabled:
            any_claims = True
        page_types.append(PageTypeSpec(page_type=page_type, id_prefix=id_prefix, canonical_sections=canonical, claims_enabled=claims_enabled))
        if not typer.confirm("Add another page type?", default=False):
            break
    return tuple(page_types), any_claims


def _prompt_relations() -> tuple[RelationDecl, ...]:
    """Prompt for zero or more typed relations."""
    relations: list[RelationDecl] = []
    while typer.confirm("Add a relation?", default=False):
        r_slug = typer.prompt("  Relation slug (e.g. is_a)")
        r_domain = typer.prompt("  Domain (page type kind)")
        r_range = typer.prompt("  Range (page type kind)")
        r_desc = typer.prompt("  Description", default="")
        relations.append(RelationDecl(slug=r_slug, domain=r_domain, range=r_range, description=r_desc))
    return tuple(relations)


def _prompt_vocab_entries(noun: str) -> tuple[VocabEntry, ...]:
    """Prompt for zero or more vocabulary entries, calling each one `noun` in the prompts."""
    entries: list[VocabEntry] = []
    while typer.confirm(f"Add a {noun}?", default=False):
        value = typer.prompt(f"  {noun.capitalize()} value")
        definition = typer.prompt("  Definition")
        entries.append(VocabEntry(value=value, definition=definition))
    return tuple(entries)


def _prompt_facets() -> tuple[FacetSpec, ...]:
    """Prompt for zero or more facets."""
    facets: list[FacetSpec] = []
    while typer.confirm("Add a facet?", default=False):
        f_key = typer.prompt("  Facet key (e.g. cuisine)")
        f_seeds_csv = typer.prompt(f"  Seed values CSV for {f_key!r}", default="")
        f_seeds: tuple[str, ...] = tuple(v.strip() for v in f_seeds_csv.split(",") if v.strip())
        facets.append(FacetSpec(key=f_key, seed=f_seeds))
    return tuple(facets)


@init_app.command(name="spec")
def init_spec_cmd(
    *,
    interactive: bool = typer.Option(default=False, help="Prompt for spec details interactively."),
    out: str = typer.Option(default="knowledge-base-schema.json", help="Output path for the generated spec."),
) -> None:
    """Build a knowledge base schema by answering prompts, with no model involved.

    Prompts for the knowledge base slug, its purpose, its page types (name, id prefix, sections, claims),
    relations, entity types, claim types and facets. When any page type enables claims, it also prompts for
    the claim grammar section. The schema it writes passes every schema check, including that a schema with
    claims on any page type declares its claim grammar.
    """
    if not interactive:
        _fail("Pass --interactive to generate a spec interactively (or use 'studio init spec --help').")

    slug = typer.prompt("Knowledge base slug (e.g. research, legal, medical)")
    purpose = typer.prompt("Short purpose statement")

    page_types, any_claims = _prompt_page_types()
    relations = _prompt_relations()
    entity_types = _prompt_vocab_entries("entity type")
    claim_types = _prompt_vocab_entries("claim kind")
    facets = _prompt_facets()

    # A schema with claims enabled on any page type must declare its claim grammar.
    claims_grammar: ClaimGrammarSpec | None = None
    if any_claims:
        section = typer.prompt("Claim grammar section heading slug (e.g. findings)", default="findings")
        claims_grammar = ClaimGrammarSpec(section=section)

    try:
        schema = KnowledgeBaseSchema(
            slug=slug,
            purpose=purpose,
            page_types=page_types,
            relations=relations,
            entity_types=entity_types,
            claim_types=claim_types,
            facets=facets,
            claims=claims_grammar,
        )
    except ValueError as exc:
        _fail(f"Spec validation failed: {exc}")

    out_path = Path(out)
    out_path.write_text(schema.model_dump_json(indent=2) + "\n", encoding="utf-8")
    announce_knowledge_base(out_path.parent.resolve())
    typer.echo(f"Wrote spec to {out_path}")
