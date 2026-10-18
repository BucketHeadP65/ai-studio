# engram onboarding interview guide

This is the facilitator's reference for the `/knowledge-base:init` skill. Each section maps a domain concept onto the `KnowledgeBaseSchema` shape. Use it to ask the right questions and map answers onto the schema correctly.

---

## Section 1: Purpose and domain

**Goal:** Establish the vocabulary and scope before designing the schema.

**Questions:**
- What do you use this knowledge base for? (e.g. "tracking research papers", "storing recipes", "drafting legal memos", "documenting company processes")
- What is the primary subject domain?
- Who reads the knowledge base, just you, a small team, or agents?
- How large do you expect the knowledge base to grow? (dozens, hundreds, or thousands of notes)

**Mapping:**
- `slug`: short domain identifier, lowercase, hyphenated (e.g. `research`, `cooking`, `legal`, `company-wiki`)
- `purpose`: one-sentence summary of what the knowledge base is for
- `slug_conventions`: any naming conventions for note IDs (or leave `""` if none yet)

**Worked example, cooking:**
> The knowledge base is a personal recipe collection. Domain is cooking. Slug = `cooking`. Purpose = "A personal recipe collection organized by cuisine and technique."

**Worked example, research:**
> The knowledge base indexes academic papers on retrieval-augmented generation and context engineering. Slug = `research`. Purpose = "An indexed research knowledge base for literature on retrieval/RAG and LLM context engineering."

---

## Section 2: Page types

**Goal:** Identify the distinct note shapes. Each page type gets its own `page_type`, `id_prefix`, folder, and frontmatter schema.

**Elicitation prompts:**
- "What are the main 'things' you write notes about?" (e.g. papers, recipes, clauses, projects, people)
- For each type: "What information does every note of this type carry in its header (frontmatter)?"
- "Do notes of this type link to notes of other types?"

**Schema mapping:**
| User concept | Spec field |
|---|---|
| Note type name | `page_type` (e.g. `recipe`, `paper`, `clause`) |
| Short ID prefix | `id_prefix` (e.g. `recipe.`, `paper.`, `cl.`) |
| Storage folder | `folder` (e.g. `Wiki/Recipes/`, `Sources/`) |
| Header fields | `frontmatter_fields` |
| Structured body sections | `canonical_sections` (ordered H2 slugs) |
| Fields that point to other notes | `edge_fields` (must be `link` or `link-list` fields) |
| Evidence bullets | `claims_enabled: true` (only if this type carries claims) |

**Field types:**
| Type | Meaning |
|---|---|
| `scalar` | Plain string |
| `int` | Integer |
| `scalar-list` | List of strings |
| `link` | Single wikilink `[[note-id]]` |
| `link-list` | List of wikilinks |

**Good defaults for canonical sections:**
- General note: `["overview", "notes"]`
- Recipe: `["ingredients", "method", "notes"]`
- Paper: `["tl_dr", "context", "approach", "evaluation_setup", "findings", "limitations", "practical_notes"]`
- Legal clause: `["summary", "text", "interpretation", "related_clauses"]`
- Project: `["goal", "status", "decisions", "tasks", "retrospective"]`

**Domain mini-examples:**

*Cooking:*
```
page_type: page_type=recipe, id_prefix=recipe., folder=Wiki/Recipes/
  fields: servings (int, required), cuisine (scalar), pairs_with (link-list)
  sections: ingredients, method, notes
  edge_fields: [pairs_with]
  claims_enabled: false

page_type: page_type=ingredient, id_prefix=ing., folder=Wiki/Ingredients/
  fields: substitute_for (link-list)
  sections: description, uses
  edge_fields: [substitute_for]
  claims_enabled: false
```

*Legal:*
```
page_type: page_type=clause, id_prefix=cl., folder=Wiki/Clauses/
  fields: statute_ref (scalar, required), jurisdiction (scalar), superseded_by (link)
  sections: summary, text, interpretation, related_clauses
  edge_fields: [superseded_by]
  claims_enabled: false

page_type: page_type=case, id_prefix=case., folder=Wiki/Cases/
  fields: court (scalar, required), year (int, required), cites (link-list)
  sections: facts, holding, analysis, notes
  edge_fields: [cites]
  claims_enabled: false
```

*Personal notes:*
```
page_type: page_type=note, id_prefix=n., folder=Notes/
  fields: (none required)
  sections: content, references
  edge_fields: []
  claims_enabled: false

page_type: page_type=project, id_prefix=proj., folder=Projects/
  fields: status (scalar, choices=["active","paused","done"], default=active)
  sections: goal, status, decisions, tasks
  edge_fields: []
  claims_enabled: false
```

