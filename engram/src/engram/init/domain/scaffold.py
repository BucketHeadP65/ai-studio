"""Render a `KnowledgeBaseSchema` into the text of every `_meta/` file a new knowledge base starts with.

It does no IO. The result maps knowledge-base-relative POSIX paths to file text.
"""

from __future__ import annotations

import importlib.metadata

from typing import TYPE_CHECKING

from engram.kb.contracts import DEFAULT_GUIDANCE, render_policy

if TYPE_CHECKING:
    from engram.kb.contracts import KnowledgeBaseSchema, PageTypeSpec, RelationDecl


def scaffold(schema: KnowledgeBaseSchema) -> dict[str, str]:
    """Turn a `KnowledgeBaseSchema` into the full set of knowledge base `_meta/` files.

    It does no IO. Callers may inspect, diff or write the result. `write_scaffold` is the idempotent writer.

    Args:
        schema: The knowledge base schema to render into files.

    Returns:
        A `dict[knowledge_base_relative_posix_path, file_text]` covering all `_meta/`
        vocabulary files, templates (one per non-meta page type), prose files
        (purpose.md, ingestion-policy.md, schema.md), `knowledge-base-schema.json`, and
        `AGENTS.md`/`CLAUDE.md` at the knowledge base root.
    """
    files: dict[str, str] = {}

    # Vocabulary
    files["_meta/vocabulary/entity-types.md"] = _render_entity_types(schema)
    files["_meta/vocabulary/relation-types.md"] = _render_relation_types(schema)
    files["_meta/vocabulary/facets.md"] = _render_facets(schema)

    # Templates, one per non-meta page type
    for page_type_schema in schema.page_types:
        if page_type_schema.page_type == "meta":
            continue
        files[f"_meta/templates/tpl-{page_type_schema.page_type}.md"] = _render_template(schema, page_type_schema)

    # Prose / structural
    files["_meta/purpose.md"] = _render_purpose(schema)
    files["_meta/ingestion-policy.md"] = render_policy(schema.sources.source_queue, DEFAULT_GUIDANCE)
    files["_meta/schema.md"] = _render_schema(schema)

    # Root agent guidance
    files["AGENTS.md"] = _render_agents(schema)
    files["CLAUDE.md"] = _render_claude(schema)

    # knowledge-base-schema.json, the auditable record
    files["knowledge-base-schema.json"] = schema.model_dump_json(indent=2) + "\n"

    return files


def _meta_frontmatter(note_id: str, title: str, summary: str = "") -> str:
    """Render a meta note's YAML frontmatter block with its `---` delimiters, omitting an empty summary."""
    lines = ["---", f"id: {note_id}", "type: meta", f'title: "{title}"']
    if summary:
        lines.append(f'summary: "{summary}"')
    lines += ["status: verified", "---", ""]
    return "\n".join(lines)


def _render_entity_types(schema: KnowledgeBaseSchema) -> str:
    """Render `_meta/vocabulary/entity-types.md` as one three-column table with each value in backticks."""
    frontmatter = _meta_frontmatter(
        "meta.vocabulary.entity-types",
        "Vocabulary: entity types",
        f"Controlled vocabulary for entity_type on entity pages in the {schema.slug} knowledge_base.",
    )
    lines = [
        frontmatter,
        "# Entity types",
        "",
        f"Entity types for the **{schema.slug}** knowledge_base.",
        "",
        "| entity_type | Definition | Examples |",
        "|---|---|---|",
    ]
    for entry in schema.entity_types:
        defn = entry.definition.replace("|", "\\|")
        lines.append(f"| `{entry.value}` | {defn} | |")
    lines.append("")
    return "\n".join(lines)


def _render_relation_types(schema: KnowledgeBaseSchema) -> str:
    """Render `_meta/vocabulary/relation-types.md`, putting relations with no domain and no range in a two-column table."""
    frontmatter = _meta_frontmatter(
        "meta.vocabulary.relation-types", "Vocabulary: relation types", f"The typed-link vocabulary for the {schema.slug} knowledge_base."
    )
    normal: list[RelationDecl] = []
    lifecycle: list[RelationDecl] = []
    for relation in schema.relations:
        if not relation.domain and not relation.range:
            lifecycle.append(relation)
        else:
            normal.append(relation)

    lines = [frontmatter, "# Relation types", "", "Typed relations for this knowledge base. Unregistered keys fail validation.", ""]

    if normal:
        lines += ["## Relations", "", "| Relation | Domain → Range | Meaning |", "|---|---|---|"]
        for relation in normal:
            arrow = "↔" if relation.symmetric else "→"
            # A literal "|" in a domain or range is escaped as "\|" so it survives the table cell.
            # parse_vocabulary unescapes it, so a multi-valued domain or range round-trips intact.
            domain = relation.domain.replace("|", "\\|")
            rng = relation.range.replace("|", "\\|")
            desc = relation.description.replace("|", "\\|")
            lines.append(f"| `{relation.slug}` | {domain} {arrow} {rng} | {desc} |")
        lines.append("")

    if lifecycle:
        lines += ["## Lifecycle", "", "| Relation | Meaning |", "|---|"]
        for relation in lifecycle:
            desc = relation.description.replace("|", "\\|")
            lines.append(f"| `{relation.slug}` | {desc} |")
        lines.append("")

    return "\n".join(lines)


