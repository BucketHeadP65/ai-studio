---
id: engram-adr-0023-sql-boundary-type-safety
aliases: ["engram-adr-0023-sql-boundary-type-safety"]
type: adr
app: engram
status: accepted
tags: []
title: "ADR-0023 — SQL-boundary type safety"
---

# ADR-0023 — SQL-boundary type safety

**Status:** Accepted (2026-06-30). Delivered in the `engram-domain-remediation` initiative (Slice C, the
SQL-boundary-type-safety slice, tasks C1 through C4). Builds on the explicit composition root
([ADR-0021](0021-explicit-eager-composition-root.md)) and the evaluation sub-packages plus the `serve_bridge`
anti-corruption seam ([ADR-0022](0022-evaluation-context-subpackages.md)), both consumed unchanged. Scope: the
compile Load-path row builders, the serve `api.*` read contract, and the evals→serve active-policy seam. No
schema, `api` SQL surface, `wiki_reader` role, or HTTP/OpenAPI contract change. Findings F30, F33, F35.

**Context.** Three independent edges where typed Python met the SQL layer were enforced only at runtime, so a
divergence surfaced far from its cause. On the **write path**, the compile Load stage built each `wiki.*` row as
a bare `dict[str, object]` literal: a renamed or removed ORM column produced a silently wrong dict that the
repositories and the incremental-ingestion path then carried downstream, with nothing static to catch it (F30).
The lone exemplar of the better shape was `EdgeRow` in `domain.py` — constructed then `.model_dump()`'d — but the
remaining five row kinds had no equivalent. On the **read path**, the serve handlers (`features.serve.queries`)
translate each query message into a single call against the read-only `api` schema, reading cells off each row
by name (`row["hit_level"]`, `row["src_id"]`, …) to hydrate the frozen `features.serve.domain` models; that
cross-boundary contract — the `api.*` function parameter names and the projected OUT columns — was asserted only
when a real query ran at MCP or dashboard runtime, so a migration that renamed a param or a column failed in
production rather than in CI (F33). And on the **evals→serve** edge, the calibrated `RetrievalPolicy` artifact
reached the serve surface as an implicit `_meta/active-policy.yaml` file convention plus a `strategy` *string*
re-resolved at startup, not a typed edge: a stale policy whose strategy serve no longer supported failed deep in
MCP startup, not at a named boundary (F35). The driving constraint, confirmed with the user, was that each of the
three SQL-boundary divergences must fail at the earliest possible gate — compile-time, test-time, or
load-boundary — and that the write-path change must preserve **byte-identical** row output so the
`incremental == full` property guard stays green.

**Options considered.**

- **Leave the three edges runtime-checked, document the contracts.** Rejected. The boundaries stay advisory: a
  renamed column, a renamed `api` param, or an unsupported strategy still reaches production before failing —
  exactly the late-failure the findings flagged.
- **Model the write path but assert the read contract by reflecting the live `api.*` signatures.** Considered for
  F33. Rejected in favour of executing the real SQL constants against a migrated DB: reflection re-derives the
  contract from the same migrations under test, whereas running the handlers' own SQL and asserting an explicit
  column literal makes the guard bidirectional — a serve-side change that needs a new column also forces the test
  to be updated.
- **Typed frozen row models, a golden-query contract test, and a typed policy accessor (chosen).**

**Decision.**

Three mechanisms, one per SQL-boundary edge.

**1. Frozen write-path row models mirror the ORM columns 1:1 (F30).** `features/compile/rows.py` adds
`SectionRow`, `ClaimRow`, `LinkRefRow`, `LintRow`, and `ComparisonCellRow` — frozen pydantic models, each
mirroring one `wiki.*` table's columns and excluding the columns the row dicts never carry (the `embedding`
vector, the `bigserial` surrogate `id`, server-default timestamps), following the existing `EdgeRow` exemplar.
Each is **built then `.model_dump()`'d**, so the repositories and the incremental path receive a byte-identical
row dict to the prior literal — a renamed or removed ORM column now fails the model and pyright at the build
site instead of silently emitting a wrong dict. `CompiledPage` and `EdgeRow` stay in `domain.py` (not
relocated). `ComparisonCellRow` is defined for the typed seam but has **no Load-path builder** — the `cells`
aggregate is never populated — so it is a deliberate forward seam, not dead code.

