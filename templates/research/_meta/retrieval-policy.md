---
id: meta.retrieval-policy
aliases: [meta.retrieval-policy]
type: meta
title: "Retrieval policy: question archetypes to strategy"
summary: The living routing doctrine for query agents, classify the question, pick the entry surface, escalate deliberately, respect token budgets. Indexed into api.about. Evolves with usage evidence.
status: draft
created: 2026-06-10
updated: 2026-06-10
---

# Retrieval policy

**The router is a document, not code (D-16).** A query agent reads this once per session (L0, via `api.about`) and *decides*. Strategy selection is judgment over a living doctrine, not a hardcoded classifier. This page evolves like any other: edits logged, superseded when usage evidence from `wiki.query_log` (D-17) shows a strategy under/over-performing.

## Step zero: classify the question

| Archetype | Smell test | Entry surface | Escalate to | Session budget* |
|---|---|---|---|---|
| **Exploratory** | Vague, no named entities ("best practices for RAG?") | `api.search(q, k=>15)` collapsed across levels, + `domain`/`task` facet if known | Top playbook/concept cards, then their `tldr` sections, then a claims sanity-check on `api.claims` | ~4-6 queries, ≤5k tok loaded |
| **Navigational** | "What does the knowledge base know about X?" | `moc` cards (`type='moc'` facet match), then `api.facets` counts per facet value | Entity cards on the map, then `tldr`/`evidence` sections, edges 1 hop | ~2-3 queries, ≤2k tok |
| **Factual / claim lookup** | Named entities, checkable ("did anyone replicate LightRAG > GraphRAG on legal?") | **SQL-first** on `api.claims` (subject/object/dataset/kind filters) + `api.edges` (`replicates`, `outperforms`) | Hybrid search on `level=>'claim'` if SQL misses, L4 verify | ~3 queries, ≤3k tok |
| **Comparative** | "X vs Y (for my case)?" | comparison cards and their sections | `api.claims` about each subject, then claims behind disputed cells | ~3-5 queries, ≤4k tok |
| **Situational / prescriptive** | A context + a design ask ("50k legal PDFs, low latency, design retrieval") | Playbook facet match: `api.cards WHERE type='playbook'` joined to `api.facets` on `scale_regimes`/`domains`/`tasks`, `api.search(level=>'card')` for fuzzy fit | Compose playbooks, then check `caveats`, gaps, then draft a `q.*` page | ~5-8 queries, ≤6k tok |
| **Verification / provenance** | "Is this claim solid / current?" | `api.claims`, then `quote`/`loc`, then `api.raw_chunks` span | `api.contradictions`, `api.stale`, retraction status | ~3 queries, ≤3k tok |
| **Gap-finding** | "What's unknown / contested?" | `api.contradictions`, `api.cards WHERE type='question' AND frontmatter->>'question_status' = 'open'`, `api.stale` | `api.claims` subjects with thin or old evidence | ~3 queries, ≤3k tok |

*Budgets are **recommended doctrine** (D-20): exceed them knowingly when the question warrants it. Sessions >2 times budget get flagged by the validation digest, recurring overspend recalibrates this table or tightens an infrastructure floor. Hard floors (timeouts, `k≤50`, fetch truncation) live in the DB and are not negotiable.

## Standing rules (context engineering)

1. **Orient once:** `api.about` + `api.schema_guide` at session start. Never re-read mid-session.
2. **Filter before rank:** facet `WHERE` clauses shrink the space before any BM25/vector work runs, `collection` first (your mission's default scope, D-19), then domain/task/stage.
3. **Cards before bodies:** never fetch a section/page you haven't seen the card of (exception: claim-lookup, which is row-shaped).
4. **Targeted reads:** prefer `canon`-addressed sections (`tldr`, `limitations`, `verdict`) over whole pages. `api.fetch(page)` is a last resort.
5. **Always `LIMIT`:** treat truncation notes as a signal to narrow, not to page through.
6. **Stop on saturation:** when a new query returns mostly already-seen ids, stop retrieving and synthesize.
7. **Verify only what you'll assert:** L4 raw spans are for claims you are about to rely on or republish, not routine browsing.
8. **Trust filters:** default to `only_current => true` (excludes superseded/stubs/retracted). Opt in to history explicitly when auditing.
9. **Leave a trace (D-17):** log every session via `api.log_session(...)`. An answer becomes/extends a `q.*` page iff it synthesized across **≥3 pages** AND no existing page answers it, search before writing. Recurring themes (≥3 related question pages) are playbook/concept candidates. The validation digest flags them.
