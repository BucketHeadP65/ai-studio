"""The `studio note` command group: build and check notes against the knowledge base's note grammar.

`validate` checks one note. `build` and `build-batch` render authoring payloads into notes and validate them
before writing. `verify` checks each claim's quote against its source mirror, and `normalize` merges frontmatter
into an existing note in place. `schema` prints a page type's authoring JSON schema. The entry module adds
`note_app` to the root `app`, so each command runs as `studio note <name>`.
"""

from __future__ import annotations

import json

from pathlib import Path
from typing import TYPE_CHECKING

import typer

from core.files import atomic_write
from core.markdown import render_frontmatter, split_frontmatter
from engram.indexing.contracts import MirrorDoc, validate_note
from engram.kb import contracts as kb

from studio.knowledge_base.cli_support import (
    _fail,
    _load_vocabulary,
    _resolve_knowledge_base_for_authoring,
    _resolve_knowledge_base_path,
    announce_knowledge_base,
    knowledge_base_path_origin,
    load_knowledge_base_schema_or_fail,
)

if TYPE_CHECKING:
    from collections.abc import Iterable

    from engram.indexing.contracts import ValidationReport
    from engram.kb.contracts import KnowledgeBaseSchema
    from pydantic import JsonValue

    from studio.knowledge_base.settings import KnowledgeBasePathSource

note_app = typer.Typer(name="note", help="Authoring toolset: construct and validate memory notes from the grammar.", no_args_is_help=True)


def _path_root(knowledge_base_path: Path, schema: KnowledgeBaseSchema) -> Path:
    """The folder `path_fields` values resolve against: the knowledge base itself or its parent, as the schema's `path_root` says."""
    return knowledge_base_path if schema.path_root == "knowledge_base" else knowledge_base_path.parent


def _find_mirror_text(knowledge_base_path: Path, key_value: str, sources_subdir: str = "Sources") -> str | None:
    """The text of the first mirror under `sources_subdir` whose source key is `key_value`, or None when no mirror has it."""
    sources = knowledge_base_path / sources_subdir
    if not sources.is_dir():
        return None
    for candidate in sorted(sources.rglob("*.md")):
        text = candidate.read_text(encoding="utf-8")
        if MirrorDoc.from_text(candidate.relative_to(knowledge_base_path).as_posix(), text, "").source_key == key_value:
            return text
    return None


def _quote_failures(note: kb.KnowledgeBaseNote, knowledge_base_path: Path, schema: KnowledgeBaseSchema, sources_subdir: str = "Sources") -> list[str]:
    """The quote check failures of a note: one per quote its mirror lacks, or one naming the mirror that cannot be found."""
    quoted = [claim for claim in note.claims if claim.quote]
    if not quoted:
        return []
    key_field = schema.sources.key_field
    key_value = getattr(note.frontmatter, key_field, None) if key_field else None
    if not isinstance(key_value, str) or not key_value:
        return [f"note {note.frontmatter.id} has quoted claims but no {key_field or 'sources.key_field'} to resolve a mirror"]
    mirror_text = _find_mirror_text(knowledge_base_path, key_value, sources_subdir)
    if mirror_text is None:
        return [f"no {sources_subdir}/ mirror found for {key_field} {key_value!r}"]
    return [
        f"claim {claim.anchor}: quote not found in mirror"
        for claim in quoted
        if claim.quote is not None and not kb.quotes_in_mirror(claim.quote, mirror_text)
    ]


def _report(title: str, problems: Iterable[str]) -> None:
    """Echo a readable per-problem report under `title` (clean when there are none)."""
    items = list(problems)
    if not items:
        typer.echo(f"{title}: OK")
        return
    typer.echo(f"{title}: {len(items)} problem(s)")
    for item in items:
        typer.echo(f"  - {item}")


def _print_validation_report(title: str, report: ValidationReport) -> None:
    """Echo a note validation report, errors then warnings, one `rule: message` line each."""
    if report.n_errors == 0:
        typer.echo(f"{title}: OK ({report.n_warnings} warning(s))" if report.n_warnings else f"{title}: OK")
    else:
        typer.echo(f"{title}: {report.n_errors} error(s), {report.n_warnings} warning(s)")
    for issue in (*report.errors, *report.warnings):
        anchor = f" {issue.anchor}" if issue.anchor else ""
        typer.echo(f"  {issue.severity.upper()} {issue.page_id}{anchor} — {issue.rule}: {issue.message}")


