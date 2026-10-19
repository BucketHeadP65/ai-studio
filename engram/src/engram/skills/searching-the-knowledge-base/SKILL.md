---
name: searching-the-knowledge-base
description: "Use when retrieving from an engram-indexed knowledge base, drives the engram retrieval surface (the MCP tools search/fetch/graph_expand/find_paths + run_sql/describe_schema over the api.* schema, and the `studio search`/`studio fetch` CLI). Domain-general: works for any knowledge base's schema. Triggers: 'search the knowledge base', 'find notes about X', 'what does the knowledge base say about Y'."
---

# Searching the knowledge base

This skill is **domain-agnostic**, it works for any engram-indexed knowledge base regardless of whether that knowledge base contains research papers, recipes, legal clauses, or any other note type. The retrieval surface is the same. Only the schema (page types, facets, entity types) differs. Read `./knowledge-base-schema.json` to understand this knowledge base's specific schema before querying.

---

## Tuned retrieval instructions

The block between the sentinels below is **managed by `studio evals apply-policy`**. It carries the winning tuned system instruction for the agent-under-test. Do not hand-edit it. Running `apply-policy` rewrites it (working-tree only, you review the diff and commit).

<!-- BEGIN ENGRAM TUNED INSTRUCTIONS -->
You are an assistant for an engram-indexed knowledge base. Use the retrieval tools to find supporting sources, then answer the question and cite the page ids you relied on.
<!-- END ENGRAM TUNED INSTRUCTIONS -->

---

## Retrieval surface

engram exposes three retrieval surfaces. Prefer them in this order:

### 1. MCP tools (preferred, available after `studio mcp` attaches)

The plugin registers these tools automatically when the MCP server is running. Which tools are present depends on this knowledge base's **`retrieval.profile`** declared in its `knowledge-base-schema.json` (default `full`):

| Tool | Profile | Description |
|---|---|---|
| `search` | all profiles except `graph-only` | Hybrid BM25 + vector retrieval. **The primary entry point.** Returns ranked page summaries with scores. Not present under `graph-only`, which grounds `graph_expand`/`find_paths` ids from the task instead. |
| `fetch` | all profiles | Fetch a specific page by its `id` (e.g. `recipe.bolognese` in a cooking knowledge base). Returns the full page. Under `graph-only`, the id comes from a `graph_expand`/`find_paths` result or from the task, never from `search`. |
| `graph_expand` | `full`, `graph-only`, `keyword-graph` | Expand the typed-relation graph one or more hops from a starting page id. Returns neighbouring pages and the relation types connecting them. Under `graph-only`, the starting id is one the task already gave you, not one `search` found. |
| `find_paths` | `full`, `graph-only`, `keyword-graph` | Find typed-relation paths between two page ids. Useful for "how does X relate to Y?" Under `graph-only`, both ids come from the task, not from `search`. |
| `describe_schema` | all profiles | Returns the M-Schema description of the live `api.*` views, call this first before `run_sql` to understand what columns and tables are available for THIS knowledge base. |
| `run_sql` | all profiles | Execute a read-only SQL query over the `api.*` views. Use for precise structured filters (by facet value, status, type, date, etc.). |
| `run_code` | `code-mode` only (opt-in) | Run Python in a Monty sandbox where the retrieval functions are bound as callables, orchestrate several retrieval calls + filter/aggregate locally in ONE round-trip. Only present when the server runs the `code-mode` profile. See "Code mode" below. |
| `submit_feedback` | all profiles | Report back on these tools: an unclear/misleading instruction, a missing capability, a nice-to-have, a bottleneck, or a bug. Recorded locally for the maintainer (never sent anywhere). See "Giving feedback" below. |

#### Effective MCP usage

**`search`**: hybrid BM25 + vector. Use it as the primary entry for any natural-language question. Tune with:
- `-k N`: number of results to return (default varies, try 10-20 for broad exploration)
- `--task <task-description>`: a retrieval-task hint that biases the embedding
- `--bm25-only`: keyword-only retrieval, useful when the query is a precise term/name rather than a concept
- `facets`: keep only pages that carry the given facet values, written `{"<key>": ["<value>"]}`. Values for one key are alternatives and several keys must all match. The tool's description names the keys this knowledge base declares, and a value must be written exactly as the knowledge base stores it.

**`fetch`**: use when you already know the page `id`. More precise than search for a specific page. Combine with search: search to find the id, then fetch for the full content.