**2. A golden-query contract test locks the serve read layer to the `api.*` surface (F33).**
`tests/integration/test_api_contract.py` (flat, matching the serve integration-test layout) executes the **real
SQL constants** the handlers run (`features.serve.queries`'s `_SEARCH_SQL`, `_FETCH_SQL`, `_LOOKUP_SQL`,
`_GRAPH_NODES_SQL`, `_GRAPH_EDGES_SQL`, `_GRAPH_EXPAND_SQL`, `_GRAPH_PATHS_SQL`) against a freshly-migrated,
empty testcontainer database. An empty DB returns zero rows but the result still carries the column names, so
each test asserts both halves of the contract: the query **executes without error** (proving the bound param
names and arg types match the live `api.*` signature) and `set(result.keys())` **equals** the exact OUT-column
set the matching serve handler consumes (encoded as an explicit literal, so the guard is bidirectional). A
migration renaming an `api` param or an OUT column now fails *here*, in CI, not at MCP/dashboard runtime. The
module auto-skips when Docker is unavailable.

**3. A typed accessor owns the evals→serve active-policy edge (F35).**
`features/evals/calibration/policy.py` adds `load_active_policy(vault_path) -> ActivePolicyResolution | None`,
which owns the `_meta/active-policy.yaml` relative path and, when the file is present, loads the
`RetrievalPolicy` and **resolves its `strategy` against the LIVE serve strategy set** (`strategy.resolve_strategy`
/ `strategy.STRATEGY_NAMES`) at load, returning a frozen `ActivePolicyResolution` that composes the registry's
own `RetrievalProfile`. A stale policy whose strategy serve no longer supports fails at the accessor boundary
with a clear error naming the supported strategies, not deep in MCP startup. `RetrievalPolicy`'s own
eval-valid-set validator is unchanged, and `cli.py:_apply_active_policy` delegates to the accessor.

**Consequences.**

- (+) **Each SQL-boundary divergence fails at the earliest gate.** A renamed ORM column fails at compile-time
  (pyright on `rows.py`), a renamed `api` param or OUT column fails at test-time (the contract test in CI), and
  an unsupported active-policy strategy fails at the load boundary (`load_active_policy`) — none reach
  production.
- (+) **Byte-identical row output is preserved.** The construct-then-`model_dump` shape reproduces the prior
  dicts value-for-value, so the `incremental == full` property guard (`test_incremental_equals_full.py`) stays
  green and the repositories see no change.
- (+) **The read contract is bidirectional and self-documenting.** The OUT-column sets are explicit literals
  derived from the handler bodies and the migrations' `RETURNS TABLE(...)`, so neither side can drift silently.
- (−) **`ComparisonCellRow` is defined without a builder,** a typed seam ahead of its consumer; this is a known,
  named forward seam (the `cells` aggregate is never populated yet), accepted so the model surface is complete
  when the builder lands.
- (−) **The write-path modelling is not exhaustive.** Pages built via `add_about`, the `sources_raw` /
  `raw_chunks` rows, and the `compile_manifest` dicts are **not yet modeled** — they stay dict-built, a named
  residual for a future slice rather than a covered surface.
- **Reversible?** Yes. The row models can be re-inlined as dict literals, the contract test deleted, and the
  accessor re-inlined into the CLI, since no schema, `api` surface, `wiki_reader` role, or HTTP contract was
  touched.

**Related.** [ADR-0021](0021-explicit-eager-composition-root.md) (the explicit composition root this builds on),
[ADR-0022](0022-evaluation-context-subpackages.md) (the evaluation sub-packages and the `serve_bridge` seam this
builds on; the active-policy accessor lives in the `calibration` sub-package it created),
[ADR-0019](0019-true-incremental-ingestion.md) (the incremental-ingestion path whose byte-identical row guard the
write-path models preserve), [ADR-0018](0018-calibration-loop.md) (the calibration loop whose winning-policy
artifact the typed accessor reads). Code: `features/compile/rows.py` (the frozen write-path row models),
`tests/integration/test_api_contract.py` (the golden-query `api.*` contract test),
`features/evals/calibration/policy.py` (`load_active_policy` / `ActivePolicyResolution`, the typed evals→serve
edge). Entry point: [CONTEXT-MAP.md](../../../CONTEXT-MAP.md). Glossary: [engram/CONTEXT.md](../../CONTEXT.md).
