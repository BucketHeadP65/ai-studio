---
id: engram-adr-0013-design-and-vision-docs-in-repo
aliases: ["engram-adr-0013-design-and-vision-docs-in-repo"]
type: adr
app: engram
status: accepted
tags: [docs]
title: "ADR-0013 — The design & vision docs live in the repo, as engram explanation docs"
---

# ADR-0013 — The design & vision docs live in the repo, as engram explanation docs

**Status:** Accepted (2026-06-20). Brings engram's **product vision, solution design, and
design-decision rationale** into the repository as governed `explanation` docs under
`engram/docs/explanation/`, and removes them from the Knowledge vault. Extends — does not
replace — the docs-as-code standard ([root ADR-0005](../../../docs/adr/0005-docs-as-code-standard.md)).
Scope: documentation only; no code changes.

**Context.** engram's deepest thinking — *why the system exists* ("compile a corpus once into
distilled, provenance-pinned, contradiction-aware knowledge"), the solution design (the 3-layer
wiki, the seven page types, claims as the atomic unit, compilation, retrieval), the eval-loop
design, the OKF interchange design, the literature that grounds it, and the product decision log
(**D-1…D-24**) — lived in the **Knowledge vault** at `~/Desktop/knowledge/_meta/design/`. That is
the wrong home twice over: (1) the vault is the **research corpus engram compiles**, so design docs
about engram are not research and risk being ingested as if they were; and (2) the vision sat
*divorced from the code that realizes it* — the repo's `docs/` owned the engineering story (arc42-lite
architecture, slice references, ADRs 0001–0012) but never the product vision. The repo did not "own
the vision." Meanwhile the repo already had a mature docs standard (ADR-0005: Diátaxis + arc42-lite +
split-doc + `CONTEXT-MAP.md` + stdlib validators) with a natural home for this material — the
Diátaxis **explanation** quadrant — that was simply unused for it.

**Options considered.**

- **Leave it in the vault, link from the repo.** Rejected: keeps engram's design inside the compiled
  research corpus (ingestion hazard) and split across two trees; the repo still doesn't own its vision.
- **Copy into the repo (duplicate).** Rejected: two sources of truth drift; the user's explicit
  constraint was "no duplication — the Knowledge vault is just the [research] docs, nothing else."
- **Invent a new top-level `design/` doc family.** Rejected: it is a non-Diátaxis quadrant the charter
  and validators would have to learn, for content that is already exactly "explanation" (understanding-
  oriented: why it is this way). Standardizing means *using* the existing standard, not growing it.
- **Migrate into `engram/docs/explanation/` as governed explanation docs, curate-on-move, delete the
  vault originals (chosen).**

**Decision.**

- **Home: `engram/docs/explanation/`** (federated under the app, since the vision *is* the engram
  product). The migrated family:
  - `vision.md` — the grand vision + solution design (from the vault `architecture/` master): the
    compile-vs-retrieve thesis, the three layers, page types, claims, registries, disputes, ingest,
    curation, compilation, retrieval, generalization, the roadmap.
  - `eval-loop.md` — the eval-loop design, reconciled against the shipped evals
    ([ADR-0011](0011-single-source-retrieval-toolset.md)/[0012](0012-retrieval-strategy-and-code-mode-axes.md)).
  - `okf.md` — the OKF interchange design, reconciled against the shipped exporter ([ADR-0009](0009-okf-export-interchange.md)).
  - `product-decisions.md` — the **D-1…D-24** decision log, preserved append-only, with a cross-
    reference table mapping each product decision to the engineering ADR that implements it.
  - `research/01–05` — the literature briefs that grounded the decisions.
- **Two altitudes of decision, kept distinct.** The **product** decision log (D-n: schema, vocabulary,
  curation policy, retrieval doctrine) stays one register; the **engineering** ADRs (0001–0013: how the
  code realizes them) stay the per-decision record. `product-decisions.md` cross-links D-n → ADR rather
  than flattening 24 product decisions into ADRs (different altitude, different audience).
- **Curate-on-move, not a verbatim dump.** Each doc is reconciled against the **as-built** system as it
  lands: design-era "will / planned / candidate" that has since shipped is moved to present tense and
  cross-linked to the implementing ADR / [reference doc](../reference/evals.md); genuinely unbuilt
  items (multi-collection federation, the usage/query-log loop, MOC clustering, the OKF importer,
  incremental recompilation) are marked **roadmap**; vault `[[wikilinks]]` become repo-relative links or
  plain text. The author's prose and voice are preserved — this is curation, not a rewrite.
- **Governed like all docs.** Every file carries the ADR-0005 frontmatter (`id`/`type:
  explanation`/`app: engram`/`status`), is registered in `CONTEXT-MAP.md`, and passes
  `scripts/docs/validate_docs.py`. These are **single-file** explanation docs (the standard's
  precedent — `tech-stack.md`, `design-patterns.md`); they keep their per-section `TL;DR` + `load-when`
  lines as in-document navigation. Split-doc (`scripts/docs/split_doc.py`) remains reserved for arc42
  architecture *masters*; a doc that outgrows a single file can graduate to `engram/docs/architecture/`.
- **The vault keeps only its research corpus.** After the repo side is verified, the migrated
  `_meta/design/` originals are removed from the vault — no duplication. The same principle retired two
  more vault files that were *about engram*, not research: `_meta/ingest-staging/` (a leftover
  ingest manifest — exhaust per D-21, the paper was already merged) and
  `_meta/compilation/postgres-target.md` (the old compilation contract — superseded by the alembic
  migrations, which now own the schema, plus the [index](../reference/indexing.md) /
  [serve](../reference/serve.md) reference docs and ADR-0006; its ~16 source-docstring citations were
  repointed at those repo-owned docs). The vault's *authoring* contracts that engram still reads at
  compile time (`_meta/schema.md`, `_meta/registry/*`, `_meta/purpose.md`, `_meta/retrieval-policy.md`,
  `_meta/dashboard.md`) plus its templates **stay** — the vault knows only the contracts it must
  follow, nothing about engram's internals; whether the wikinote grammar (which engram's `wikinote/`
  kernel already owns per D-22) also graduates into the repo is a deliberate later question.

**Consequences.**

- (+) **The repo owns its vision.** The why, the solution design, and the decision rationale sit beside
  the code and ADRs that implement them, retrievable through the one `CONTEXT-MAP.md` entry point.
- (+) **The vault is clean.** Only the research corpus remains, so the compiler ingests research, not
  engram's self-documentation.
- (+) **No drift.** One source of truth per doc; product decisions cross-link to engineering ADRs, so a
  reader moves from "why we decided X" to "how X is built" in one hop.
- (−) **Curation is judgment work.** Reconciling design-era prose against as-built reality is manual and
  must be redone whenever the design and the code diverge again; the cross-reference table needs an
  update when a new ADR implements an older product decision.
- (−) **Two decision registers to keep coherent.** D-n and the ADRs can fall out of sync; the
  cross-reference table is the seam that must be maintained.
- **Reversible?** Yes — documentation only; the files can move again (their `id`s are stable join keys),
  and nothing in the code depends on their location.

**Related.** [root ADR-0005](../../../docs/adr/0005-docs-as-code-standard.md) (the standard this
extends), [ADR-0009](0009-okf-export-interchange.md) /
[0011](0011-single-source-retrieval-toolset.md) / [0012](0012-retrieval-strategy-and-code-mode-axes.md)
(the engineering decisions the migrated design docs are reconciled against). Migrated docs:
`engram/docs/explanation/{vision,eval-loop,okf,product-decisions}.md` + `research/`.