**`graph_expand` and `find_paths`**, use for relational questions: "what does this page link to?", "which pages link to this one?", "what are the paths between page A and page B?". These tools traverse the indexed typed-relation graph. They are faster and more precise than searching for relation terms. Ground `seed_ids` / `from_id` / `to_id` from a prior `search` hit when `search` is present. Under `graph-only`, there is no `search` to ground with, so start from ids the task already gave you, then `fetch` a reached node to read it. `keyword-graph` is the discovery-capable graph baseline: it keeps keyword `search` and `fetch` alongside the graph tools, so reach for it over `graph-only` whenever the task does not already hand you a starting id.

**`describe_schema` + `run_sql`**, use for structured queries that require filtering, counting, or sorting. Always call `describe_schema` first in a new session to learn the live `api.*` schema, which reflects THIS knowledge base's schema via M-Schema. Then write SQL against the concrete view/column names returned. Examples:
  - Filter by a facet value: `SELECT c.id, c.title, c.summary FROM api.cards c JOIN api.facets f ON f.collection = c.src_collection AND f.page_id = c.id WHERE f.facet = '<facet>' AND f.value = '<value>' ORDER BY c.updated DESC`
  - Count by type: `SELECT type, COUNT(*) FROM api.cards GROUP BY type`
  - Find all pages with a specific status: `SELECT id, title FROM api.cards WHERE status = 'draft'`
  - Join claims to pages for evidence queries (schema-dependent, check describe_schema for the claims view name)

#### Code mode (`run_code`): opt-in, one round-trip orchestration

`run_code` is **opt-in**: it appears only when the knowledge base runs the `code-mode` profile, set `retrieval.profile` to `"code-mode"` in this knowledge base's `knowledge-base-schema.json`, then restart the MCP server (an `APP_RETRIEVAL__RETRIEVAL_PROFILE` env var can override it for ops/dev). **Use it only if `run_code` is in your live tool list**, otherwise stick to the individual tools above.

Reach for it when a task needs **several retrieval calls plus local filtering/aggregation**, and doing that as separate tool calls would be many slow round-trips: `run_code` runs your Python in a Monty sandbox (a Python subset) where engram's retrieval functions are bound as callables, so loops, conditionals, `asyncio.gather`, dedup, and ranking all happen in ONE round-trip. For a single lookup, just call `search`/`fetch` directly. `run_code` earns its keep only when you're composing.

Bound functions (call them directly, do **not** import or redefine them):