*Company docs:*
```
page_type: page_type=process, id_prefix=proc., folder=Processes/
  fields: owner (scalar, required), status (scalar, choices=["draft","active","deprecated"])
  sections: purpose, steps, exceptions, history
  edge_fields: []
  claims_enabled: false

page_type: page_type=decision, id_prefix=adr., folder=Decisions/
  fields: status (scalar, required), deciders (scalar-list)
  sections: context, decision, consequences
  edge_fields: []
  claims_enabled: false
```

---

## Section 3: Typed relations

**Goal:** Define the graph edges between page types (beyond inline wikilinks).

**Elicitation prompts:**
- "Are there named relationships between your note types?" (e.g. "a recipe PAIRS_WITH an ingredient", "a clause SUPERSEDES another clause")
- "Are any of these symmetric, i.e. if A is related to B, is B automatically related to A?" (e.g. `pairs_with` between ingredients)
- "Do any relationships carry extra metadata?" (e.g. in the research template, `outperforms` carries `on_dataset` or `on_task`, and `metric`)

**Schema mapping:**
| User concept | Spec field |
|---|---|
| Relationship name | `slug` (snake_case) |
| Source page type | `domain` |
| Target page type | `range` |
| Bidirectional? | `symmetric: true` |
| Extra metadata fields | `qualifiers`, a list of groups of claim link fields (e.g. the research template's `[["on_dataset", "on_task"], ["metric"]]`). A claim asserting the relation, when the claims grammar lists it in `comparative_relations`, must carry at least one field from every group. |
| One-line explanation | `description` |

**Note:** A relation in `relations` must correspond to a `link` or `link-list` field in at least one page type's `edge_fields`. It is also used as the `slug` in claims if `claims_enabled`.

**Domain mini-examples:**

*Cooking:*
```
pairs_with: ingredient → ingredient, symmetric=true — "Flavour pairing"
substitute_for: ingredient → ingredient, symmetric=false — "Can replace in a recipe"
features: recipe → ingredient, symmetric=false — "Recipe uses this ingredient"
```

*Legal:*
```
supersedes: clause → clause, symmetric=false — "This clause replaces the older one"
cites: case → case, symmetric=false — "Cites this precedent"
interprets: case → clause, symmetric=false — "Interprets this clause"
```

---

## Section 4: Entity types and claim types (only if claims are enabled)

**Goal:** Define the closed vocabulary for claims. Only needed if at least one page type has `claims_enabled: true`.

**Elicitation prompts:**
- "What page_types of named 'things' appear in your evidence bullets?": `entity_types`
- "What categories of assertion do you make?": `claim_types`

**Schema mapping:** Each `VocabEntry` has a `value` (the token) and a `definition` (one sentence).

**When to enable claims:** Claims model structured, citable evidence. They make most sense for research (benchmark results, ablations), legal (holdings, precedents), or any domain where you want to track who-said-what-and-where. For cooking or personal notes, leave claims off.

**Research example entity_types:** `technique`, `task`, `problem`, `dataset`, `benchmark`, `metric`, `tool`, `model`, `concept`

**Research example claim_types:** `benchmark-result`, `ablation`, `replication`, `negative-result`, `theoretical`, `qualitative`, `author-claim`, `observation`

---

## Section 5: Facets

**Goal:** Declare each cross-cutting tag as a facet, by its key. A page writes a facet's values as a list under that key in its frontmatter, and `api.facets` serves one row per page, facet and value.

**Each facet declares:**
| Field | Meaning |
|---|---|
| `key` | The facet's name, and the frontmatter field a page writes its values under. |
| `scope` | Which `page_type` values it applies to (informational, empty = all). |
| `seed` | The facet's first vocabulary values. |
| `card` | Whether a page's search card prints this facet's values (default false). |

**Elicitation prompts:**
- "Are there any cross-cutting tags you want to apply across multiple note types?"
- "Which page types carry each one, and what are its first values?"
- "Which of them should a search result show at a glance?" (those get `card: true`)

**Domain mini-examples:**

*Cooking:*
```
cuisine: scope=[recipe], seed=[italian, thai, french, indian, japanese, mexican, middle-eastern], card=true
dietary: scope=[recipe], seed=[vegan, vegetarian, gluten-free, dairy-free, nut-free]
meal_type: scope=[recipe], seed=[breakfast, lunch, dinner, snack, dessert]
```

*Research (the `research` template):*
```
tasks: scope=[paper, entity, comparison, playbook, question], seed=[open-domain-qa, multi-hop-qa, long-doc-qa, ...], card=true
domains: scope=[paper, playbook], seed=[encyclopedic, legal, biomed, ...], card=true
stages: scope=[entity, comparison, playbook], seed=[indexing, chunking, embedding, ...], card=true
modalities: scope=[paper, entity, playbook], seed=[text, code, tables, ...]
scale_regimes: scope=[playbook, comparison], seed=[toy, small, large, ...]
```

*Legal:*
```
jurisdiction: scope=[clause, case], seed=[england-wales, scotland, eu, us-federal, us-ny], card=true
practice_area: scope=[clause, case], seed=[contract, property, tort, ip, employment, criminal]
```

---

## Section 6: Claims grammar (only if claims are enabled)

**Goal:** Define the claim-bullet grammar. Only fill this in if at least one page type has `claims_enabled: true`.

| Field | Description |
|---|---|
| `section` | The H2 slug where claims live (default: `findings`) |
| `edge_relations` | Relation slugs that appear as claim verbs (e.g. `supports`, `refutes`, `outperforms`) |
| `multilink_fields` | Fields that take multiple wikilinks (e.g. the research template's `on_dataset`, `metric`, `disputed_by`) |
| `link_scalar_fields` | Fields that take a single wikilink (e.g. the research template's `subject`, `on_task`) |
| `text_scalar_fields` | Free-text fields (e.g. the research template's `condition`, `baseline`, `magnitude`, `status`) |
| `plain_fields` | Untyped fields (e.g. `loc`, `quote`, `kind`) |
| `quote_required_types` | Claim page_types that require a direct quote (e.g. `benchmark-result`, `ablation`) |
| `comparative_relations` | Relations encoding ordered comparisons (e.g. `outperforms`, `comparable_to`) |

---

## Section 7: Sources

**Purpose:** The sources layer controls the external-ingest pipeline, what a knowledge base mirrors from outside itself, and how a
page written from a mirror points back at it. engram owns the pipeline. The backends that fetch are connector packages a host
installs, and this block names the ones this knowledge base uses.

- `sources.enabled: true`: turn on if this knowledge base mirrors external material at all. Off (the default) means the knowledge base is
  authored by hand and the rest of this block stays empty.
- `sources.connectors`: the connectors that feed it, a list of entry-point names. Ask the owner which connector packages
  their host has installed and take the names from there. Do not suggest one. `enabled: true` with an empty list is refused,
  and a name nothing installs fails at sync time naming itself.
- `sources.key_field` / `sources.digest_link_field` / `sources.hash_field`: the three frontmatter fields a digest page carries
  to join the source it was written from: the mirror key, the link back to the mirror, and the source's content hash. These are
  this knowledge base's own field names, and all three are required once `enabled` is true, engram has no default for any of them.
- `sources.digest_types`: which page type digests each source type, mapping engram's four source types (`video`, `paper`, `article`,
  `post`) onto page types this knowledge base declares. A source type left out of the map has no digest page type and its skeleton is refused.
- `sources.quote_verify: true`: turn on if you want engram to verify that quoted text actually appears in the source (requires
  the conversion/OCR pipeline).

**Default for a knowledge base with no external material:** `sources.enabled: false, quote_verify: false`, everything else unset.

---

## Section 8: Retrieval profile

**Purpose:** Controls which MCP tools the bundled `studio mcp` server exposes.

| Profile | Tools active |
|---|---|
| `full` | `search`, `fetch`, `graph_expand`, `find_paths`, `run_sql`, `describe_schema` |
| `search-hybrid` | `search` (BM25+vector), `fetch`, `run_sql`, `describe_schema` |
| `search-keyword` | `search` (BM25 only), `fetch`, `run_sql`, `describe_schema` |
| `graph-only` | `fetch`, `graph_expand`, `find_paths`, `run_sql`, `describe_schema`, no `search`, so `graph_expand`/`find_paths` ids must come from the task itself or a prior traversal |
| `keyword-graph` | `search` (BM25 only), `fetch`, `graph_expand`, `find_paths`, `run_sql`, `describe_schema`, the discovery-capable graph baseline: it can still find a starting id, which `graph-only` cannot |

`run_sql` and `describe_schema` are always active regardless of profile.

**Recommendation:**
- **New knowledge base, general use**: `full` (default)
- **Mostly keyword search, no graph**: `search-hybrid` or `search-keyword`
- **Entity-graph-heavy, few documents**: `keyword-graph`, or `graph-only` only when every task already hands the agent a starting id
- **Large knowledge base, cost-sensitive**: `search-keyword`

---

## Quick domain to schema mapping cheat sheet

| Domain | Typical page_types | Notes |
|---|---|---|
| Research knowledge base | paper, entity, concept, comparison, playbook, question, moc | claims_enabled on paper, sources.enabled |
| Recipe / cooking | recipe, ingredient | symmetric `pairs_with`, cuisine facet |
| Legal | clause, case, statute | supersedes/cites relations, jurisdiction facet |
| Personal notes | note, project, diary | Minimal schema, no claims, no sources |
| Company docs | process, decision, team, project | status fields, owner fields, no graph needed |
| Software project | module, issue, decision, runbook | depends_on relations, component facet |
| Reading notes | book, note, author | read_by/cites relations, genre facet |
