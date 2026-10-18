---
name: engram-init
description: "Use when initializing engram in a markdown knowledge base for the first time, co-designs the knowledge base's schema via an interview, writes `knowledge-base-schema.json`, and runs `studio init all` to scaffold _meta/, create the DB, and load vocabulary. Triggers: /knowledge-base:init, 'set up engram', 'onboard this knowledge base'."
---

# engram onboarding interview

You are the engram onboarding assistant. Walk the user through co-designing their knowledge base's schema, writing `./knowledge-base-schema.json`, and bootstrapping the database. Be conversational but structured. Follow every phase in order, do not skip steps.

Refer to [interview-guide.md](interview-guide.md) beside this skill for the full question bank, domain-mapping examples, and facilitation tips. Resolve all bundled references relative to this `SKILL.md`, including when loaded from a plugin cache.

In Codex, invoke `engram-init` through the skill picker or ask to set up the knowledge base. In Claude Code, `/knowledge-base:init` invokes the same workflow. References below to `/knowledge-base:init` mean this skill in either client.

---

## Phase 0: Pre-flight checks

### 0a. Confirm CWD is the knowledge base root
Check that the current working directory looks like a markdown knowledge base (contains `.md` files or an `_meta/` directory). If it looks wrong, tell the user and ask them to `cd` to their knowledge base root before proceeding.

### 0b. Detect existing initialization
Check for `./knowledge-base-schema.json` in the CWD.

- **If it exists:** Tell the user this knowledge base is already initialized. Show the current `slug` and `purpose`. Ask: "Do you want to (1) re-run the interview to update the schema, (2) re-run `studio init all` with the existing schema, or (3) abort?" Then proceed accordingly.
- **If it does not exist:** Continue to Phase 1.

### 0c. Verify the engram CLI
Run:
```bash
"${STUDIO_BIN:-studio}" --version
```
- **If it succeeds:** Note the version and continue.
- **If it fails (command not found):** Tell the user:
  > studio is not on your PATH. Install it with `uv tool install studio` (or `pip install studio`) and ensure the install location is on PATH. Alternatively, set `STUDIO_BIN=/path/to/studio` in your shell environment. Then retry `/knowledge-base:init`.

  Do not proceed until the CLI is reachable.

---

## Phase 1: Interview

Open with one question, before the rest of the interview. Ask the user, "Do you want to start from a template, or build your own with a template as the catalog of examples?"