- `await search(query, k=12, task=None)` returns a list of hit dicts
- `await fetch(unit_id)` returns the unit dict (or raises if the id is unknown, re-ground with `search`)
- `await graph_expand(seed_ids, hops=2)` returns the reached subgraph (`{nodes, edges}`)
- `await find_paths(from_id, to_id)` returns a list of ranked path dicts
- `await run_sql(sql, row_limit=50)` returns a list of row dicts (one per row, `column => value`)
- `describe_schema()` returns the M-Schema text (the one non-structured return, it's a document)

Key rules, all consistent with the tool's own description:

- **Returns are structured data**: dicts and lists, not objects. Index a hit by key: `hit['id']`, `hit['score']`, `row['title']`, **not** `hit.id`.
- **Async functions need `await`.** Run independent calls concurrently with `asyncio.gather`.
- **Read-only.** Everything goes through the same read-only `api.*` surface (the `kb_reader` role) and the 50-row cap, exactly the constraints of calling the tools directly. A rejected SQL statement raises inside the sandbox.
- **Return the value, don't print it.** The last expression's value is captured as the result. Use `print()` only for debug logging.

Worked example: search two phrasings in parallel, dedupe by id, then fetch the single top hit's full text, all in one call:

```python
import asyncio

# Two phrasings of the same intent, run concurrently (queries from a research knowledge base).
a, b = await asyncio.gather(
    search(query="graph-structured retrieval", k=10),
    search(query="multi-hop question answering over a knowledge graph", k=10),
)

# Merge + dedupe by id, keeping the highest score seen for each.
best: dict[str, dict] = {}
for hit in [*a, *b]:
    cur = best.get(hit["id"])
    if cur is None or hit["score"] > cur["score"]:
        best[hit["id"]] = hit

ranked = sorted(best.values(), key=lambda h: h["score"], reverse=True)
top = await fetch(unit_id=ranked[0]["id"])   # full text of the single best hit

{"top_id": ranked[0]["id"], "candidates": [h["id"] for h in ranked], "body": top["body"]}
```

### 2. CLI fallback (when MCP is not attached)

```bash
"${STUDIO_BIN:-studio}" search "QUERY" [-k N] [--task X] [--bm25-only]
"${STUDIO_BIN:-studio}" fetch ID
```

The CLI exposes the same hybrid retrieval as the MCP `search` tool. Use it when the MCP server is not attached (e.g. in a fresh session before reload, or in a non-interactive context). `search` prints one line per hit, `score  id  [level]  title`, and under a hit, indented, its folded units: more units of the same document that matched, each as `id  [level]` with its snippet on the line below. `fetch ID` reads any of those ids. `ID` is the full page id (e.g. `recipe.bolognese` in a cooking knowledge base), or the id of a section, claim or chunk a search printed.

#### Giving feedback (`submit_feedback`)

When you actually use these tools and hit something worth improving (a tool description that misled you, a capability you wished existed, a nice-to-have, a slow path/bottleneck, or a bug) call `submit_feedback(message, category, tool)` to record it. `category` ∈ `usage | feature | nice-to-have | bottleneck | bug | other`. Name the `tool` it's about when relevant. It's appended to a local file (`_meta/mcp-feedback.jsonl`) for the knowledge base's maintainer to review, it never leaves the machine and doesn't change retrieval. Use it sparingly, for concrete and actionable observations (not routine acknowledgements). This is how the tools get better for the next session.

---

## Retrieval ladder

Start broad, then narrow:

1. **Summary pass**: `search "QUERY" -k 10` to get an overview of what the knowledge base contains on the topic. Read the returned summaries and scores. Pass `since` and `until` (a year, a month or a day) to keep only pages written in that period.
2. **First section**: `fetch` the highest-scoring pages and read the first of the page's canonical sections. Enough for a quick orientation.
3. **Full sections**: fetch specific pages for deep dives into the page's other canonical sections (`note schema` lists them for each page type).
4. **Structured filter**: when you need precision (all pages with a specific facet value, all claims about one subject, pages written after a date), use `describe_schema` + `run_sql`.
5. **Graph traversal**: when the question is relational ("what does this page build on?", "which pages link to this one?"), use `graph_expand` or `find_paths`.

---

## Schema-agnostic querying

Do not assume the knowledge base's schema. The page types, facet names, relation types, and entity types all come from `knowledge-base-schema.json`. Before writing a SQL filter or using a facet term, check:

```bash
# What page types exist?
"${STUDIO_BIN:-studio}" note schema --knowledge-base .   # lists available page_types

# What does the live api.* schema look like?
# (via MCP) → describe_schema
# (via CLI) → run: SELECT * FROM information_schema.views WHERE table_schema = 'api'
```

Take page types, statuses and facet keys from the schema, never from one knowledge base's assumptions. A cooking knowledge base has `recipe.` ids and `cuisine` facets. A legal knowledge base has `clause.` ids and `jurisdiction` facets. A research knowledge base has `paper.` ids and `tasks`/`domains` facets. The retrieval discipline is identical.

---

## Common patterns

**"What does the knowledge base say about X?"**
```
search "X" -k 10
→ fetch top 2–3 results for full content
→ graph_expand from most relevant page if you want related pages
```

**"Find all pages of type Y"**
```
describe_schema
→ run_sql: SELECT id, title, summary FROM api.cards WHERE type = 'Y' ORDER BY title
```

**"What relates to page Z?"**
```
graph_expand <page-id> hops=1
→ find_paths <page-id> <other-page-id>   # if you want the path between two specific pages
```

**"Find pages where field F = V"**
```
describe_schema   # a frontmatter field lives in api.cards.frontmatter
→ run_sql: SELECT id, title FROM api.cards WHERE frontmatter->>'F' = 'V'
```

**"Is there evidence for claim about entity E?"**
```
search "E <claim topic>" --task "find evidence about E" -k 15
→ fetch relevant pages and read their claims section
→ run_sql: SELECT page_title, text, quote, loc FROM api.claims WHERE subject_name ILIKE '%E%'
```

---

## No absolute paths

All CLI invocations use `"${STUDIO_BIN:-studio}"`. No hard-coded binary paths. The knowledge base is the CWD. Use `--knowledge-base .` when a command requires it.