@note_app.command(name="validate")
def note_validate_cmd(
    path: str = typer.Argument(..., help="Path to the note to validate."),
    knowledge_base: str | None = typer.Option(
        default=None, help="Knowledge base root for vocabulary resolution, defaults to the working directory when it holds a knowledge-base-schema.json."
    ),
) -> None:
    """Validate one note with the same checks `studio index` runs on it, and exit 1 on any error.

    The checks are summary length, qualifiers on comparative claims, the quotes each claim type requires, unique
    anchors and vocabulary. Links to other notes are not checked here. One notice gives their count instead.
    Warnings and that notice are printed and never fail the command.
    """
    knowledge_base_path = _resolve_knowledge_base_path(knowledge_base)
    announce_knowledge_base(knowledge_base_path)
    vocabulary = _load_vocabulary(knowledge_base_path)
    schema = load_knowledge_base_schema_or_fail(knowledge_base_path)

    note_path = Path(path)
    try:
        note = kb.parse(note_path.read_text(encoding="utf-8"), source_path=note_path.name, spec=schema)
    except kb.NoteGrammarError as exc:
        _fail(f"parse error: {exc}")

    report = validate_note(
        note, vocabulary, spec=schema, path_root=_path_root(knowledge_base_path, schema), path_root_source=knowledge_base_path_origin(knowledge_base)
    )
    _print_validation_report(f"validate {note_path.name}", report)
    if report.n_errors > 0:
        raise typer.Exit(code=1)


def _render_and_validate(
    payload_data: object,
    out_name: str,
    vocabulary: kb.VocabularyIndex,
    schema: KnowledgeBaseSchema,
    path_root: Path,
    path_root_source: KnowledgeBasePathSource,
) -> tuple[str, kb.KnowledgeBaseNote, ValidationReport]:
    """Render one payload into note text, parse it back and validate it, raising `ValueError` or `NoteGrammarError` when it is bad."""
    payload_obj = {**payload_data, "knowledge_base_schema": schema} if isinstance(payload_data, dict) else payload_data
    built = kb.AuthoringPayload.model_validate(payload_obj)
    rendered = kb.render(built, spec=schema)
    note = kb.parse(rendered, source_path=out_name, spec=schema)
    return rendered, note, validate_note(note, vocabulary, spec=schema, path_root=path_root, path_root_source=path_root_source)


@note_app.command(name="build")
def note_build_cmd(
    payload: str = typer.Option(..., help="Path to the authoring-payload JSON."),
    out: str = typer.Option(..., help="Destination path for the rendered note."),
    knowledge_base: str | None = typer.Option(
        default=None, help="Knowledge base root for spec + vocabulary resolution, defaults to the working directory when it holds a knowledge-base-schema.json."
    ),
    *,
    verify_quotes: bool = typer.Option(default=False, help="Verify each quote against its source mirror."),
    force: bool = typer.Option(default=False, help="Overwrite an existing file."),
) -> None:
    """Render an authoring payload into a note, validate it, and write it only when it passes.

    The payload's frontmatter is validated against the knowledge base's schema, so the payload never carries the
    schema. The rendered note goes through the same checks as `note validate`, so a note `studio index` would reject
    is refused here. Any error exits 1 and writes nothing. A warning, such as an oversized summary, is printed and
    the note is still written. `--verify-quotes` also checks each quote against its mirror under `Sources/` before
    the write. The schema and vocabulary come from `--knowledge-base`, or from the knowledge base the working
    directory sits in.
    """
    out_path = Path(out)
    if out_path.exists() and not force:
        _fail(f"refusing to overwrite {out_path} (pass --force to replace it)")

    # Everything up to the write can fail, and a failure leaves no file behind.
    knowledge_base_path, sources_subdir = _resolve_knowledge_base_for_authoring(knowledge_base)
    announce_knowledge_base(knowledge_base_path)
    vocabulary = _load_vocabulary(knowledge_base_path)
    schema = load_knowledge_base_schema_or_fail(knowledge_base_path)

    try:
        data = json.loads(Path(payload).read_text(encoding="utf-8"))
        rendered, note, report = _render_and_validate(
            data, out_path.name, vocabulary, schema, _path_root(knowledge_base_path, schema), knowledge_base_path_origin(knowledge_base)
        )
    except (OSError, ValueError) as exc:
        _fail(f"invalid payload: {exc}")

    if report.n_errors > 0 or report.n_warnings > 0:
        _print_validation_report("build validation", report)
    if report.n_errors > 0:
        raise typer.Exit(code=1)

    if verify_quotes:
        failures = _quote_failures(note, knowledge_base_path, schema, sources_subdir)
        if failures:
            _report("quote verification", failures)
            raise typer.Exit(code=1)

    atomic_write(out_path, rendered)
    typer.echo(f"wrote {out_path}")


