---
id: engram-adr-0016-structural-eval-generation
aliases: ["engram-adr-0016-structural-eval-generation"]
type: adr
app: engram
status: accepted
tags: []
title: "ADR-0016 — Structural eval-case generation (archetypes + gold-from-seed)"
---

# ADR-0016 — Structural eval-case generation (archetypes + gold-from-seed)

**Status:** Accepted (2026-06-24). Replaces the single-paper `gen-dataset` generator with a
structural one: golden QA cases are seeded from the **shape** of the compiled corpus (five
retrieval archetypes), their difficulty is **derived** from that shape rather than authored as a
label, and their gold source ids are read **from the structural seed before any model runs** (the
gold-from-seed inversion). The model writes only the question, answer, and verbatim support quotes,
and a faithfulness gate keeps only cases whose every quote is verbatim in the seed's source bodies.
Scope: a new `features/evals` cluster (`domain.py`'s `Archetype` / `derive_difficulty` /
`DifficultyThresholds` / `Seed` / `AnchorHash`, `seed_repository.py`, `archetypes.py`,
`generation.py`, `curate.py`, `pipeline.py`, the `GenerateEvalDataset` bus command), the new
`engram evals gen-dataset` flags (`src/cli_evals.py`), `EvalsSettings.generation`
(`src/settings.py`), and two opt-in MCP authoring tools (`features/serve/mcp_server.py`). The read
path, the `api` SQL surface, the `wiki_reader` role, and the scoring / experiment / RCA paths are
unchanged. This is **Phase 1** of the broader self-calibrating-retrieval program (the calibration
loop and incremental ingestion are deliberately out of scope here).

**Context.** The old `gen-dataset` drew from a single compiled paper and let the *model* invent
`expected_source_ids`, which a downstream gate then accepted or rejected. Two problems followed.
First, the difficulty axis was weak: a case's `easy` / `medium` / `hard` label was authored, not
grounded in anything structural, so it could not be trusted as a controlled variable in an
experiment. Second, a fixed corpus could not yield many datasets: one paper produced one small set,
and there was no principled way to scale dataset volume from the same compiled vault. Letting the
model pick the gold ids also inverted the trust boundary: the thing being evaluated (retrieval over
the corpus) depended on the model getting the ground truth right, then we filtered its mistakes.

The compiled corpus already carries the structure to fix both problems. `wiki.pages`, `wiki.claims`,
`wiki.comparison_cells`, `wiki.edges`, and `wiki.sections` describe a graph whose shape *is* a
retrieval task: a single claim is a pinpoint lookup, a comparison matrix is a contrast, an edge
fan-out is a survey, a multi-hop path is a trace, an out-fanning page is a synthesis. If the gold
sources are read off that structure, they are correct by construction and the model never touches
them.

**Options considered.**

- **Keep model-picked gold ids, harden the gate.** Rejected. It keeps the inverted trust boundary
  (the eval depends on the model getting ground truth right) and still has no principled difficulty
  axis. A better gate cannot manufacture a controlled variable that was never grounded.
- **Author difficulty as an LLM-assigned label on each generated case.** Rejected. The label is a
  guess, not a property of the case. Difficulty should fall out of the seed's shape (sources, hops,
  sections), so it is reproducible and comparable across runs and vaults.
- **A new `wiki.eval_cases` table for the generated datasets.** Rejected (out of scope). The eval
  read/score path already round-trips `pydantic-evals` YAML on disk
  ([ADR-0011](0011-single-source-retrieval-toolset.md) era), and Logfire is the deep store
  ([ADR-0010](0010-claude-code-event-bridge-model.md)). A new table is storage churn for no gain
  this round; datasets stay YAML.
- **Hard-code the research relation slugs into the seeders.** Rejected. The relation universe is
  per-vault. The seeders read it from the injected `VaultSchemaSpec`
  ([ADR-0014](0014-domain-general-vault-spec-and-plugin.md)), so a different vault's relations work
  unchanged.
- **Structural archetypes plus a gold-from-seed inversion (chosen).**

**Decision.**

Two ideas carry the change.

**Idea 1, structural archetypes with derived difficulty.** A generated case starts from an
`Archetype` (`domain.Archetype`), a closed set of five retrieval shapes drawn from the compiled
corpus:

- `pinpoint`, a single `^cN` claim; gold is the page owning it (one source).
- `contrast`, a comparison page's `comparison_cells` matrix; gold is the compared row entities.
- `survey`, a node that many edges of one relation point *into*; gold is the contributing source
  pages plus the hub (wide fan-out).
- `trace`, a real `src -> mid -> dst` path over `wiki.edges`; gold is the path's node ids
  (multi-hop).
