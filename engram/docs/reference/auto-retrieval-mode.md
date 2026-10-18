---
id: engram-auto-retrieval-mode
aliases: ["engram-auto-retrieval-mode"]
type: reference
app: engram
status: current
tags: []
title: "auto-retrieval-mode, the knowledge base earns its machinery"
---

# auto-retrieval-mode, the knowledge base earns its machinery

## What auto mode is

Fixed mode is the default, and a fixed-mode knowledge base behaves exactly as it always has: embeddings on
every write, hybrid search on every query, the SQL and graph tools mounted by hand. Nothing on this
page changes that path. Everything below is scoped to a `KnowledgeBaseSchema` whose `RetrievalSpec.mode` is
`"auto"` rather than the default `"fixed"` (`engram.kb.domain.schema.RetrievalSpec`).

An auto-mode knowledge base earns its retrieval machinery instead of carrying all of it from the first note.
`engram.indexing.domain.switches.decide_switches` reads the shape of the knowledge base's stored rows and turns three
independent switches (`embeddings`, `sql`, `graph`) on or off. Every read and write surface in
engram then consults the stored result and behaves accordingly. The harness, the memory adapter and
the plugin never read the switch record themselves, and never change what they call. `studio mcp`
does read it: it reads the served knowledge base's switches once at start and passes them to
`create_mcp_server`.

## The three switches

Each switch has an on-condition and a separate, looser off-condition. Between the two the decision
carries the current stored value through unchanged, so a knowledge base that crosses a threshold and drifts
back a little does not flap (`engram.indexing.domain.switches`):

| Switch | Turns on when (any) | Turns off when (all) | Signals |
|---|---|---|---|
| `embeddings` | notes > 200, or token estimate > 150,000 | notes ≤ 100 and token estimate ≤ 75,000 | note count, token estimate |
| `sql` | notes > 1000, or page types > 5 | notes ≤ 500 and page types ≤ 2 | note count, page type count |
| `graph` | edges/note ≥ 1.5 and giant component ≥ 0.6 and notes > 100 | edges/note < 0.75, or notes ≤ 50 | edges-per-note, giant-component share |

`embeddings` gates whether a write embeds (`engram.indexing.service.ingest.IngestPageHandler`: a caller may
still veto with `embed=False`, but `embed=True` no longer forces it on an auto-mode schema) and
whether search runs its vector leg. `sql` gates the `Earned`-wrapped SQL tools
(`engram.retrieval.earned`). `graph` gates `graph_expand`/`find_paths`'s appearance on
`RetrievalKnowledgeBase`. A `RetrievalSpec` may also pin switches regardless of the count: `floor` (names
forced on) applies after the threshold decision, `ceiling` (when given, names the only switches
allowed on) applies last and overrides both the thresholds and `floor`.

## The signals

`engram.indexing.infrastructure.repository.SignalsQueries.measure` computes the five signals fresh at every
indexing run and every page write, scoped to the collection:

- **notes**, `count(*)` over `kb.pages`. Every pages row counts, including the synthetic,
  edge-free orientation pages (`api.about` and friends). They inflate the denominator without ever
  contributing an edge, so `edges_per_note` reads a little sparser than the authored notes' own
  interconnectedness alone would suggest.
- **token_estimate**, `sum(length(body)) / 4` over `kb.sections`, a coarse token count.
- **page types**, `count(DISTINCT page_type)` over `kb.pages`.
- **edges_per_note**, `count(*)` over `kb.edges` with both endpoints in the collection, divided
  by `notes`. Counts only resolved relations, never the dangling half (`kb.link_refs`), so a
  knowledge base rich in unresolved links reads sparser still.
- **giant_component**, the share of notes reachable, undirected, from the highest-degree node (a
  deterministic tie-break on ties), via a set-based reachability walk. Skipped (reported `0.0`)
  whenever `edges_per_note` is already below the graph off-threshold (0.75). The expensive walk
  never runs when the decision cannot turn `graph` on regardless of its result.

## Where the record lives

