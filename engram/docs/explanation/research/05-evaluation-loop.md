---
id: engram-research-05-evaluation-loop
aliases: ["engram-research-05-evaluation-loop"]
type: explanation
app: engram
status: current
tags: [research]
title: "Evaluation loop, deep research"
---

# Evaluation loop, deep research

*Research notes that grounded engram's [eval loop](../eval-loop.md) and [product decisions](../product-decisions.md).*

> **Scope.** This is the **research** artifact (phase 1 of 4: research to workflow design to implementation design to implement). It captures best-practice findings and grounds them in the knowledge base's actual code and its own content. It does **not** design the implementation. It realizes the decisions *Open/deferred* item: *"eval harness before retrieval tuning (build when the indexer lands)."*
>
> **Method.** Knowledge-base-first, externally grounded (the approved sourcing). Six parallel Opus probes: three mined the knowledge base itself (eval metrics & judge validity, retrieval & schema, the optimization/self-improvement thread), two surveyed external practice (the `pydantic-evals`/`pydantic-ai`/`logfire` stack, RAG-eval methodology + synthetic generation), one read the `engram/engram` source to pin the three surfaces as concrete knobs. Citations: knowledge base notes as note / note#^cN, ADRs as **D-n** in decisions, design docs by section, engram code as `path:line`, external work by arXiv id or docs URL (unlinked, not in the knowledge base).

---

## Top findings (the eight that shape the design)

