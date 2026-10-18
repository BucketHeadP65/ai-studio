---
id: q.llm-judged-eval-validity
type: question
title: How well do LLM-judged win rates track factual quality in RAG comparisons?
aliases: [LLM judge validity]
summary: "Most graph-RAG sensemaking claims rest on LLM-judged win rates, often with the judge also generating answers. Quantified: judge/claim agreement 69–78%; win-rate→F1 inverts orderings; pointwise vs pairwise disagree; the expert-anchored benchmark says rubric grading fails to scale."
status: draft
created: 2026-06-10
updated: 2026-06-10
question_status: open
answered_by:
tasks: [sensemaking-qa, fact-verification]
stages: [evaluation]
---

## TL;DR

If [[llm-judged-win-rate]] rewards comprehensiveness/diversity-shaped answers, and graph methods produce exactly that shape, are the reported family-level wins partly metric artifact? And how biased is a protocol where the judge model is also every system's generator?

## Why it matters

Our playbook recommendations for "when graph-RAG over chunk-RAG" currently inherit this metric wholesale ([[lightrag-2025#^c3]], [[lightrag-2025#^c10]]). If rankings shift under factual-accuracy metrics, the applicability conditions in future playbooks must narrow to preference-style sensemaking tasks.

## Current evidence

- [[lightrag-2025#^c10]] — single small model (GPT-4o-mini) as generator and judge; no groundedness metric reported.
- [[lightrag-2025#^c2]] — the one near-tie (Mix) shows the margin can vanish by domain even under the favorable metric.
- [[memgraphrag-2026#^c4]] (+ ^c5) — **the strongest evidence yet that the question bites**: under gold-anchored accuracy (answer vs gold), vanilla top-5 RAG ties LightRAG and beats MS-GraphRAG — the win-rate-based family-level conclusion does not survive the protocol change.
- [[memgraphrag-2026#^c14]] — a middle-path protocol exists: LLM-judged but gold-anchored (GPT-4o-mini scoring answer-vs-gold at temperature 0), avoiding pairwise-preference shape bias while keeping judging cheap; the judge is still the generator, though.
- [[simgrag-2024#^c1]] — the KGQA corner avoids the problem entirely (answers are KG entities; Hits@1 is judge-free) — evidence that metric trouble is task-shape-dependent, not universal.
- [[graphrag-2024#^c10]] — **the first quantitative validity anchor**: on the same answers, GPT-4-turbo judge labels agree with claim-count metrics on only 78% (comprehensiveness) / 69–70% (diversity) of non-tie comparisons. Same paper, same protocol concern: the judge is also every system's generator.
- [[hipporag2-2025#^c5]] — **the inversion realized**: LightRAG's LLM-judged wins over GraphRAG ([[lightrag-2025#^c1]]) become a 6.6-vs-49.6 avg-F1 loss under a unified extractive harness — and LightRAG falls below BM25 *and* no-retrieval. Partly answer-format mismatch (default config, short-phrase rephrasing), which is itself the point: win-rate protocols never surface format fragility. Now formally disputed by [[hirag-2025#^c12]] (43.20 F1 for the same system/dataset under a sibling harness) — reproduction variance compounds the protocol problem.
- [[leanrag-2025#^c2]] vs [[leanrag-2025#^c3]] — **pointwise and pairwise disagree inside one paper**: HiRAG ties LeanRAG on pointwise 1–10 scores (CS/Agriculture) yet loses every pairwise subset. Same systems, same answers, same judge family — the protocol *shape* alone decides the ordering.
- [[leanrag-2025#^c13]] — generator-judge identity at its purest: DeepSeek-V3 generates every system's answers and scores them, five times per query.
- [[hirag-2025#^c15]] — **the first judge-robustness check**: HiRAG's Overall win on Mix survives swapping GPT-4o for Claude-3.5-Sonnet and Qwen-turbo (one dimension-cell flips). Validity evidence FOR the protocol, in the right direction for once.
- [[hmem-2026#^c6]] (+ ^c2) — **F1 and LLM-judged accuracy pick different winners within one harness**: on REALTALK Temporal, EverMemOS wins judged accuracy by 6.3 points while H-Mem wins F1 — the metric pair the agent-memory field treats as interchangeable is not.
- [[msrs-2025#^c8]] — reasoning models score higher G-EVAL while scoring *lower* ROUGE-2/BERTScore; authors attribute the lexical drop to paraphrase. Judged and lexical metrics diverge systematically with model class, not just system pair.
- [[gsw-2025#^c14]] — the fully LLM-mediated endpoint: synthetic LLM-generated corpus, LLM-extracted answers, LLM judge — no human anchor anywhere in the loop.
- [[cuprate-expert-eval-2025#^c13]] — **the expert-anchored counterpoint and its cost**: blinded world-expert rubric grading of 67 questions is the validity gold standard, and the authors state it does not scale to other fields or newer model generations. The published rubric + questions are a reusable anchor for calibrating LLM judges.
- [[agents-k1-2026#^c4]] (+ ^c1, ^c5) — **every headline is LLM-judged**: the multi-hop SOTA edge over HippoRAG2/GFM-RAG lives in GPT-4o-mini GPT-Judge Accuracy (baselines tie it on surface Containment, ^c5), and the FrontierScience/geoscience lifts are rubric/GPT-5.2-judged with no human calibration — partly a floor artifact (bare Gemini-3 = 0.0% physics, ^c1). A clean within-table instance of the judge-metric-vs-surface-metric gap.

## Candidate answers

- Re-evaluate flagship comparisons with (a) a different judge family, (b) citation-groundedness checks, (c) factoid subsets with ground truth. Look for papers doing exactly this during future ingests (benchmark/analysis `paper_type`).
