---
id: engram-adr-0022-evaluation-context-subpackages
aliases: ["engram-adr-0022-evaluation-context-subpackages"]
type: adr
app: engram
status: accepted
tags: []
title: "ADR-0022 — Evaluation context sub-packages and the serve_bridge anti-corruption seam"
---

# ADR-0022 — Evaluation context sub-packages and the serve_bridge anti-corruption seam

**Status:** Accepted (2026-06-30). Delivered in the `engram-domain-remediation` initiative (Slice B, the
evaluation-slice reorganization, tasks B1 through B6). Reworks the internal layout of the evals slice first
built for structural eval generation ([ADR-0016](0016-structural-eval-generation.md)) and the calibration loop
([ADR-0018](0018-calibration-loop.md)), and consumes the explicit composition root
([ADR-0021](0021-explicit-eager-composition-root.md)) unchanged. Scope: `features/evals/` internals only — the
new `shared` / `generation` / `calibration` / `harness` / `rca` sub-packages and the
`shared/serve_bridge.py` seam. The slice's public edges (`__init__`, `commands`, `deps`, `handlers`, `http`)
stay at the package root with their import paths unchanged. No schema, `api` SQL surface, `wiki_reader` role, or
HTTP/OpenAPI contract change. Findings F37, F38, F39, F40.

**Context.** The evals slice had grown to roughly twenty-eight flat modules in a single directory that bundled
five distinct sub-contexts — the shared domain, dataset generation, the calibration loop, the run harness, and
root-cause analysis — with no structural boundary between them, so a reader could not tell which module served
which concern (F37). The slice runs its agent-under-test against the *real* MCP retrieval surface, which created
a typed Evaluation-to-Retrieval edge: three separate modules imported `features.serve.tools` directly, so any
change to serve's tool surface broke evals at import time in several places at once (F38). A legacy `QaKind`
vocabulary duplicated the structural `Archetype` label lossily — two parallel ways to say the same thing, kept
in sync by hand (F39). And `ExpectedQa.expected_claim_anchors` was a dead field: collected during generation
but dropped before it reached any consumer (F40). The driving constraint, confirmed with the user, was that a
maintainer must be able to navigate the slice by sub-context, the serve edge must have exactly one home, and the
slice's *public* edges (wireup module discovery, the CQRS bus, the FastAPI mount) must stay byte-for-byte stable
so no downstream contract churns.

**Options considered.**

- **Leave the slice flat, document the sub-contexts in a comment.** Rejected. The boundary stays advisory, a new
  module can land in any sub-context silently, and the three-site serve import keeps spreading — exactly the
  drift the findings flagged.
- **Split into sub-packages but expose a narrow, calibration-only serve bridge.** Considered for the F38 seam.
  Rejected in favour of a single comprehensive bridge (see Decision 2): a narrow bridge would have left the
  harness importing serve directly, so the "one home for the serve edge" invariant could not be asserted.
- **Internal sub-packages with stable root edges and one comprehensive `shared/serve_bridge.py` (chosen).**

**Decision.**

Four load-bearing choices carry the refactor.

**1. The slice is split into five internal sub-packages by sub-context, with the public edges held at the root.**
`shared` (domain / orm / repository), `generation` (generation / archetypes / seed_repository / datasets /
curate / pipeline / subscriber), `calibration` (calibration / proposer / objective / policy / strategy /
mcp_calibration), `harness` (harness / evaluators / runner / experiments / instrumentation / delivery), and
`rca` (rca / diagnosis). The slice's **public edges keep stable import paths at the package root** —
`features.evals.__init__`, `commands` (the CQRS command types), `deps` (the wireup DI providers), `handlers`,
and `http` (the FastAPI router). Because those paths are unchanged, wireup module discovery, the CQRS bus
routing, and the FastAPI mount see no difference.