The decided switches persist on `kb.index_fingerprint.switches` (migration 0032), a jsonb
column `engram.indexing.service.switch_stage.SwitchStage` stamps for an auto-mode schema. It
stamps after every real index (full and incremental alike, never a dry run) and after every page
write into a written collection (`engram.indexing.service.ingest.IngestPageHandler`). A fixed-mode
index leaves the decided switches untouched. On both modes the stage also stores the schema's
`retrieval.private` flag as the `private` key, writing nothing when the stored flag already agrees.
It then gives a private knowledge base its own login reader role and its row in `kb.reader_scopes`,
or takes a shared knowledge base's row away, which is what `api.visible_collection` reads (engram
ADR-0046). A written collection's first write creates its
fingerprint row, with empty version and hash columns that no index matches, and every later stamp
writes the `switches` column alone. The table stays ungranted to `kb_reader`, exactly like the rest
of the index-state tables: `api.retrieval_switches(p_collection)` is the one deliberate hole, a
`SECURITY DEFINER` function returning `'{}'::jsonb` (read as every switch off) for a collection with
no fingerprint row, rather than no row. It is the one reader gateway onto this record. Nothing else
touches the column.

## Freshness: three different clocks

The switch record is read in three places, each with its own freshness contract:

1. **Per-request, on the read gateway.** `engram.retrieval.infrastructure.embedded.switch_source` reads
   `api.retrieval_switches` fresh, through the reader gateway, on every call, no cache. This is
   what `ReadSurface.switch_source` gives `RetrievalKnowledgeBase`: both the search leg and the graph-tool
   exposure consult it, so a flip a later index makes is live on the very next call. `studio mcp`
   sets it on every auto-mode surface too, so its routed `search` picks its leg the same way.
2. **Build-time snapshot, on the toolset builders and the MCP server.** `retrieval.tools.build_retrieval_toolset`,
   `register_mcp_tools` and `create_mcp_server` take an optional `switches` parameter, resolved once
   when the toolset or the server is built. An off graph switch withholds `graph_expand` and
   `find_paths` even when `only` names them, and `create_mcp_server` applies it to its own graph tools
   and to the `run_code` sandbox. `studio mcp` reads the served knowledge base's switches once at
   start, so a flip reaches its graph tools on the next process start.
3. **Within one turn after use, on the Earned SQL tools.** `engram.retrieval.earned.SwitchCache` seeds
   from a real read at open, then every `.switches()` call answers from the last completed read and
   kicks a background refresh (skipped when one is already in flight, or no event loop is running).
   A flip an owner makes through the store reaches this surface within one turn after any later use.

## The honesty line

Every search hit is stamped `answered_by` (`"lexical"` or `"hybrid"`, a `Literal` on
`core.ai.retrieval.contracts.SearchHit`) with the leg that actually answered it, fixed-mode and
auto-mode knowledge bases alike. The toolset surface appends "Answered by hybrid." / "Answered by lexical."
to the result's `next_steps.summary`. This is the one trace of the mechanism a consumer ever sees.

## Cost, and fail-closed

A search over an auto-mode `RetrievalKnowledgeBase` issues one switch read per model request (deciding
whether `graph_expand`/`find_paths` appear this turn) plus one further read per surface actually
searched (deciding that surface's hybrid leg), none of it cached on the read gateway itself, by design,
so freshness never lags a stale in-process value. `SQLAlchemyError` or a `ValidationError` on that
read (a pre-migration database, a malformed row) both degrade the same way: every switch reads off,
with one logged warning, never a failed tool call.

## What consumers see

Nothing new unless a knowledge base earns it. The same `search` and `fetch` calls, the same envelopes, the
same MCP surface, plus the honesty line, and `graph_expand`/`find_paths` appearing on a surface once
its graph switch is on. A fixed knowledge base keeps exactly the two tools it always had.

## Known seams

- `filtered()` on `SqlKnowledgeBase`/`RetrievalKnowledgeBase` does not gate by switches, the same doctrine dials
  already follow: a static grant read off `filtered()` reflects what was built, not what is currently
  earned.
- `Earned.wrapped` is reachable directly, bypassing `earned()`, the same doctrine `Switched`
  (`harness.doors`) already follows for its own wrapped capability.
- The research knowledge base stays fixed-mode by convention: `harness.adapters.knowledge_base.knowledge_base_capability`
  narrows the opened capability with `isinstance(capability, SqlKnowledgeBase)` and raises `TypeError` if it
  is not. Were the research knowledge base's schema ever set to `"auto"`, this breaks loudly rather than
  silently mounting an `Earned` wrapper harness does not expect.
- A written collection's pages written while `embeddings` was off keep a NULL embedding after it turns
  on. A page write embeds only its own rows, and no index runs over a written collection, so search
  answers hybrid over the vectors of the pages written since the switch turned on.