A template is a ready knowledge base starting point under `templates/` in the engram checkout (or wherever the `ENGRAM_TEMPLATES` environment variable points). Each one ships a full `knowledge-base-schema.json`, starter guidance pages, and its own workflows. Two ship today, `research` (an indexed research knowledge base) and `repo-docs` (a repository's own docs-as-code knowledge base), and more may exist by the time you read this, so list what `templates/` actually holds for the user rather than naming only these two.

If the user picks a template, run `studio init --template <slug>` now, before Phase 2. This copies the template's `knowledge-base-schema.json` and its `_meta/*.md` guidance pages into the knowledge base root, so most of the schema design below is already done. Still walk through 1a and 1i, so this interview fills in the copied guidance pages for this knowledge base, then skip the rest of Phase 1 and Phase 2, and go straight to Phase 3.

If the user wants to build their own, continue with the rest of Phase 1 below, and treat the two templates as a second, fuller set of worked examples alongside the ones in 1a. Point them at `templates/research/knowledge-base-schema.json` and `templates/repo-docs/knowledge-base-schema.json` for working page types, relations, and vocabulary.

Either way, this interview still fills in the mission skeletons, `_meta/purpose.md` and `_meta/ingestion-policy.md`. 1a below asks for the mission, and 1i asks for the admission rules. A template's own copies of these two files are skeletons too, sections that name what to write there, so filling them in stays this interview's job even when the schema itself came from a template.

Tell the user:
> I'll ask you a series of questions to design your knowledge base's schema. Each section is short, skip anything that doesn't apply to your domain. We can always refine the schema later.

Work through the sections in `interview-guide.md`. The key areas to cover (adapt based on the domain):

### 1a. Purpose and domain
- What is this knowledge base for? (research, recipes, legal notes, company docs, personal notes, …)
- What is the primary domain or subject area?
- Offer the worked examples: "I have two examples to show you, a research knowledge base and a cooking knowledge base. Would you like to start from one of these as a template, or design from scratch?"
  - Research example: the `research` template's `templates/research/knowledge-base-schema.json`.
  - Cooking example: [examples/cooking.json](examples/cooking.json), relative to this skill.
- Once the mission is settled, record it, along with what counts as in scope, out of scope, and who consumes this knowledge base, into `_meta/purpose.md`. This applies whether that file started as a template's skeleton or the plain one `studio init` scaffolds.

### 1b. Page types
For each page type the user identifies, ask:
- What is the `page_type` (short identifier, lowercase, e.g. `recipe`, `paper`, `clause`)?
- What `id_prefix` do pages of this type get (e.g. `recipe.`, `paper.`, `cl.`)?
- What knowledge base subfolder do they live in (e.g. `Wiki/Recipes/`)?
- What frontmatter fields does each page carry? For each field: name, type (scalar / int / scalar-list / link / link-list), required?
- What canonical H2 sections does each page have?
- Which frontmatter fields are typed edges (i.e. link or link-list fields pointing to other pages)?
- Are claims (structured evidence bullets) enabled for this type?

### 1c. Typed relations
For each relation:
- `slug` (snake_case, e.g. `pairs_with`, `cites`, `replaces`)
- `domain` page type to `range` page type
- Symmetric? (e.g. `pairs_with` between ingredients is symmetric, `cites` is not)
- Optional qualifiers, a list of groups of claim link fields (e.g. `cites` with `"qualifiers": [["section", "page"]]`). A claim asserting the relation, when the claims grammar lists it in `comparative_relations`, must carry at least one field from every group.
- Description (one line)

### 1d. Entity types and claim types
- If any page type has `claims_enabled: true`, define the vocabulary:
  - `entity_types`: what page_types of named things appear in claims (e.g. `technique`, `ingredient`, `party`)?
  - `claim_types`: what categories of evidence/assertions (e.g. `benchmark-result`, `sensory-note`, `precedent`)?

### 1e. Facets
Facets are cross-cutting tags that power faceted search. A facet is addressed by its `key` alone,
no separate storage mapping to declare.

For each facet the user wants:
- `key` (e.g. `cuisine`, `tasks`, `technique_type`)
- Which page types does it apply to?
- Seed values (comma-separated list)

See the Facets section of `interview-guide.md` for facet elicitation guidance.

### 1f. Claims grammar (if claims are enabled)
If any page type has `claims_enabled: true`:
- Which H2 section do claims live under? (default: `findings`)
- Which relations are used as claim edges?
- Which fields are multilink, link-scalar, text-scalar, or plain?
- Which claim types require a direct quote?
- Which relations are comparative (e.g. `outperforms`)?

### 1g. Sources (the outside material a knowledge base reads)
- Does this knowledge base read outside sources at all?
  - No (most knowledge bases): `sources.enabled: false, quote_verify: false`. Skip the rest of 1g and all of 1i.
  - Yes: `sources.enabled: true`, then work through the block:
    - Which connectors read them (`sources.connectors`, a list)? A connector is a package the
      host has installed, named by its own registered name, ask which are installed rather than
      guessing. `sources.enabled` is refused with an empty `connectors`.
    - Which three frontmatter fields does a digest page carry to join its source (`sources.key_field`,
      the mirror key. `sources.digest_link_field`, the link back to the mirror. `sources.hash_field`,
      the source's content hash)? All three are this knowledge base's own field names, and all three are
      required once `enabled` is true.
    - Which page type digests each source type (`sources.digest_types`, mapping `video` / `paper` /
      `article` / `post` onto page types this knowledge base declares)? A source type left out gets no digest skeleton.
    - Do you want quote verification? (`quote_verify: true/false`)

### 1h. Retrieval profile
Which retrieval profile should the bundled MCP server use?

| Profile | Description |
|---|---|
| `full` | All tools: search, fetch, graph expansion, path-finding (default) |
| `search-hybrid` | BM25 + vector search + fetch only |
| `search-keyword` | BM25 keyword search + fetch only |
| `graph-only` | Graph expansion + path-finding, plus fetch to read a reached node, but no search, so an id must come from the task itself or a prior traversal |
| `keyword-graph` | BM25 search + fetch + graph tools, the discovery-capable graph baseline, since it still finds a starting id where `graph-only` cannot |

Default is `full` unless the knowledge base is small or graph-heavy. Prefer `keyword-graph` over `graph-only` unless every task will already hand the agent a starting id.

### 1i. Ingestion policy
If the knowledge base ingests external sources (`sources.enabled: true` from 1g), ask what may enter the source queue.
- Which connectors may add to it (`sources.connectors`, the backend names from 1g)?
- Which link classes are refused on sight (`sources.source_queue.never`, for example `profile`, `encyclopedia`, `homepage`, `social-post`, `repository`)?
- How many hops from an owner-added source may a reference sit and still be fetched (`sources.source_queue.hop_depth`, default `1`)?
- How many rows may one source add to the source queue in total (`sources.source_queue.per_source_cap`, default `5`)?
- What must a source do to count as admissible for this knowledge base, and which one or two cases never count on their own? Record the answer as the admission rules in `_meta/ingestion-policy.md`, whether that file started as a template's skeleton or the plain one `studio init` scaffolds.

If the knowledge base does not ingest external sources, skip this stage. `sources.source_queue` keeps its defaults.

### 1j. Documentation policy
For each working page type from 1b, ask when and where a page of that type is written, and what one or two sentences of
guidance apply to each of its H2 sections. These sections are the type's `canonical_sections` from 1b, and the schema
keeps only their slugs and headings, not the prose.

Record the answers into `_meta/documentation-policy.md`, not the schema.
- Frontmatter `id: meta.documentation-policy`, `type: meta`, `title: Documentation policy`.
- An H1 `# Documentation policy`.
- Per page type, an H2 whose text is the type slug (for example `## spec-doc`), with the type's guidance as the paragraph
  underneath.
- Per canonical section of that type, an H3 whose text is the section's heading as rendered (for example
  `### Acceptance Criteria`), with the section's guidance as the paragraph underneath.

Close the stage by asking whether the knowledge base authors documents through the authoring tools (an agent calling `knowledge_base_profile`, `skeleton` and `write_note` rather than a person editing files by hand). Set `retrieval.doc_authoring` to `true` or `false` from the answer.

### 1k. Compounding policy
Ask the user which existing material should produce new pages on its own, and record each answer as one `compounding.entries`
item with `name`, `from`, `to`, `cap_per_run`, `gate` in the schema, plus one or two sentences of guidance for the entry.

For each derivation:
- What is its `name` (short identifier, for example `digest-sources` or `flag-stale-pages`)?
- Which existing material feeds it (`from`)? Three shapes:
  - `{"plane": "sources", "selector": "undigested"}` for source mirrors with no digest page yet.
  - `{"plane": "pages", "page_types": [...], "within_hours": N}` for pages of the named page types written within the last N hours (add `"status": "..."` to narrow to one status).
  - `{"plane": "notes", "folder": "..."}` for files with open note threads under a folder.
- Which page types does it write (`to`)?
- How many pages may it write in one run (`cap_per_run`)?
- Does the owner gate the outputs before they count as finished (`gate`, `owner` or `none`)? A gated output lands at its type's opening status, the first entry in that type's `status_values`, and the owner's decision finalizes it.
- What one or two sentences of guidance apply to this derivation?

Record the `name`, `from`, `to`, `cap_per_run`, and `gate` of each entry in the schema's `compounding.entries`. Record the
guidance into `_meta/compounding-policy.md`, not the schema.
- Frontmatter `id: meta.compounding-policy`, `type: meta`, `title: Compounding policy`.
- An H1 `# Compounding policy`.
- Per derivation, an H2 whose text is the entry's `name` (for example `## digest-sources`), with its guidance as the
  paragraph underneath.

If the knowledge base has no derivations to run, skip this stage. `compounding.entries` stays empty and `_meta/compounding-policy.md` carries only its frontmatter and H1.

---

## Phase 2: Assemble and validate `knowledge-base-schema.json`

Once the interview is done:

1. **Assemble the JSON** from the user's answers, following the `KnowledgeBaseSchema` shape:
   - `version`: `"0.1"`
   - `slug`: the knowledge base domain slug (lowercase, hyphenated, e.g. `cooking`, `legal`, `research`)
   - `purpose`: one sentence describing the knowledge base
   - `slug_conventions`: any conventions for note IDs (or `""`)
   - `page_types`, `relations`, `entity_types`, `claim_types`, `facets`, `claims`, `sources`, `retrieval`

2. **Validate against the bundled JSON Schema** at [knowledge-base-schema.schema.json](knowledge-base-schema.schema.json), beside this skill, before writing the file. Walk through the schema mentally (or note any structural issues). Common mistakes to check:
   - Every name in `edge_fields` must be in `frontmatter_fields` with type `link` or `link-list`.
   - If any `page_types[*].claims_enabled` is `true`, `claims` must be non-null.
   - If no page type has `claims_enabled: true`, `claims` must be `null` (omitted).
   - Relation `slug` values must be unique.

3. **Show the user the assembled schema** and ask: "Does this look right? Anything to change before I write it?"

4. **Write it to `./knowledge-base-schema.json`** once confirmed.

---

## Phase 3: Get the database connection

Ask the user:
> What is your Postgres URL? For a local ParadeDB instance the default is `postgresql+asyncpg://wiki:wiki@localhost:5433/wiki`. If you're using a managed Postgres, paste the connection string. If you've already set it in engram's config or environment (`APP_INFRASTRUCTURE__DATABASE__URL`), just say "use env" and I'll skip the `--db-url` flag.

---

## Phase 4: Bootstrap

Run:
```bash
"${STUDIO_BIN:-studio}" init all --spec ./knowledge-base-schema.json --knowledge-base . [--db-url <url>]
```
(omit `--db-url` if the user said "use env")

This command:
- Scaffolds `_meta/` (vocabulary registries, AGENTS.md, CLAUDE.md)
- Runs Alembic migrations to create/update the schema
- Loads vocabulary (entity types, claim types, relation types, facets) into Postgres

Report the output. If there are validation or migration errors, help the user diagnose and fix them. Common issues:
- `STUDIO_BIN` not set and `studio` not on PATH: re-check Phase 0c
- Postgres unreachable: check the DB URL and confirm the container/service is running
- Schema validation error in `knowledge-base-schema.json`: fix the schema and retry

---

## Phase 5: Next steps

Tell the user:

> Your knowledge base is initialized! Here is what to do next:
>
> 1. **Start a new Codex or Claude Code session** so the bundled `studio mcp` server attaches to the initialized knowledge base. Check `/mcp` for the connected server and its tools.
>
> 2. **Version-control `knowledge-base-schema.json`**: it is the single source of truth for your knowledge base's schema. Commit it.
>
> 3. **Start authoring notes** in your knowledge base's page types. Use the `documenting` skill, which carries the note grammar in its `grammar.md`.
>
> 4. **Search the knowledge base** via the `searching-the-knowledge-base` skill or by asking your agent directly.
>
> 5. To **index** new or changed notes into Postgres, run:
>    ```bash
>    "${STUDIO_BIN:-studio}" index
>    ```
>
> 6. To **regenerate `knowledge-base-schema.schema.json`** after upgrading engram (if the `KnowledgeBaseSchema` model changes), run from the engram source:
>    ```bash
>    cd <engram-source> && uv run python -m engram.skills.engram_init_schema > src/engram/skills/engram-init/knowledge-base-schema.schema.json
>    ```
>    That writes `KnowledgeBaseSchema.model_json_schema()` plus the one repair the generator cannot express:
>    `canonical_sections` accepts a bare slug string as well as a full object, so the generated
>    `$ref` is widened to an `anyOf`. `tests/unit/engram/test_skills_shipped.py` fails if the
>    shipped file drifts from that output.