1. **Our claim grammar is the field's recommended synthetic-data method, already done.** "Know Your RAG" (arXiv 2411.19710) shows naive question-generation collapses to ~95 % trivial single-fact items and that question-first "evolution" accumulates hallucination. Its fix is to **invert** (build *statements* from context, then questions those statements answer). The knowledge base's **block-anchored claims with verbatim `quote::` ARE those statements** (D-2, D-3), and they give a *finer* gold target than a chunk: the exact unit id `paper:slug#^cN`. We get groundedness-by-construction for free.
2. **Keep retrieval-side and answer-side scoring separate. Their disagreement is the diagnostic**, independently asserted by the knowledge base (graphrag-bench) and the whole external triad literature (TruLens/ARES/RAGAS). This is the backbone of the "diagnose why" loop, and it validates the locked *both-co-equal* ground-truth choice.
3. **Feedback richness is the lever, capture raw trajectories, never scalars.** meta-harness-2026#^c6: full-trace access scores **50.0/56.7** vs scores-only **34.6/41.3** vs scores+summary **34.9/38.7** ("summaries may *hurt*"). Three knowledge base papers converge (gepa-2025 µ_f, ace-2025, meta-harness-2026). This is the non-negotiable mandate behind the Logfire trajectory store.
4. **The config surface (S2) is the right thing to auto-optimize. Instructions/schema are not**, and the knowledge base's own content proves it. The boundary is *(object by feedback by regime)*, not the optimizer (when-does-optimization-beat-hand-design). Numeric config has the *exploitable structure* that optimization needs (prompt-opt-coinflip-2026#^c8), while the gain demonstrably does **not** live in the prose/instruction surface (ahe-2026#^c9, the system-prompt is the only component variant that *regresses*). Directly endorses the locked graduated-autonomy ladder.
5. **The real retrieval code has high-leverage, untouched dials.** `api.search` fuses with **RRF, constant `60` hardcoded, and *no* BM25-vs-vector weighting** (`alembic/versions/0002_api_surface.py:442-450`). `hnsw.ef_search` is **never set** (default 40, the single biggest untouched ANN dial). The ParadeDB tokenizer is default (no stemming/stopwords). There is **no reranker and no post-retrieval step at all**. Only `search_k`, `hybrid`, `collection` are runtime-tunable (`src/features/serve/prefs.py:42-44`). The knowledge base says exactly these are the levers that move quality.
6. **Difficulty, not configuration, dominates score variance.** prompt-opt-coinflip-2026#^c3: question difficulty explains **19-91 %** of variance vs **≤0.77 %** for any prompt effect. Consequence: the margin-gate must be sized against *measured* case-variance (bootstrap/paired CI on a frozen held-out split), or the loop will "optimize" noise.
7. **Don't trust LLM-judge win-rates as ground truth.** hipporag2-2025#^c5: a system that *won* on LLM-judged win-rate collapsed to **6.6 F1, below the no-retrieval baseline (38.4)** once measured against gold. Use **gold-anchored pointwise** judging (we have a reference answer), temp 0, published rubric, agent-under-test not the judge, validated against a small human tuning set.
8. **Two measurement traps must be designed in from day one:** (a) **equalize the retrieval budget across arms**, many published graph-vs-chunk wins compare top-60 against top-5 (hypergraphrag-2025#^c2). (b) **measure our own knowledge base's graph density** before trusting any graph-structure result, standard benchmarks (HotpotQA ~0.65, UltraDomain ~0.86 edges/node) are too sparse to test the claim (graph-rag-survey-2025#^c9).

---

## Section 0, constraints locked during grilling

The six decisions taken in the `/grill-with-docs` session, plus what was explicitly deferred. These are inputs to design mode, not yet ADRs.

| # | Decision | Locked choice |
|---|---|---|
| 1 | **Ground truth** | **Both, co-equal.** Each case carries a gold unit id *and* a reference answer. Retrieval metrics *and* an LLM-judge run together. Their **disagreement is the primary diagnostic**. |
| 2 | **Harness** | **Two rigs.** A **mechanical probe** (fixed query, then call `api.search` directly, sweep config, isolates S2, deterministic) and an **agentic rig** (a real `pydantic-ai` agent uses the knowledge base's MCP server, full trajectory traced, isolates S1, exercises answer-correctness). Both span knowledge base versions (S3). |
| 3 | **Autonomy** | **Graduated by surface.** **Config** auto-applies behind a margin-gate + rollback. **Instructions** return as a proposed diff for human approval. **Schema/ingestion** is diagnose-only (a finding/ADR, never a silent rewrite). |
| 4 | **Dataset** | **Golden core + frontier.** A small, curated, human-approved, hash-stamped golden set (the regression bar + cross-version yardstick) plus a **gated auto-expanding candidate pool** fed on each refresh (Phase 5) or manual run. Promotion is gated. |
| 5 | **Artifacts** | **Derivative-doctrine split.** Eval **code** + **datasets** live in `engram/engram/` (datasets hash-stamped, lock-step with retrieval code, knowledge base = source of truth, derivatives don't pollute it). The **generate/diagnose skill** is knowledge-base-local in `.claude/skills/` beside `ingesting-papers`. |
| 6 | **Sourcing** | **Knowledge-base-first, externally grounded** (this document). |

**Deferred to design mode (not decided here):** loop cadence/trigger, the exact metric set + thresholds, the judge model + rubric, which model the diagnostic agent runs as and where it runs, the on-disk dataset format specifics, reconciling the `pydantic-ai` version bump that adding `pydantic-evals` forces.

**The three surfaces** (the user's framing, made concrete in section 4): **S1** = the retrieval *instructions* an agent sees, **S2** = the retrieval *config* (mechanical hyperparameters), **S3** = the *schema + ingestion/chunking/graph layering*.

---

## Section 1, synthetic eval-set generation

### 1.1 Claim-anchored generation (the central insight)

The field's hard-won lesson is that generating question-answer pairs straight from a knowledge base fails in two predictable ways, and the knowledge base's existing design pre-empts both:

- **Naive single-prompt generation gives ~95 % trivial single-fact questions** (arXiv 2411.19710). *Fix:* stratify by question type and generate each deliberately.
- **Question-first "evolution" (RAGAS v0.1 style) accumulates hallucination** and un-grounds the Q&A from its source. *Fix:* invert, derive **statements** from the context, then generate questions those statements uniquely answer.

> **The knowledge base already produces those statements.** A claim bullet (`^cN`) carries a verbatim `quote::` and a `loc::` into an immutable `Sources/` mirror (D-2, D-3), pinned to a stable retrieval unit id. Generating a question *from a claim* yields a case whose gold target is unambiguous and whose answer is grounded by construction, the recommended method, for free, at a finer grain than a chunk.

### 1.2 Question-type taxonomy (stratify generation)

Adopt a small, explicit taxonomy and generate to quota per type (don't let the generator default to trivia):

| Type | Source in the knowledge base | Gold target |
|---|---|---|
| **fact_single** | one claim's `quote::` | that claim unit id |
| **comparative** | a claim with an `outperforms`/`comparable_to` edge (D-4 guarantees dataset+metric qualifiers) | the claim id (+ the two entity pages) |
| **multi-hop / reasoning** | a path over typed edges (`builds_on`, `supports`, `refutes`) | **the set of claim ids on the path** |
| **summary / synthesis** | a section (H2 unit, D-7) or a `q.*`/`moc` page | the section/page id |
| **unanswerable / negative** | a plausible question with no supporting claim | ∅ (must return "not found") |

**Multi-hop gold via "dependency irreducibility"** (AgenticRAGTracer, arXiv 2602.19127): a case is genuinely multi-hop iff removing *any one* supporting claim makes it unanswerable. Our typed edge graph (D-13) lets us construct and verify this, and it produces the gold *set* that `all-recall@k` (section 2) needs.

### 1.3 The gold-target "hit" definition (tie it to the level ladder)

Because the knowledge base is indexed at four grains sharing one index with a `level` column (D-9, RAPTOR-collapsed-tree pattern, `pages/sections/claims/raw_chunks`), "did retrieval surface the gold?" needs an explicit hit rule. Proposal for design mode: a case stores its gold **claim** id. A retrieval counts as a hit if it returns that claim **or** the parent section/page that contains it (`section_path @> loc_path`, the ltree provenance backbone, `indexing/...` per A6), scored at progressively lower credit. This makes the metric robust to the legitimate "right neighbourhood, wrong grain" case rather than penalising it as a miss.

### 1.4 Gating: generate large, then filter hard, then promote (golden vs frontier)

The field converges on over-generating then filtering hard. This *is* our golden/frontier split (locked #4). The filters to adopt:

- **Round-trip retrievability filter (ARES, arXiv 2311.09476), judge-free, exact for us:** discard a generated case unless our *own* retriever surfaces its gold unit id in top-k. Doubles as a **difficulty signal** (cases that barely pass are the hard, valuable ones). **Caveat (Goodhart, A5):** the round-trip filter can silently discard your *hardest currently-failing* cases, so the golden core must deliberately **retain some unfiltered hard cases**, or the eval will flatter the system.
- **Self-containment / leakage critique ("Stand-Alone" check):** reject questions that trivially echo their passage.
- **Answerability + groundedness critiques** (RAGalyst, arXiv 2511.04502): measured to lift answerability from **0.418 to 0.994**. The residual #1 failure even after filtering is **over-specificity (71.3 %)**, so specifically critique for over-specific questions.
- **Unambiguous-target check:** reject if a different unit answers equally well (protects the retrieval metric).

**Promotion to golden** requires: passes all critiques + quote re-verified against the mirror (reuse `engram note --verify-quotes` machinery, D-22) + deduped (embedding similarity) + a human approval. Frontier candidates run in CI for signal but **never move the regression bar**.

### 1.5 Triggers

Generation runs (a) automatically at **ingest Phase 5** (the `ingesting-papers` close step, new claims give new candidate cases, mirroring D-17's "compounding at the close" shape) and (b) on **manual prompt** (the eval skill). Both feed the gated candidate pool, never golden directly.

---

## Section 2, metrics and the two-layer disagreement diagnostic

### 2.1 Separate the layers (validated from both sides)

graphrag-bench (knowledge base) and the TruLens RAG-triad (Context Relevance, then Groundedness, then Answer Relevance) both insist on splitting "the right evidence was retrieved" from "the LLM answered well." The **two-by-two of (retrieval ok?) by (answer ok?)** is the routing primitive:

| | answer correct | answer wrong |
|---|---|---|
| **gold retrieved** | ✅ pass | **generation / instruction problem** (S1 / generator), *or* a fooled judge |
| **gold missed** | **parametric-memory leak** (the model knew it anyway, so validate judge, check S1 abandonment) | **retrieval problem** (S2 config / S3 schema) |

The bottom-left cell (answer right despite a retrieval miss) is the one **answer-only evals cannot see**. Our gold-claim anchor exposes it (A5). The top-right cell is where the **win-rate-to-F1 inversion** (hipporag2-2025#^c5) lives: never let a judged win override a gold miss.

### 2.2 Retrieval-side metrics (per harness)

The knowledge base documents a precise ladder, each metric catching a different failure:

| Metric | Catches | Citation |
|---|---|---|
| **recall@k** | "did *any* gold unit make top-k" (coarse) | recall-at-k |
| **nDCG@k** | ranking/ordering quality, **but under-predicts synthesis**, never gate on it alone | ndcg |
| **all-recall@k** | **complete** multi-hop, a single missing hop fails the case. **recall@k hides this** (the HippoRAG↔ColBERTv2 gap grows 20 to 38 pts under AR vs R) | all-recall |
| **evidence-recall** | answer-grounded sufficiency (maps onto our claim units) | evidence-recall |
| **fixed-budget recall** | unranked, variable-size retrieved sets, the regime a multi-step agent produces, where ranked metrics don't apply | fixed-budget-recall |

**Adopt:** mechanical probe uses **nDCG@k + all-recall@k** (ranking + partial-multi-hop), with **evidence-recall** as the answer-grounded bridge. Agentic rig uses **fixed-budget recall** (+ trajectory metrics, section 3). **Match k per case to the gold-unit count**, don't fix it globally. Our claim-anchored gold makes all of these **deterministic, no LLM relevance-judge needed** (cheaper, less noisy than RAGAS-style judged context-precision).

**Always report recall and relevance as a pair.** The core expansion failure is recall↑ / relevance↓, memgraphrag-2026#^c2: graph retrieval hit **84.3 % recall but 38.5 % relevance** vs flat RAG's 71.8 % / 62.9 %. A single recall number hides over-expansion (recall-relevance-tradeoff).

### 2.3 Answer-side metric

**Gold-anchored pointwise LLM-accuracy**, with a published rubric, named judge, temp 0, CoT, *not* free pairwise win-rate. We have a reference answer, so anchor to it (memgraphrag-2026#^c14). Pointwise + gold-anchor sidesteps the preference-shape bias that pairwise win-rates inherit (llm-judged-win-rate). (External corroboration: style bias now dominates position bias on modern judges, 0.76-0.92 vs ≤0.04, so guard formatting. CoT + a tuned rubric are the two complementary safe mitigations, "Judging the Judges", arXiv 2604.23178.)

### 2.4 Judge validity (gate the judge)

Treat an unvalidated judge as **~70-80 % reliable at best**, graphrag-2024#^c10: the aggregated LLM label matched the claim-based label in 78 % (comprehensiveness) / 69-70 % (diversity) of comparisons. Validation recipe (synthesized from llm-judged-eval-validity + memory-eval-commensurability + cuprate-expert-eval-2025):

1. anchor to gold/reference. 2. citation-groundedness check. 3. **judge-family swap** (a verdict that flips across model families is untrustworthy, hirag-2025#^c15). 4. report agreement vs a non-LLM proxy as a %. 5. fixed named judge + published rubric + temp 0. 6. keep a small **human-expert tuning set** as the gold anchor (the validity ceiling, though it "does not scale to other fields or newer models", cuprate-expert-eval-2025#^c13). **Hard rule:** in the agentic rig the **agent-under-test must not be the judge model** (generator=judge contamination is the field's #1 anti-pattern). Gate: if judge↔retrieval-metric agreement on the golden core drops below ~75-80 %, investigate before trusting the judge half.

### 2.5 Commensurability (stamp every row)

memory-eval-commensurability: *"until the harness is pinned, a leaderboard reports configurations, not capability."* Every eval row must be stamped with the full config: embedder, fusion weights, `k`, `rrf_k`, `ef_search`, named judge + rubric, token/latency budget, and the **knowledge base/source hash**. The hash-stamped golden core supplies the fixed benchmark. The config-stamp supplies commensurability **across sweep points** (essential for S2). Never copy a baseline number across harnesses, re-execute it.

### 2.6 Retrieval-error taxonomy to surface routing

The knowledge base names the error types. This table is the failure-to-surface classifier the diagnostic step uses (A1):

| Error | Definition (citation) | Surface(s) |
|---|---|---|
| **retrieval-error** | wrong/partial/poorly-ranked passages (retrieval-error) | **S2** (ranking), **S3** (gold not a retrievable unit) |
| **retrieval-noise** | irrelevant content interfering with reasoning (retrieval-noise) | **S2** (k too high), **S3** (noisy ingest), **S1** (instruct to filter) |
| **recall-relevance tradeoff** | expansion floods context (recall-relevance-tradeoff, memgraphrag-2026#^c2) | **S3** (over-expansion), **S2** (expansion k) |
| **retrieval-reasoning imbalance** | agent offloads reasoning to trivial queries or abandons retrieval (retrieval-reasoning-imbalance) | **S1** (query policy) |
| **partial multi-hop** | some-but-not-all gold hops (only `all-recall` exposes it) | **S3** (missing edge), **S2** (k too small) |
| **conceptual-link miss** | surface-similarity misses conceptually-linked docs (cuprate-expert-eval-2025#^c6) | **S3** (graph layer), **S2** (vector↔BM25 weight) |
| **temporal/supersession blindness** | cites superseded claims (cuprate-expert-eval-2025#^c7) | **S3** (`superseded_by`, D-5), **S1** (check revisions) |
| **inaccurate citation** | answer supported by unrelated refs (cuprate-expert-eval-2025#^c11) | **S1** (grounding, diagnosable via our `loc::`/`quote::`) |
| **format/answer-shape fragility** | collapse under format mismatch, invisible to preference metrics (hipporag2-2025#^c5) | **S1** (output format) |

---

## Section 3, agentic retrieval & trajectory tracing (what to log)

### 3.1 The mandate: raw trajectories, not scalars

The strongest single result in the optimization thread (A3) is that compressed feedback lands the loop in the weak regime: full-trace **50.0** vs scores-only **34.6** vs scores+summary **34.9** (meta-harness-2026#^c6). Summaries "remove the signal needed to trace downstream failures to earlier decisions." gepa-2025#^c10 (per-hop µ_f traces for credit assignment) and ace-2025#^c7 (degrades *below baseline* without reliable feedback) corroborate. **The loop must persist the raw per-query trajectory and must not pre-summarize before diagnosis.** The metric-vs-judge disagreement (section 2.1) is only the *scalar* shadow of the signal. The *rich* version ("gold claim retrieved at rank 7 but the generator only saw top-5" vs "gold retrieved but a contradicting chunk outranked it") is what routes a finding to S1/S2/S3.

### 3.2 What to capture per case (the trajectory record)

Per retrieval (mechanical) and per step (agentic): the query as issued, whether `vector_query` (semantic) or lexical `query` was used, which of the **8 RRF legs** fired (BM25/vector by card/section/claim/raw, A6), the ranked candidate ids + per-leg ranks + fused score, **gold_rank**, what the generator actually saw (the top-k passed on), the answer, the judge verdict + rationale. For the agentic rig additionally: the full `run_sql` tool-call sequence with arguments and returned rows, **C-Steps vs I-Steps** (steps taken vs target hop-count, AgenticRAGTracer), **marginal gold-claim recall per call** (distinguishes productive iteration from redundant loops, the field has no standard query-reformulation metric, and our gold claim chain gives this proxy).

### 3.3 How (tooling, A4)

- **Trajectory from `pydantic-ai`:** `result.all_messages()`, then walk `ToolCallPart` (normalize args with `.args_as_dict()`) and `ToolReturnPart`, correlate call↔return by `tool_call_id`. The MCP `run_sql` tool is wired as a toolset (`MCPServerStreamableHTTP`/`MCPServerStdio`, note `MCPServer*` to `MCPToolset` migration before v2). A `process_tool_call` hook can intercept every SQL call at the boundary. Cap loops with `UsageLimits(tool_calls_limit=…)`.
- **Tracing:** `Agent(instrument=True)` + `logfire.instrument_pydantic_ai()`. **`include_content=True` is mandatory** or tool args/results are stripped from telemetry. Auto-instrumentation emits an agent-run span + per-tool spans carrying `gen_ai.tool.name`, the SQL args, and the returned rows. **Pin the instrumentation `version`** (attribute names rename at v3).
- **Eval↔trace bridge:** `pydantic-evals` `ReportCase` carries `trace_id`/`span_id` **per case**, the join key the diagnostic step uses to pull the raw trajectory for any failing case (or stamp `case_id` as OTel baggage).
- **Determinism:** `agent.override(model=FunctionModel(...)|TestModel(...))` + `capture_run_messages()` + `ALLOW_MODEL_REQUESTS=False` lets the *same* golden-core dataset run live (real model, real signal) and as a fast regression test (scripted model) in CI.

### 3.4 Alignment with the existing telemetry design

The knowledge base already designs a runtime telemetry channel: **D-17** (`api.log_session()` to `kb.query_log`, "telemetry is exhaust, knowledge is synthesis", exempt from rebuild-from-Markdown) and **D-20** (the audit loop: flag sessions over 2 times budget, retune the retrieval policy). The eval-loop's Logfire trajectory store is the **design-time analog** of that **runtime** audit loop, same "exhaust, not knowledge" stance, same downstream consumer (retrieval-policy tuning). Design mode should decide whether they share a shape or stay parallel.

---

## Section 4, the three surfaces, grounded in real knobs

For each surface: the **actual** mechanism (A6, with `path:line`), what the **knowledge base** says moves quality vs what's folklore (A1/A2), and how the **loop** touches it (the locked ladder, justified by A3's *object by feedback by regime* verdict).

### S1, retrieval instructions (propose-only)

**Actual surface (A6).** Three places an agent gets told how to retrieve. (1) The two MCP tool descriptions in code: `describe_schema` ("Call this FIRST … retrieval ladder L0-L4") and `run_sql` (teaches the `vector_query => '<text>'` shim, `api.lookup_value` grounding, read-only) at `src/features/serve/mcp_server.py:66-88`. (2) The **M-Schema**, per-object/column `COMMENT`s surfaced via `describe_schema` (`0002_api_surface.py:528-684`), carrying the ladder + facet-grounding + exemplar queries (D-11). (3) **`api.about`, prose authored in the *knowledge base*** (`_meta/{schema,purpose,retrieval-policy}.md` to `meta.about`), the L0 orientation. **Gaps found:** no guidance on choosing `level`, no facet-filtered exemplar, no warning that facets silently drop `raw_chunks` (see S2), no multi-step retrieval few-shot.

**Knowledge base verdict (A2/A3).** This is the **coin-flip surface**, instruction/prompt search is prompt-opt-coinflip-2026's exact target (≈ random at realistic budgets), and the gain demonstrably does **not** live in the prose surface (ahe-2026#^c9, the only component variant that *regresses*). And the real bottleneck is **discovery, not SQL syntax**: curation took an agent from 21 % to 95 % task success. Stale docs dropped it to 65 % (research/04, 04-agent-sql-discovery).

**Loop role.** **Propose-only** is strongly corroborated. Crucially, **D-16 makes S1 a tunable *knowledge base page*, not code**, retrieval-policy indexes into `api.about`, so the loop edits *doctrine and exemplars* (drafted, supersedable, linted), not a hardcoded prompt. Apply ace-2025's itemized-delta discipline: accumulate domain heuristics as bullets, **never compress** (the brevity-collapse guard, section 5).

### S2, retrieval config (auto-apply, gated)

**Actual knobs (A6, `alembic/versions/0002_api_surface.py`, `src/features/serve/prefs.py`).**

| Knob | Current state | Tunable where |
|---|---|---|
| Fusion | **RRF, constant `60` hardcoded, NO BM25-vs-vector weighting**, every leg contributes `1/(60+rank)` equally (`0002:442-450`) | baked in migration |
| Legs | **8 materialized** (BM25 + vector by card/section/claim/raw). **No per-leg candidate cap** before final `LIMIT k` (`0002:379-431`) | baked |
| ANN | **HNSW, cosine, halfvec(1024). All pgvector defaults, `hnsw.ef_search` NEVER set (= 40)** (`0001:253-256`) | baked, **biggest untouched dial** |
| BM25 | default ParadeDB tokenizer, **no stemming/stopwords/field-boosts** (`0001:226-250`) | baked |
| `k` | default 12, hard-cap 50 (D-20) | **runtime** (`prefs.py:42`) |
| `hybrid`, `collection` | on / "research" | **runtime** (`prefs.py:43-44`) |
| Facets | `level/task/stage/domain/collection` pre-filter (`0002:364-372`). `raw_chunks` are correctly excluded by `task/stage/domain` (raw spans carry no page-facets) and are **not** dropped by `collection` alone (`:373-378`), verified, not a bug | per-query |
| Post-retrieval | **none, no rerank, no MMR, no dedup, no query expansion** | absent |

**Knowledge base verdict (A2).** Evidence-backed levers: **BM25-vs-dense weighting is knowledge-base-conditioned, not a fixed prior** (so the *missing* weighting is a real gap, not a simplification). **Reranking is the sharpest "spend-budget-here" lever but only on a weak first stage** (+11.29 nDCG on BM25, zero-to-negative on strong dense), and we have *no* reranker. **top-k saturates ~60**. Folklore (tune only with local A/B): **RRF vs weighted fusion and the exact `rrf_k`** have *no ablation anywhere in the knowledge base*, and we hardcode `60`. "iterate always helps" is contested. A third retriever can *hurt* via context confusion.

**Loop role.** **Auto-apply, margin-gated + rollback** is the well-supported choice (numeric config = the *exploitable structure* optimization needs, prompt-opt-coinflip-2026#^c8). First experiments the eval should quantify: `ef_search`, `k`, **adding weighting to the RRF**, **adding a gated reranker**, and the tokenizer. **Bake in the two measurement traps:** equalize retrieval budget across arms, measure our knowledge base's own edge density first.

### S3, schema + ingestion / chunking / layering (diagnose-only)

**Actual mechanism (A6).** Four embedded units (pages/sections/claims/raw_chunks). **Embedding is breadcrumb-prefixed** (`indexing/card.py:77-140`): a page embeds a synthetic `card_text`, a section embeds `"{page_title} > {heading_path}\n\n{body}"` (the "free contextual retrieval" of D-7). Knowledge base pages use **structure-based chunking** (H2 = whole ≤600-tok unit, D-7), good. But **raw-mirror chunking is heading-based with NO overlap and NO size cap** (`indexing/knowledge_base_reader.py`), a giant heading body becomes one vector truncated at the 8 000-token embed limit. Empty-bodied heading rows are emitted **deliberately** (they are L4 verification spans, the `section_path @> loc_path` join needs the row to exist for a cited `§N.M`). Only the *breadcrumb-only embedding* of an empty body is waste, fixed by a scoped embed-stage skip rather than a chunker change. `api.sections.tokens` is advertised but never populated (`api.raw_chunks` exposes no `tokens` at all), a design-semantics gap (should it count the body, or the breadcrumb-prefixed embed input?), not a one-liner. The **ltree structure** (`section_path @> loc_path`) is the L4 claim-to-raw-span provenance backbone. Graph is **derived** from typed links (D-13). Trust is **computed live** (`api.scoreboard`, D-3).

**Knowledge base verdict (A2/A1).** Graph/hierarchy buys **recall (~+5 R@5) and token economy, NOT answer content** (graph-free map-reduce matches GraphRAG on claim quality, graphrag-2024#^c8). **Pays off** for multi-hop / summarization / at-scale / n-ary facts. **Does not pay** for fact-lookup (graph **5.67 %** context relevance vs flat RAG **64.73 %** on a medical L1 task, graph-rag-survey-2025). Named failure modes that *are S3 findings*: **claim split across chunks** (600-tok chunks extract ~2 times the entities of 2 400-tok, graphrag-2024#^c12), **disconnected subgraphs return empty multi-hop paths** (gnn-rag-2024#^c9), **semantic islands / local-global gap** in hierarchies, **conceptual-link miss to graph layer**, **supersession must be modeled** (we have `superseded_by`), **knowledge base curation is the single biggest quality lever** (cuprate-expert-eval-2025, the closest analog to our knowledge base).

**Loop role.** **Diagnose-only**, the riskiest, most non-additive, baseline-invalidating surface. The loop emits findings/ADRs (e.g. "this multi-hop cluster fails for lack of an edge", "claims split across raw chunks", "empty-body raw chunks waste embeddings") for human design. It does **not** re-chunk, re-embed, or rewrite the schema autonomously.

---

## Section 5, self-evolving loop architecture + anti-patterns

### 5.1 The loop

```
generate (claim-anchored, gated)
   → run  (mechanical probe ‖ agentic rig)
      → trace (raw trajectories → Logfire; never scalars)
         → diagnose (reflective agent reads trajectories,
                     routes each failure to S1 / S2 / S3 via §2.6,
                     emits a falsifiable change-manifest)
            → act, by surface:
                 S2 config  → eval-gate on FROZEN held-out → auto-apply + rollback
                 S1 instr.  → propose a retrieval-policy diff (human approves)
                 S3 schema  → finding / ADR (human designs)
               → re-eval (verify the manifest's predictions)
```

### 5.2 Patterns to borrow (A3)

- **Propose, then eval, then accept-by-margin + rollback**, endorsed everywhere (gepa-2025, self-improving-harness regression-gated acceptance). This *is* the locked config design.
- **Pareto frontier, multi-objective**, keep candidates on a frontier of (recall/nDCG by latency/token-cost by judge-quality). Never collapse to one metric. Greedy/single-metric loses 6-11 % (gepa-2025#^c4).
- **Reflective credit assignment over traces**, the diagnoser is a critic reading raw trajectories. **Separate the diagnoser from the change-author** (ACE's Reflector≠Curator, removing the Reflector cost 59.4 to 55.1, ace-2025).
- **Falsifiable change-manifest**, every change ships a predicted-fix / at-risk-regression set verified next round, revertible at file granularity (ahe-2026#^c6). Converts self-justification into a measured contract.
- **Itemized delta updates + deterministic merge** for the instruction surface (anti-brevity, fits a knowledge base naturally).

### 5.3 Anti-patterns (each with its guard)

1. **Eval-overfit / Goodhart.** Iterative optimizers show train-test gaps up to **+5.6 pts**, non-iterative show none (prompt-opt-coinflip-2026#^c9). meta-harness ran search and final eval on the *same* benchmark. **Amplified here** because our eval is generated from the same knowledge base *and* drives the loop (A5). **Guard:** a **frozen held-out golden split the loop never optimizes against**. The margin must clear on held-out, **whole-set**. Prefer non-iterative generate-and-rank for config at small case-counts. Tripwire = diagnosed-gain ≫ held-out-gain. Keep a human anchor + periodic refresh. **Retain unfiltered hard cases** (the round-trip filter hides them).
2. **Brevity-bias collapse.** ace-2025#^c8: 18 282 tokens @ 66.7 becomes **122 tokens @ 57.1** in one compression step. **Guard:** lives on propose-only S1 (human catches it). Proposer accumulates itemized deltas. Flag net-deletions of domain guidance.
3. **Perishable optima / model instability.** gepa-2025#^c9 (a variant that helps one model degrades another under identical hyperparameters). prompt-opt-coinflip-2026#^c10 (which task is optimizable *reverses* with the executor model, 6/6 to 1/6). **Guard:** pin (retrieval by generator by judge) in the gate. Invalidate cached wins on any model change. Rollback as the net.
4. **Coin-flip noise floor.** Difficulty explains **19-91 %** of variance vs **≤0.77 %** for config (prompt-opt-coinflip-2026#^c3). **Guard, the margin-gate's reason to exist:** it *is* the headroom test productionized (best-of-N must beat baseline by a real margin on held-out, prompt-opt-coinflip-2026#^c14). **Size the margin against measured case-variance** (bootstrap/paired CI), never a fixed number. Run a cheap headroom check before spending search budget.
5. **Judge validity** (section 2.4), inversion, generator=judge contamination. **Guard:** gold-anchored pointwise. Agent ≠ judge. Human tuning set. Treat disagreement as *investigate*, never *average away*.
6. **Self-attribution is regression-blind.** Self-improving harnesses predict their *fixes* ~5 times random but their *regressions* only ~2 times random (ahe-2026#^c10). **Guard:** gate across the whole held-out set, auto-rollback on any net regression, don't trust the loop's own regression forecast.

---

## Section 6, observability & failure-clustering

- **Store:** Logfire. The `records` table (Postgres/ParadeDB dialect) exposes `trace_id, span_id, parent_span_id, span_name, attributes (JSON), duration, level, is_exception, …`. Query programmatically with `LogfireQueryClient.query_json_rows(sql, …)` (needs a separate **read token**), JSON attrs via `attributes->>'gen_ai.tool.name'`, **self-join parent↔child** (`c.trace_id=p.trace_id AND c.parent_span_id=p.span_id`) to attribute a failing `run_sql` span to its agent run.
- **Clustering for "main pain points":** pull every case where `gold_rank > k` or the judge failed, and cluster by **(error-type by surface by facet by question-type by question-difficulty)**. Because difficulty dominates variance (section 5 #4), **stratify every aggregate by difficulty**, an unstratified mean will mislead.
- **In-eval trajectory assertions:** `pydantic-evals` `SpanTree`/`SpanQuery` + the built-in `HasMatchingSpan` let an evaluator assert on the trajectory *inside* the run (e.g. "did the agent ever filter before ranking", a D-9 standing rule).
- **The output of diagnosis** is not a number, it's a ranked set of S1/S2/S3 findings, each a falsifiable change-manifest (section 5.2), feeding the act step and (for S1) retrieval-policy / (for S3) a design ADR.

---

## Section 7, open questions carried to design mode

1. **Cadence/trigger of the loop**, post-ingest only, scheduled, or manual? (Generation trigger is settled, both. The *diagnosis/act* cadence is not.)
2. **Where the diagnostic agent runs**, the knowledge-base-local skill, a `wiki` CLI subcommand, or both, and which model.
3. **Exact metric thresholds + the margin size**, must be derived from measured variance on the first golden core, not guessed now.
4. **Judge model + rubric**, and the size/source of the human tuning set.
5. **Dataset format**, `pydantic-evals` `to_file` YAML is the candidate, but byte-stability isn't promised. The hash should cover `model_dump(mode="json")` + the `pydantic-evals` version, not raw bytes.
6. **Dependency reconciliation**, adding `pydantic-evals` force-bumps `pydantic-ai-slim` to its lockstep version. Check against the repo's `pydantic-ai>=1.73.0`.
7. **Reuse vs parallel** of the existing test harness, A6 found a working `KnowledgeBaseBuilder -> index -> search -> assert` testcontainers seam (`tests/conftest.py`, `tests/support/knowledge_base_builder.py`), but `FakeEmbedding` is hash-noise (useless for vector quality) and there are no labels/metrics/eval-deps yet. Decide what to reuse.
8. **The eval↔runtime-telemetry relationship** (section 3.4), shared shape with D-17's `query_log`, or parallel.

---

## Sources & method

- **Knowledge base (primary).** Metrics/judge: llm-judged-eval-validity, memory-eval-commensurability, graphrag-bench, recall-at-k, ndcg, all-recall, evidence-recall, fixed-budget-recall, g-eval, llm-judged-win-rate, cuprate-expert-eval-2025, retrieval-error, retrieval-noise, recall-relevance-tradeoff, retrieval-reasoning-imbalance. Retrieval/schema: graph-rag, graph-rag-architectures, when-does-graph-structure-pay-off, when-to-skip-retrieval, graphrag-2024, hypergraphrag-2025, memgraphrag-2026, graph-rag-survey-2025, gnn-rag-2024, hipporag2-2025, hirag-2025, leanrag-2025, plus 02-hierarchical-retrieval, 04-agent-sql-discovery and architecture sections 11/12. Optimization/loop: when-does-optimization-beat-hand-design, gepa-2025, ace-2025, meta-harness-2026, prompt-opt-coinflip-2026, ahe-2026, self-improving-harness, brevity-bias, filesystem-feedback-channel.
- **Knowledge base source (grounding).** `engram/engram`: `alembic/versions/0001_*`, `0002_api_surface.py`, `src/features/serve/{mcp_server.py,prefs.py}`, `src/src/engram/indexing/{card.py,kb_reader.py,repository.py}`, `tests/{conftest.py,support/knowledge_base_builder.py}`.
- **External (grounding).** `pydantic-evals`/`pydantic-ai` 1.107.0, `logfire` ~4.37 (docs at pydantic.dev/docs). Methodology: "Know Your RAG" (arXiv 2411.19710), ARES (2311.09476), RAGalyst (2511.04502), AgenticRAGTracer (2602.19127), "Judging the Judges" (2604.23178). RAGAS / TruLens / DeepEval frameworks.
- **ADRs honored.** D-2/D-3 (claims + provenance), D-5 (supersession), D-7/D-9 (units + collapsed multi-level), D-11/D-16/D-17/D-20 (agent surface, living policy, telemetry-exhaust, CE budgets), D-13 (derived graph), D-22 (programmatic note construction).