def _render_facets(schema: KnowledgeBaseSchema) -> str:
    """Render `_meta/vocabulary/facets.md`, one section per facet plus a `kind` section for claim kinds."""
    frontmatter = _meta_frontmatter(
        "meta.vocabulary.facets", "Vocabulary: facet keys & controlled values", f"Structured filter vocabulary for the {schema.slug} knowledge_base."
    )
    lines = [frontmatter, "# Facets", "", "Facets are the noise-reduction surface: agents shrink the search space with WHERE clauses on facets.", ""]

    for fspec in schema.facets:
        scope_str = ", ".join(fspec.scope) if fspec.scope else "all"
        lines += [f"## `{fspec.key}`: on {scope_str}", "", "| Value | Definition |", "|---|---|"]
        for seed in fspec.seed:
            cell = seed.replace("|", "\\|")
            lines.append(f"| `{cell}` | |")
        lines.append("")

    # Claim kinds go under the `kind` facet, parse_vocabulary routes this to claim_types
    if schema.claim_types:
        lines += ["## `kind`: claim kinds", "", "| Value | Definition |", "|---|---|"]
        for entry in schema.claim_types:
            defn = entry.definition.replace("|", "\\|")
            lines.append(f"| `{entry.value}` | {defn} |")
        lines.append("")

    return "\n".join(lines)


def _slug_to_heading(slug: str) -> str:
    """Title-case an underscore-separated section slug, such as `open_questions` to `Open Questions`."""
    return " ".join(word.capitalize() for word in slug.split("_"))


def _render_template(schema: KnowledgeBaseSchema, page_type_schema: PageTypeSpec) -> str:
    """Render the starter template for one non-meta page type, with a claim stub in its claims section."""
    id_prefix = page_type_schema.id_prefix
    page_type = page_type_schema.page_type

    # --- frontmatter ---
    fm_lines = [
        "---",
        f"id: {id_prefix}<slug>",
        f"type: {page_type}",
        'title: "<title>"',
        "summary: <≤280 chars>",
        f"status: {page_type_schema.status_values[0] if page_type_schema.status_values else 'stub'}",
        "aliases: []",
    ]
    for ff in page_type_schema.frontmatter_fields:
        if ff.type in ("link", "link-list"):
            fm_lines.append(f"{ff.name}: []")
        elif ff.type == "int":
            placeholder = "0" if ff.required else ""
            fm_lines.append(f"{ff.name}: {placeholder}")
        elif ff.choices:
            fm_lines.append(f"{ff.name}: {ff.choices[0]}  # {' | '.join(ff.choices)}")
        else:
            fm_lines.append(f"{ff.name}: ")
    fm_lines += ["---", ""]

    # --- body sections ---
    claims_section = schema.claims.section if schema.claims else None
    body_lines: list[str] = []
    for section_slug in page_type_schema.canonical_section_slugs:
        heading = page_type_schema.canonical_headings.get(section_slug, _slug_to_heading(section_slug))
        body_lines += [f"## {heading}", ""]
        if page_type_schema.claims_enabled and section_slug == claims_section:
            # Claim stub
            body_lines += [
                "%% 5 to 15 atomic claims. One claim = one bullet + ^cN + key:: sub-bullets. %%",
                "",
                "- <atomic, self-contained claim sentence>. ^c1",
                "    - loc:: §",
                "    - kind:: <kind>",
                "",
            ]
        else:
            body_lines += ["", ""]

    # The extra "\n" puts the blank line YAML frontmatter parsers need between the closing --- and the body.
    return "\n".join(fm_lines) + "\n" + "\n".join(body_lines)


def _render_purpose(schema: KnowledgeBaseSchema) -> str:
    """Render `_meta/purpose.md`."""
    frontmatter = _meta_frontmatter("meta.purpose", "Purpose & scope", f"Purpose and scope of the {schema.slug} knowledge_base.")
    mission = schema.purpose or f"An indexed {schema.slug} knowledge base."
    return "\n".join(
        [
            frontmatter,
            "# Purpose & scope",
            "",
            "## Mission",
            "",
            mission,
            "",
            "## In scope",
            "",
            f"Notes, relations, and evidence curated for the **{schema.slug}** knowledge_base.",
            "",
            "## Out of scope",
            "",
            "Raw source material that has not been curated into memory notes.",
            "",
            "## Consumers",
            "",
            "Agents and humans retrieving indexed knowledge via the engram MCP server.",
            "",
        ]
    )


