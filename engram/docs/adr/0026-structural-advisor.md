---
id: engram-adr-0026-structural-advisor
aliases: ["engram-adr-0026-structural-advisor"]
type: adr
app: engram
status: accepted
tags: []
title: "ADR-0026 — The structural advisor (Vibe RAG Phase 4)"
---

# ADR-0026 — The structural advisor (Vibe RAG Phase 4)

**Status:** Accepted (2026-07-03). Built on `feat/vibe-rag-phase4-structural-advisor` (Vibe RAG
**Phase 4**, the S3 edit surface). Extends the [ADR-0018](0018-calibration-loop.md) calibration
core (Phase 2, the S1 controller) with the advisor the eval-loop design deferred: S3 — schema +
ingestion/chunking/graph layering — was "a diagnostic finding / ADR for human design … NEVER
auto-applied" ([eval-loop §9](../explanation/eval-loop.md)). Scores against the
[ADR-0016](0016-structural-eval-generation.md) judge-free structural ground-truths and attributes
post-apply drift through the [ADR-0019](0019-true-incremental-ingestion.md) compile manifest.
Scope: the new `features/evals/advisor/` subpackage (`advisory.py`, `detectors.py`, `gateway.py`,
`proposer.py`, `gate.py`, `adr.py`, `apply.py`, `verify.py`, `mcp_advisor.py`), the
`AdviseStructure` / `VerifyAdvisory` bus commands with handlers and DI, the `engram evals advise` /
`apply-spec` / `verify-advisory` CLI verbs, `EvalsSettings.advisor`, and three opt-in MCP authoring
tools. The read path, the `api` SQL surface, and the generation/calibration paths are unchanged. No
new DB tables.

**Context.** The calibration loop fixed what instructions and strategy could fix; its residual is
structural. But S3 is a different control problem from S1, because a `vaultspec.json` diff is
**inert**: a new relation has zero edges and a new page type zero pages until vault *content* is
migrated (authorial work, product decision D-7), and a `canonical_sections` change restructures no
existing note. "Apply the spec diff" and "the corpus change that moves the eval baseline" are
separate events, possibly days apart. Consequences: an honest pre-apply recall score is impossible
without simulating the migration; applying the spec file itself is safe to mechanize; verification
is necessarily a post-migration protocol; and because eval scoring is **page-level** (gold page ids
survive section restructures and edge additions), the same retained dataset re-run post-migration is
an apples-to-apples delta.

**Decision.** One advisory artifact, typed where the signal is crisp and free-form where it is not,
delivered propose-only over two brains and verified mechanically after the human migrates:

- **v1 edit vocabulary** — two typed, spec-valid op classes: `restructure_sections` (the
  chunking-granularity edit) and `add_relation` (the graph-layering edit), a discriminated union
  whose validity is by construction (`apply_op` re-validates the rebuilt spec through
  `VaultSchemaSpec`'s own invariants, from a plain `model_dump()` because
  `revalidate_instances="never"` would skip them). Everything else — new page types, compound
  "layer" designs, machinery ideas — is a named, ranked **findings tail**, never a diff op.
- **Evidence, not scores** — proposals are ranked by measured evidence (bucket recall deficit,
  over-budget gold sections read off `wiki.sections` with a read-time token count when the stored
  `tokens` is NULL, unlifted links mined from `wiki.link_refs`, and a **counterfactual edge
  injection**: the reachability delta from re-running the recursive path CTE over
  `wiki.edges ∪ candidate_edges`) and carry **falsifiable predictions** plus a per-page **migration
  brief**. There is no pre-apply eval score.
- **Shadow mechanical-split simulation (`--simulate`)** — the restructure class's optional measured
  upgrade. The plan's scratch-mini-vault compile proved structurally impossible (`wiki.sections` is
  one row per H2/H3 *leaf*, so an over-budget row has no H3 left to split, and a mini-vault's tiny
  competitor set makes cross-collection ranks incomparable), so the pre-authorised fallback shipped:
  each over-budget gold section is split at paragraph boundaries under the token budget (the
  compiler's own `cl100k_base` counting) and the whole body plus every part is rank-scored against
  the live collection's sections in one bound-parameter `ts_rank_cd(..., 1)` pass (a
  log-length-normalised lexical proxy for the serving BM25 leg). The measured before/after would-be
  ranks land on the proposal's evidence. Compile-free and judge-free, so the "no advisor verb
  compiles" constraint holds even under `--simulate`.
- **Deterministic detectors, one drafting LLM** — pure section-split / missing-edge /
  over-expansion detectors read the diagnosis and the corpus (via tight SQL ports over the owner
  session); the LLM appears only as the proposer, and the pure **gate** recomposes all evidence
  mechanically from the diagnosis, never trusting the draft's claims (judge-free, the same
  inversion as ADR-0016).
- **Residual-first** — the advisor prefers a baseline run under the calibrated policy
  (`_meta/active-policy.yaml` or `--policy`), grading its evidence `residual-of-calibrated`; a raw
  baseline is accepted but graded lower-confidence.
- **Three verbs, propose-only** — `evals advise` writes the `StructuralAdvisory` YAML plus a
  MADR-shaped **draft ADR** sibling for the human gate; `evals apply-spec` folds selected proposals
  onto `vaultspec.json` in the working tree only (idempotent, never compiles/migrates/commits) and
  prints the follow-up protocol; `evals verify-advisory` re-runs the retained page-level dataset,
  judges every prediction, and attributes drift via the manifest's `compiled_at` against the
  advisory's corpus stamp — content changed outside the migration briefs is a loud unattributed
  drift warning.
- **Two brains, one gate** — the CLI's pydantic-ai proposer and Claude Code over
  `engram mcp --authoring` (`diagnose_structure`, `what_if_edges`, `submit_advisory`); the submit
  tool is an integrity trust boundary (`model_validate` + `apply_op` re-validation + a safe-name
  guard) regardless of client.

**Options considered and rejected.** *LLM-judged proposal scoring* — reintroduces the judge
inversion Phase 1 removed; the LLM's opinion of a schema edit is not a measurement. *Pre-apply
sandbox scoring as the core signal* — requires simulating the authorial migration, which is the
expensive part; `--simulate` ships only as a rank-plausibility signal on the restructure class,
never an accepted-iff-better gate. *Scratch-mini-vault shadow compile* — the plan's primary
`--simulate` mechanism, rejected at build time for the rank-over-SQL fallback (leaf sections have
no H3 to split; a mini corpus is not a comparable competitor set; and it would have made `advise`
compile). *Auto-apply* — forbidden by design: an S3 edit invalidates the baseline a margin-gate
depends on, and a one-corpus signal must not rewrite an ontology unreviewed.

**Consequences.** (+) A principled human gate over a validated, evidence-ranked artifact — the
draft ADR literalizes eval-loop §9's "ADR for human design". (+) Reversible additively: the two op
classes extend without touching the orchestrating handler; the findings tail names what the grammar
cannot yet type. (+) Verification closes the loop mechanically — predictions get measured, not
trusted. (−) Chunking evidence without `--simulate` stays correlational, and even the shadow split
is a lexical would-be rank, not an eval score — the post-migration verify remains the only real
measurement. (−) `verify-advisory`
depends on operator discipline (migration ⊆ briefs); the drift warning detects, but cannot prevent,
uncontrolled corpus change, and a full rebuild degrades path attribution to a loud caveat. (−) An
`explicit`-policy baseline cannot be reproduced exactly at verify time (the artifact records the
source, not the path), so verify substitutes the active policy with a note.