def _build_batch_entry(
    entry: object,
    index: int,
    vocabulary: kb.VocabularyIndex,
    schema: KnowledgeBaseSchema,
    path_root: Path,
    path_root_source: KnowledgeBasePathSource,
    *,
    force: bool,
) -> tuple[tuple[Path, str] | None, tuple[str, str] | None]:
    """Build one manifest entry into `(ready, failure)`, exactly one set: `(out_path, text)` or `(label, reason)`."""
    if not isinstance(entry, dict):
        return None, (f"entry {index}", "each entry must be an object with a string 'out' and a 'payload'")
    out_value = entry.get("out")
    if not isinstance(out_value, str) or "payload" not in entry:
        return None, (f"entry {index}", "each entry must be an object with a string 'out' and a 'payload'")
    out_path = Path(out_value)
    if out_path.exists() and not force:
        return None, (out_value, "refusing to overwrite (pass --force to replace it)")
    try:
        rendered, _note, report = _render_and_validate(entry["payload"], out_path.name, vocabulary, schema, path_root, path_root_source)
    except (ValueError, kb.NoteGrammarError) as exc:
        return None, (out_value, f"invalid payload: {exc}")
    if report.n_errors > 0:
        return None, (out_value, ", ".join(issue.message for issue in report.errors))
    return (out_path, rendered), None


