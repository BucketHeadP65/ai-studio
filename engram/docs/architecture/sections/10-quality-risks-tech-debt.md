<!-- GENERATED from architecture.md by scripts/docs/split_doc.py --dir engram/docs/architecture — do not edit here; edit the master and re-run the script. -->

## 10. Quality, risks, tech debt
> **TL;DR:** engram-specific risks: the hand-written SQL `api` surface drifts silently from the ORM, the dual-dialect BM25 seam and AGE-vs-CTE graph backend each risk parity drift, the bus's declarative route table needs one entry per new command (pinned by the composition-root tests), and integration/e2e coverage is Docker-gated.
<!-- load-when: risks, tech debt, known issues, caveats, coverage, drift, parity, quality -->

- **The `api` surface is hand-written SQL.** The agent-visible views + functions
  live in the alembic migration, not in code derived from `indexing/infrastructure/orm.py`, no
  type-checker covers the join between them, so schema drift is a silent
  maintenance seam.
- **Dual-dialect BM25 drift.** The `SearchDialect` seam emits two BM25 grammars
  (`pg_textsearch` for the local Postgres, `pg_search` for ParadeDB). Only the
  lexical leg differs, but the two grammars can drift unless both are exercised.
  CI defaults to the local Postgres ([ADR-0017](../../adr/0017-local-postgres-primary-testbed.md)).
  The integration tiers read as `kb_reader` on both beds, and `verify_reader_role` refuses a reader
  that can do more than read `api`, so a role bug fails the tests rather than hiding behind an owner login.
- **AGE-vs-CTE graph parity.** Graph retrieval has two backends (the relational
  `cte` backend and Apache AGE, [ADR-0007](../../adr/0007-age-multihop-dual-graph-backend.md)).
  One generated body serves both, and only the one-hop read differs, so
  `graph_expand` / `find_paths` / `graph_reach` must return the same rows on both, a parity the
  test bed has to hold.
- **Route-at-construction invariant.** The bus is fully routed the moment
  `bus_provider` builds it, from one declarative `{Command: handler}` table
  ([ADR-0021](../../adr/0021-explicit-eager-composition-root.md)). Adding a slice's
  command is one plain provider in the slice plus one entry in
  `build_command_routes`, and the composition-root contract tests pin the table.
- **`retrieval` ↔ `evals` is a two-way edge with one seam.** `evals` reaches
  retrieval only through `engram.evals.shared.serve_bridge`, an anti-corruption
  funnel a boundary test keeps one-directional. The reverse edge has no funnel:
  `retrieval/infrastructure/mcp_server.py` imports the recommendation, tuning, and dataset
  registrations from `engram.evals` **at module scope** to offer the opt-in
  authoring tool families, so importing the MCP server pulls in the whole evals
  tree even when a host wires none of those deps. The cost is import weight, not
  a runtime cycle. Deferring those imports into the registration branches would
  close it ([ADR-0039](../../adr/0039-library-app-package-split.md)).
- **Coverage shape.** Integration/e2e tiers run a real Postgres via testcontainers
  and auto-skip without Docker, so a Docker-less CI exercises only the unit tier.

The *why* behind each design point lives in the ADRs. Debt is ledgered in prose
(no inline TODO/FIXME markers).