- `synthesis`, a page whose outgoing edges of one relation reach many distinct pages; gold is those
  reached pages, with the source page's section count carried as `n_sections`.

Each archetype resolves through an `ArchetypeSpec` registry (`features/evals/archetypes.py`) to its
structural seeder method and a per-archetype drafting prompt, the project's closed-set
strategy-registry shape (a frozen dataclass over a `Mapping[Archetype, ArchetypeSpec]`, a default-first
`ARCHETYPE_NAMES` tuple, a `resolve_archetype` that rejects an unknown name rather than silently
defaulting), so adding a shape is a one-entry diff plus a named seeder. Difficulty is **not** a label:
`derive_difficulty(n_sources, n_hops, n_sections)` (`domain.py`) computes the band from the seed's
shape (a single source with no hop is `easy`; two-or-more hops, a wide fan-out, or a multi-source case
that also spans several sections is `hard`; everything else is `medium`). The cutoffs live in a tunable
`DifficultyThresholds` model, surfaced through `EvalsSettings.generation` so a vault can retune which
shapes count as which band.

**Idea 2, the gold-from-seed inversion.** The structural seeder (`features/evals/seed_repository.py`)
draws a `Seed` whose `gold_ids` are read off the graph *before any model runs*. The drafting agent
(`generation.PydanticAiDrafter`, a pydantic-ai `Agent`) is given only the seed's source passages and
writes a question, an answer, and one-or-more `support_quotes`; it never emits gold ids, kind, or
difficulty, which the seed fixes. The pure `accept` gate (`generation.accept`) then keeps a draft iff
every support quote is verbatim in the seed's source bodies, reusing the single home of the
faithfulness predicate, `wikinote.verify.quotes_in_mirror`. Because the gold ids come from structure
and the quotes are re-verified, a kept case is **faithful by construction** rather than faithful only
if the model behaved. The same fixed corpus now becomes a combinatorial seed space (every claim, every
comparison, every fan-out, every path, every cluster), so dataset volume scales without new content.

The pipeline (`features/evals/pipeline.py`, `GenerationPipeline.generate`) composes the two: per
requested archetype it computes a seed budget from `GenerationConfig.target_distribution` and
`total_cases`, draws seeds, drafts and gates per seed, then hands the survivors to the `CaseCurator`
(`features/evals/curate.py`) which deduplicates by question-embedding cosine and stratifies to the
target distribution within the case budget, before `build_dataset` assembles the `pydantic-evals`
`Dataset`. The CLI / dashboard reach it through the `GenerateEvalDataset` bus command, mirroring the
export slice's contract shape.

Also recorded:

- **Read-only over the owner session.** `SeedRepository` only issues `SELECT`s, but it runs over the
  primary/owner `AsyncSession` the compile slice uses, because the corpus tables carry **no**
  `wiki_reader` grant (migration 0003 walls the reader off `wiki.*`; reads go through the `api`
  `SECURITY DEFINER` functions). It takes a tight `SupportsExecute` port (one `execute` method), not
  the full session, so a test drives it with any executor satisfying the slice (Interface
  Segregation).
- **Spec-driven seeders.** The relation universe the fan-out / trace / synthesis seeders dispatch
  over is the injected `VaultSchemaSpec`'s `relations` (plus the claim grammar's
  `comparative_relations`), never hard-coded slugs ([ADR-0014](0014-domain-general-vault-spec-and-plugin.md)).
  The one slug the spec does not distinguish is the synthesis relation, so synthesis is derived
  structurally (a page fanning *out* to many distinct pages over one relation) rather than from a
  named field.
- **Both surfaces over one core.** The owned path is the `PydanticAiDrafter` CLI worker. The same
  seed + gate flow is also exposed as two **opt-in** MCP authoring tools: `propose_eval_seeds`
  (hands back structural seeds with their drafting instructions) and `submit_eval_cases` (re-runs the
  faithfulness gate server-side and appends survivors to a dataset YAML). They are gated **exactly
  like** `run_code` ([ADR-0015](0015-code-mode-on-the-mcp-retrieval-surface.md)): they register only
  when the authoring deps are wired *and* the retrieval profile names them, so the default
  `engram mcp` surface stays read-only.
- **Anchor provenance.** Each seed (and each generated case) carries an `AnchorHash` per gold page
  (the page id plus its `body_hash`), so a later staleness check can tell whether the source moved
  under the case. The hash is baked in now; the staleness detector itself is future work.
- **No new table.** Datasets stay `pydantic-evals` YAML on disk (the local source of truth), mirrored
  to the hosted Logfire datasets API on `--push` exactly as before.

**Consequences.**