@note_app.command(name="build-batch")
def note_build_batch_cmd(
    manifest: str = typer.Option(..., help="Path to a JSON array of {'out': path, 'payload': {...}} entries."),
    knowledge_base: str | None = typer.Option(
        default=None, help="Knowledge base root for spec + vocabulary resolution, defaults to the working directory when it holds a knowledge-base-schema.json."
    ),
    *,
    force: bool = typer.Option(default=False, help="Overwrite existing notes."),
) -> None:
    """Build many authoring payloads in one pass.

    Reads a manifest, a JSON array of `{"out": <path>, "payload": {...}}` entries, and validates each entry as
    `note build` does. Every entry is validated before any write. The entries that pass are written, and each one
    that fails writes nothing and is reported. Exits 1 when any entry failed. Nothing is indexed, so run
    `studio index` once afterwards.
    """
    knowledge_base_path, _ = _resolve_knowledge_base_for_authoring(knowledge_base)
    announce_knowledge_base(knowledge_base_path)
    vocabulary = _load_vocabulary(knowledge_base_path)
    schema = load_knowledge_base_schema_or_fail(knowledge_base_path)

    try:
        entries = json.loads(Path(manifest).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        _fail(f"could not read manifest: {exc}")
    if not isinstance(entries, list):
        _fail("manifest must be a JSON array of {'out', 'payload'} entries")

    # Validate every entry before writing any.
    path_root = _path_root(knowledge_base_path, schema)
    path_root_source = knowledge_base_path_origin(knowledge_base)
    ready: list[tuple[Path, str]] = []
    failures: list[tuple[str, str]] = []
    for index, entry in enumerate(entries):
        ok, fail = _build_batch_entry(entry, index, vocabulary, schema, path_root, path_root_source, force=force)
        if ok is not None:
            ready.append(ok)
        if fail is not None:
            failures.append(fail)

    # Write the entries that passed.
    for out_path, rendered in ready:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write(out_path, rendered)
        typer.echo(f"wrote {out_path}")

    typer.echo(f"built {len(ready)}/{len(entries)} note(s), {len(failures)} failed")
    for label, reason in failures:
        typer.echo(f"  FAILED {label}: {reason}")
    if failures:
        raise typer.Exit(code=1)


@note_app.command(name="verify")
def note_verify_cmd(
    path: str = typer.Argument(..., help="Path to the note to verify."),
    knowledge_base: str | None = typer.Option(
        default=None, help="Knowledge base root for spec + mirror resolution, defaults to the working directory when it holds a knowledge-base-schema.json."
    ),
) -> None:
    """Check each claim's quote against the note's source mirror, and exit 1 on any failure.

    The schema and mirrors come from `--knowledge-base`, or from the knowledge base the working directory sits in.
    """
    knowledge_base_path, sources_subdir = _resolve_knowledge_base_for_authoring(knowledge_base)
    announce_knowledge_base(knowledge_base_path)
    schema = load_knowledge_base_schema_or_fail(knowledge_base_path)
    note_path = Path(path)
    try:
        note = kb.parse(note_path.read_text(encoding="utf-8"), source_path=note_path.name, spec=schema)
    except kb.NoteGrammarError as exc:
        _fail(f"parse error: {exc}")

    failures = _quote_failures(note, knowledge_base_path, schema, sources_subdir)
    _report(f"verify {note_path.name}", failures)
    if failures:
        raise typer.Exit(code=1)


@note_app.command(name="normalize")
def note_normalize_cmd(
    path: str = typer.Argument(..., help="Path to the note to normalize (rewritten in place)."),
    frontmatter: str = typer.Option(..., help="Path to a JSON object of frontmatter keys to merge in (id/type/status/facets/…)."),
    knowledge_base: str | None = typer.Option(
        default=None, help="Knowledge base root for spec + vocabulary resolution, defaults to the working directory when it holds a knowledge-base-schema.json."
    ),
) -> None:
    """Merge frontmatter into an existing note in place, keeping its body byte for byte.

    The `--frontmatter` keys win over the note's own, and every other key is kept. The result goes through the same
    checks as `note build` before the write. A validation error exits 1 and leaves the file untouched. The note's
    `[[links]]` stay in the body, where `studio index` turns them into edges. The schema and vocabulary come from
    `--knowledge-base`, or from the knowledge base the working directory sits in.
    """
    note_path = Path(path)
    knowledge_base_path = _resolve_knowledge_base_path(knowledge_base)
    announce_knowledge_base(knowledge_base_path)
    vocabulary = _load_vocabulary(knowledge_base_path)
    schema = load_knowledge_base_schema_or_fail(knowledge_base_path)

    try:
        existing = note_path.read_text(encoding="utf-8")
        overrides = json.loads(Path(frontmatter).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        _fail(f"could not read inputs: {exc}")
    if not isinstance(overrides, dict):
        _fail("--frontmatter must be a JSON object")

    data, body = split_frontmatter(existing)
    merged: dict[str, JsonValue] = dict(data)
    for key, value in overrides.items():
        merged[str(key)] = value
    rendered = render_frontmatter(merged, body)

    try:
        note = kb.parse(rendered, source_path=note_path.name, spec=schema)
    except kb.NoteGrammarError as exc:
        _fail(f"normalized note does not parse: {exc}")

    report = validate_note(
        note, vocabulary, spec=schema, path_root=_path_root(knowledge_base_path, schema), path_root_source=knowledge_base_path_origin(knowledge_base)
    )
    if report.n_errors > 0:
        _print_validation_report(f"normalize {note_path.name}", report)
        raise typer.Exit(code=1)

    atomic_write(note_path, rendered)
    typer.echo(f"normalized {note_path}")


@note_app.command(name="schema")
def note_schema_cmd(
    page_type: str = typer.Option(..., "--type", help="The page type to emit a JSON schema for."),
    knowledge_base: str = typer.Option(
        default=".", help="Knowledge base root for spec resolution, defaults to '.' (the working directory)."
    ),
) -> None:
    """Print the authoring JSON schema of one page type, and exit 1 on a page type the knowledge base does not have.

    `--type` is looked up among the page types of the knowledge base's `knowledge-base-schema.json`, so
    `note schema --type recipe --knowledge-base <cooking>` prints the recipe schema of that knowledge base. A
    knowledge base with no `knowledge-base-schema.json` exits 1, naming a template to start one from.
    """
    knowledge_base_path = Path(knowledge_base)
    announce_knowledge_base(knowledge_base_path, err=True)  # stdout carries only the JSON schema
    schema = load_knowledge_base_schema_or_fail(knowledge_base_path)
    if page_type not in schema.page_type_values:
        _fail(f"unknown page type {page_type!r} for this knowledge base. Expected one of {', '.join(sorted(schema.page_type_values))}")
    typer.echo(json.dumps(kb.payload_json_schema(page_type, spec=schema), indent=2))