**2. A single `shared/serve_bridge.py` is the sole evals importer of serve's tool and query surface.** It is the
only evals module that imports `features.serve.tools` (`TOOL_NAMES`, the `FETCH` / `SEARCH` / `GRAPH_EXPAND` /
`FIND_PATHS` / `RUN_CODE` tool-name constants, `build_retrieval_toolset`, `RetrievalToolset`,
`RetrievalHandlers`) and `features.serve.queries` (the `FetchHandler` / `SearchHandler` / `GraphExpandHandler` /
`GraphPathsHandler` types it wraps), re-exporting exactly the symbols evals needs. Every consumer
(`calibration.strategy`, `calibration.proposer`, `deps`, `harness.runner`, `harness.harness`) routes through it,
guarded by an AST-based architecture-invariant test that fails if any other evals module imports the serve
modules directly. This is a deliberate **comprehensive edge located in `shared/`** — over the narrower
calibration-only bridge — so the entire Evaluation-to-Retrieval dependency has one provable home. The eval
router's `features.serve.http` / `features.serve.jobs` imports are a **separate, intentional HTTP-mounting
seam** that mounts the eval routes on the serve app; that seam is deliberately *not* routed through
`serve_bridge`.

**3. The legacy `QaKind` vocabulary is removed; `Archetype` is the single structural label, kept optional.** The
`QaKind` alias, `QaCaseMeta.kind`, `_ARCHETYPE_KINDS`, and `_archetype_kind` are gone. **`archetype` stays
`Archetype | None = None` — not made required.** This consciously refines the plan's original "make required"
wording, because F39 only required removing the *legacy vocabulary*, and making `archetype` required would
regress deliberate, tested behaviour: the unlabelled (None) archetype is load-bearing — `rca/diagnosis` emits a
dedicated None bucket, `generation/curate` excludes None-archetype cases, and a back-compat test asserts the
None default. One structural vocabulary, with its existing None semantics intact.

**4. The dead `ExpectedQa.expected_claim_anchors` field is removed (F40).** `ExpectedQa` is declared
`extra="ignore"`, so any persisted or in-flight payload that still carries the key is silently dropped on
validation rather than rejected — the removal is persistence-safe.

**Consequences.**

- (+) **Maintainers navigate by sub-context.** The five sub-packages name the slice's concerns, so a reader
  lands in the right place and a new module has an obvious home.
- (+) **A refactor of serve's tool surface touches one file.** The Evaluation-to-Retrieval edge lives only in
  `shared/serve_bridge.py`, and the AST invariant test keeps it that way; the HTTP-mount seam stays separate by
  design.
- (+) **One structural vocabulary instead of two,** with a net reduction in code (the legacy `QaKind` machinery
  and the dead field are gone) and the deliberate None-archetype semantics preserved and tested.
- (+) **No downstream contract churn.** The public edges are unchanged, so there is no wireup, HTTP, or CQRS
  contract change and **no `engram-client` `gen:types` regeneration** is needed: the eval OpenAPI is unchanged
  because `QaCaseMeta` is only an internal pydantic-evals report type, converted to the wire-facing
  `EvalCaseResultRow` before it reaches the HTTP boundary.
- (−) **The slice is no longer a flat module list,** so a cross-cutting change can touch more than one
  sub-package directory. Accepted: the boundaries make each change's blast radius legible, which is the point.
- **Reversible?** Yes. The sub-packages can be flattened back, the bridge re-inlined, and the field
  re-introduced, since no schema, `api` surface, `wiki_reader` role, or HTTP contract was touched.

**Related.** [ADR-0016](0016-structural-eval-generation.md) (the structural eval generation this slice
implements, now in the `generation` sub-package), [ADR-0018](0018-calibration-loop.md) (the calibration loop,
now the `calibration` sub-package), [ADR-0021](0021-explicit-eager-composition-root.md) (the explicit
composition root whose `deps`/`commands` edges this refactor keeps stable),
[ADR-0014](0014-domain-general-vault-spec-and-plugin.md) (the domain-general spec the eval `Archetype` labels
are framed against). Code: `features/evals/shared/serve_bridge.py` (the sole serve edge),
`features/evals/shared/{domain,orm,repository}.py`, `features/evals/generation/`,
`features/evals/calibration/`, `features/evals/harness/`, `features/evals/rca/`, and the unchanged root edges
`features/evals/{__init__,commands,deps,handlers,http}.py`. Entry point:
[CONTEXT-MAP.md](../../../CONTEXT-MAP.md). Glossary: [engram/CONTEXT.md](../../CONTEXT.md).
