---
id: engram-adr-0036-behavioral-parity-generated-sql
aliases: ["engram-adr-0036-behavioral-parity-generated-sql"]
type: adr
app: engram
status: accepted
tags: []
title: "ADR-0036 — Behavioural parity, not byte-equivalence, for generated retrieval SQL"
---

# ADR-0036 — Behavioural parity, not byte-equivalence, for generated retrieval SQL

**Status:** Accepted (2026-07-09). Covers the migration-touching slice that moved `api.search` / `api.graph_*` generation behind the library SQL-builder ABCs ([ADR-0035](0035-retrieval-sql-from-descriptors.md)). Scope: the no-regression *gate* for that slice — a verification/testing decision, not a code contract. Records why the plan's original "byte-equivalent DDL" gate was replaced with a result-parity gate. No schema, `api` result surface, or `wiki_reader` role change.

**Context.** The original plan set a gate: the library builders must emit DDL **byte-equivalent** to engram's hand-written bodies, so the migration diff would be empty and parity self-evident. Grounding in the real bodies before implementation showed that gate is impractical as written, because the two search dialects — and the two graph backends — are **structurally divergent by design**, not mere BM25-token variants:

- **ParadeDB** `api.search`: a dynamic `RETURN QUERY EXECUTE $q$ … $q$ USING` with each scoring leg in its own `AS MATERIALIZED` CTE, positional `$1..$N` params, bare (unqualified) columns, and `@@@ paradedb.parse` / `paradedb.score() DESC`.
- **`pg_textsearch`** `api.search`: a static `RETURN QUERY WITH` with plain CTEs, named parameters, `#variable_conflict use_variable`, relation-qualified columns, a per-leg top-`k` slice, and `<@> to_bm25query(...) < 0` ascending.
- **CTE graph:** `LANGUAGE sql`, `WITH RECURSIVE` over `wiki.edges`. **AGE graph:** `LANGUAGE plpgsql`, `RETURN QUERY EXECUTE` wrapping `cypher('wiki_kg', $cy$ … $cy$, $1)` with agtype casts.

Each body also carried a large hand-written header COMMENT stored in `pg_proc.prosrc`. A single ABC skeleton with "~30-line BM25 hooks" cannot regenerate both dialects' bodies byte-for-byte; forcing it to would push every point of divergence (the outer form, materialisation, param style, column qualification, the cypher machinery) into the shared skeleton as conditionals — defeating the extraction the ABC exists to serve.

**Options considered.**

- **Hold the byte-equivalence gate.** Rejected — impractical as written (the two bodies are divergent by design), and meeting it would re-absorb both dialects' full divergence into the ABC, the opposite of the seam's intent.
- **Snapshot-test the generated SQL text against a checked-in fixture.** Rejected — a text snapshot locks the generator to one dialect's incidental formatting and turns every whitespace change into a failing test, while proving nothing about behaviour; the testcontainers tiers already exercise the real functions on real data.
- **Prove no-regression behaviourally via the existing result-parity tiers (chosen).**

**Decision.**

The builders generate **clean, canonical SQL** from the `CorpusSchema` / `GraphSchema` ([ADR-0035](0035-retrieval-sql-from-descriptors.md)). The dialect/backend hooks own the grammar **and** the workaround shape. The migration's emitted body *text* changes. No-regression is proven **behaviourally**: the existing testcontainers integration/e2e tiers run the generated `api.search` / `api.graph_expand` / `api.graph_paths` and assert identical results on the **local Postgres bed** (port 5434, the primary test bed, [ADR-0017](0017-local-postgres-primary-testbed.md)) **and** ParadeDB (`KNOWLEDGE_BASE_TEST_DIALECT=pg_search`), plus a non-destructive parity probe for any other database (the generated bodies materialised under a throwaway `api_scratch` schema, compared row-for-row against that database's `api.*` surface, then dropped). Result parity across every bed is the safety net. Text identity is not.

The behavioural latitude enabled two deliberate simplifications in the generated SQL (both valid on both dialects): always-qualify the leg/semi-join projections (killing the qualification divergence with no hook), and unify each surface's two legs onto one `base + pool-semijoin` FROM (behaviourally identical, since the facet pool equals the facet-filtered set). The whitespace-exact SQL-text unit assertions in `tests/unit/serve/test_search_dialect.py` were relaxed to whitespace-collapsed structural checks — the body is generated now, so its indentation is not the contract, which is exactly what a behavioural gate retires.

In practice the generated engram bodies came out token-identical to the originals but for two inert internal aliases (the CTE-paths accumulator and the AGE cypher payload alias), so parity was near-total by inspection too — but the *gate* is behavioural, not textual, and that is the durable decision.

**Consequences.**

- (+) **The ABC can emit clean canonical SQL.** It is freed from reproducing each dialect's incidental formatting, so the shared skeleton stays readable and the hooks stay small.
- (+) **The regression net is stronger than text-identity.** The result-parity tiers on the local Postgres bed and ParadeDB, plus the gated `pg_textsearch` profile, prove *behaviour* (identical rows for identical queries), which is what actually matters. A byte diff would have proven only layout.
- (−) **The migration/adapter diff shows changed SQL text.** A reviewer must read that as expected (the body is generated) and rely on the parity tiers, not the diff, as the check — this ADR is the standing note that says so.
- (−) **A generator bug that produces valid-but-wrong SQL is only caught at the DB tiers**, not by a cheap text assertion. The tiers must stay green on both dialects (and be spot-checked in the gated profile) for the gate to hold.
- **Reversible?** Yes — this is a verification decision, not code. A future change could re-introduce a text snapshot for one dialect if a concrete reason emerged, without touching the builders.

**Related.** [ADR-0035](0035-retrieval-sql-from-descriptors.md) (the descriptor + builder seam this gate validates), [ADR-0006](0006-dual-dialect-bm25-seam.md) and [ADR-0007](0007-age-multihop-dual-graph-backend.md) (the divergent dialect/backend bodies that make byte-equivalence impractical), [ADR-0017](0017-local-postgres-primary-testbed.md) (the local Postgres primary test bed the parity tiers run on), [ADR-0025](0025-intentional-deviations-register.md) (the register discipline for by-design deviations, of which this is one, a documented departure from the plan's letter), [root ADR-0006](../../../docs/adr/0006-engramai-shared-ai-library.md) (the extraction). Code: `tests/unit/serve/test_search_dialect.py` (the relaxed structural assertions), the testcontainers `tests/integration/` + `tests/e2e/` search/graph tiers (the behavioural net), generators in `engram.ai.retrieval.{search_builder,graph_builder}` (`engramai`). Entry point: [CONTEXT-MAP.md](../../../CONTEXT-MAP.md). Glossary: [engram/CONTEXT.md](../../CONTEXT.md).