def _render_schema(schema: KnowledgeBaseSchema) -> str:
    """Render `_meta/schema.md`."""
    try:
        engram_version = importlib.metadata.version("engram")
    except importlib.metadata.PackageNotFoundError:
        engram_version = schema.version

    frontmatter = _meta_frontmatter(
        "meta.schema",
        f"{schema.slug.capitalize()} schema: the contract",
        f"Schema contract for the {schema.slug} knowledge base (spec v{schema.version}).",
    )

    lines = [
        frontmatter,
        f"# {schema.slug.capitalize()} schema: the contract",
        "",
        f"Spec version: `{schema.version}` · Generated by engram `{engram_version}`.",
        "",
        "## Page types",
        "",
        "| type | id prefix | sections | claims |",
        "|---|---|---|---|",
    ]
    for page_type_schema in schema.page_types:
        prefix_str = f"`{page_type_schema.id_prefix}`" if page_type_schema.id_prefix else "-"
        sections_str = ", ".join(page_type_schema.canonical_section_slugs) if page_type_schema.canonical_section_slugs else "-"
        claims_str = "yes" if page_type_schema.claims_enabled else "no"
        lines.append(f"| {page_type_schema.page_type} | {prefix_str} | {sections_str} | {claims_str} |")

    lines += [
        "",
        "## Frontmatter contract",
        "",
        "Universal fields on every note: `id`, `type`, `title`, `summary`, `status`, `aliases`.",
        "",
    ]
    for page_type_schema in schema.page_types:
        if page_type_schema.frontmatter_fields:
            field_names = ", ".join(f"`{f.name}`" for f in page_type_schema.frontmatter_fields)
            lines.append(f"**{page_type_schema.page_type}**: {field_names}")
            lines.append("")

    return "\n".join(lines)


def _render_agents(schema: KnowledgeBaseSchema) -> str:
    """Render `AGENTS.md`, the knowledge base's agent guidance, with no absolute filesystem path."""
    page_types = ", ".join(f"`{page_type_schema.page_type}`" for page_type_schema in schema.page_types if page_type_schema.page_type != "meta")
    return "\n".join(
        [
            f"# {schema.slug.capitalize()} knowledge base: agent guide",
            "",
            f"This is an engram-indexed **{schema.slug}** knowledge_base.",
            "",
            "## Authoring notes",
            "",
            "- Use the engram knowledge base plugin's authoring skill to create notes: it enforces the schema.",
            f"- Supported page types: {page_types}.",
            "- The full schema contract lives in `_meta/schema.md`.",
            "- Controlled vocabulary (entity types, relations, facets) lives in `_meta/vocabulary/`.",
            "",
            "## Retrieval",
            "",
            "- Retrieval is served by the knowledge base MCP server (`studio mcp`).",
            "- Use `search` for semantic + BM25 lookup and `fetch` to retrieve a single note by id.",
            "",
            "## Adding vocabulary",
            "",
            "- New **facet values**: add a row to `_meta/vocabulary/facets.md`.",
            "- New **entity types**: add a row to `_meta/vocabulary/entity-types.md`.",
            "- New **relation types**: add a row to `_meta/vocabulary/relation-types.md`.",
            "- New **facet keys** or structural changes need a schema update (`knowledge-base-schema.json`).",
            "",
        ]
    )


def _render_claude(schema: KnowledgeBaseSchema) -> str:
    """Render `CLAUDE.md`, the knowledge base's Claude Code guidance."""
    return "\n".join(
        [
            f"# {schema.slug.capitalize()} knowledge base: Claude Code guide",
            "",
            f"This knowledge base is indexed by engram (slug: **{schema.slug}**).",
            "",
            "## Working in this knowledge base",
            "",
            "- Schema: `_meta/schema.md`: page types, id prefixes, section order, frontmatter.",
            "- Vocabulary: `_meta/vocabulary/`: entity types, relation types, facet values.",
            "- Templates: `_meta/templates/`: starter note templates per page type.",
            "",
            "## Authoring",
            "",
            "- Follow the template for the relevant page type.",
            "- Validate with the studio plugin (`studio note validate <path>`).",
            "- Controlled vocabulary in `_meta/vocabulary/`: unknown values fail validation.",
            "",
            "## Retrieval",
            "",
            "- The engram MCP server exposes `search` and `fetch` tools.",
            "- Start `studio mcp` to expose the retrieval interface.",
            "",
            "## Schema updates",
            "",
            "- Edit `knowledge-base-schema.json` and re-run `studio init` to regenerate vocabulary files (union-merge: human edits preserved).",
            "",
        ]
    )