- (+) **A real difficulty axis.** Difficulty is a reproducible function of the seed's structural
  shape, not an authored guess, so it is a trustworthy controlled variable in an experiment and is
  comparable across runs and across vaults.
- (+) **Faithful by construction.** Gold ids come from the graph (never the model) and every quote is
  re-verified verbatim, so a kept case cannot carry a fabricated citation or an unsupported quote;
  the trust boundary is no longer inverted.
- (+) **Scalable dataset volume.** A fixed corpus is now a combinatorial seed space across five
  archetypes, so many balanced datasets come from the same compiled vault with no new content.
- (+) **Domain-general and drift-free.** The seeders read the vault's relations from its
  `VaultSchemaSpec` ([ADR-0014](0014-domain-general-vault-spec-and-plugin.md)), the faithfulness check
  is the single `quotes_in_mirror` predicate, and the CLI worker and the MCP tools share the one seed
  + gate core, so a change lands in both places at once.
- (+) **The foundation for the broader program.** Structural ground-truth generation is Phase 1 of
  the self-calibrating-retrieval roadmap; the derived-difficulty axis and the anchor provenance are
  the seams later phases (calibration, staleness, incremental ingestion) build on.
- (−) **Dedup is embedding-similarity only.** The curator drops near-duplicate questions by cosine
  over their embeddings; it does not reason about semantic overlap beyond that single signal, so two
  differently-worded questions over the same fact can both survive.
- (−) **Bridge-as-brain generation is the CLI worker flow.** Driving the bridge drafter is the
  CLI path; the MCP authoring tools are the alternative surface and are off by default.
- **CLI `--authoring` MCP enablement (landed 2026-06-24, this feature's Phase 3 refine).** Originally
  deferred; now wired as a first-class opt-in flag, `engram mcp --authoring`, which supplies the
  seed-repository factory + `evals.datasets_dir` + difficulty thresholds and adds the
  `propose_eval_seeds` token to the profile, so a serving session exposes the authoring tools while a
  bare `engram mcp` stays read-only (D8). Two hardenings shipped with it: `submit_eval_cases`
  validates the `dataset` name to a single safe path segment (no traversal outside `datasets_dir`) and
  `propose_eval_seeds` caps `count` at a server-side maximum.
- (−) **The MCP authoring gate trusts the client-echoed seed.** Over MCP the seed round-trips through
  the client between `propose_eval_seeds` and `submit_eval_cases`, so the server-side `accept` gate
  verifies quotes against the echoed `source_bodies`: a fabricated seed can pass (integrity-only and
  low-risk under the opt-in + local-stdio posture, since gold ids still come from the seed and the
  worst outcome is a self-authored case in a local YAML the operator reviews). If the surface is ever
  promoted past opt-in, the fix is to persist proposed seeds server-side keyed by `seed_key` and
  re-resolve `source_bodies` / `gold_ids` on submit rather than trusting the echoed seed.
- **Reversible?** Yes, additively. The `features/evals` generation cluster, the new `gen-dataset`
  flags, `EvalsSettings.generation`, and the two MCP authoring tools are removable independently;
  reverting leaves the scoring, experiment, and RCA paths and the read surface intact.

**Related.**
[ADR-0011](0011-single-source-retrieval-toolset.md) (the single-source retrieval surface the eval
agent scores; unchanged here), [ADR-0012](0012-retrieval-strategy-and-code-mode-axes.md) (the
retrieval-strategy + code-mode experiment axes the generated datasets feed),
[ADR-0014](0014-domain-general-vault-spec-and-plugin.md) (the `VaultSchemaSpec` whose relations drive
the seeders), [ADR-0015](0015-code-mode-on-the-mcp-retrieval-surface.md) (the opt-in `run_code`
gating the eval-authoring MCP tools mirror). Code: `features/evals/domain.py` (`Archetype`,
`derive_difficulty`, `DifficultyThresholds`, `Seed`, `AnchorHash`), `features/evals/seed_repository.py`
(the five structural seeders), `features/evals/archetypes.py` (the archetype registry),
`features/evals/generation.py` (`SeedCaseDraft`, `accept`, `PydanticAiDrafter`),
`features/evals/curate.py` (`CaseCurator`), `features/evals/pipeline.py` (`GenerationConfig`,
`GenerationPipeline`), `src/settings.py` (`GenerationSettings` / `EvalsSettings.generation`),
`src/cli_evals.py` (the `gen-dataset` flags), `features/serve/mcp_server.py`
(`propose_eval_seeds` / `submit_eval_cases`). Patterns:
[Strategy registry](../../../docs/explanation/design-patterns.md#strategy-registry) (the archetype
registry's shape), [CQRS message bus](../../../docs/explanation/design-patterns.md#cqrs-message-bus)
(the `GenerateEvalDataset` command).
