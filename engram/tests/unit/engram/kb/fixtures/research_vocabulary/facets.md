---
id: meta.vocabulary-facets
type: meta
title: "Vocabulary: facet keys & controlled values"
summary: The structured filter vocabulary — flat list-typed frontmatter keys that index to typed Postgres columns. Values are open (add rows); keys need an ADR.
status: verified
created: 2026-06-10
updated: 2026-06-10
---

# Vocabulary: facets

Facets are the **noise-reduction surface**: agents shrink the search space with `WHERE` clauses on facets *before* any text/vector search runs. Facets are flat frontmatter keys (Obsidian-friendly), list-typed unless noted, values are lowercase-hyphen slugs. `tags` remain free-form and carry no contract.

**Rules:** new *value* = add a row here (cheap, do it at ingest when nothing fits). New *key* = ADR in [[decisions]] (it changes the Postgres schema). Never encode a facet in prose only.

## `tasks` — on paper, entity, comparison, playbook, question

What problem formulation it concerns. Seed: `open-domain-qa`, `multi-hop-qa`, `long-doc-qa`, `summarization`, `fact-verification`, `code-generation`, `agentic-search`, `recommendation`, `dialogue`, `sensemaking-qa` (global corpus-understanding questions, GraphRAG-eval style; added at lightrag ingest 2026-06-10), `kgqa` (QA over an existing knowledge graph where answers are KG entities, e.g. MetaQA/WebQSP; added at simgrag ingest 2026-06-10), `domain-qa` (QA over a closed professional/vertical corpus — e-government, e-health, enterprise docs — where answers must respect domain rules; added at kag ingest 2026-06-10), `reading-comprehension` (extractive/short-answer QA over a provided passage or small fixed corpus, SQuAD-style; added at cag ingest 2026-06-10), `episodic-qa` (QA over events anchored in space and time across long narratives, with recurring-event disambiguation, EpBench-style; added at gsw ingest 2026-06-10), `narrative-qa` (QA over long fiction where answers depend on plot progression and evolving character relations, NarrativeQA/∞BENCH/DetectiveQA-style — narrower than `long-doc-qa`; added at comorag ingest 2026-06-10), `scientific-reasoning` (expert-level scientific problem solving combining deep domain knowledge with multi-step reasoning, HLE/SuperGPQA-style — answers derived, not extracted; added at eigen1 ingest 2026-06-10), `long-term-memory-qa` (QA over an agent's accumulated multi-session interaction history, evidence dispersed across sessions and time, LoCoMo/LongMemEval-style; added at hmem ingest 2026-06-10), `grid-world-navigation` (sparse-reward goal-reaching navigation in procedurally configured grid worlds, MiniGrid-style; added at coral ingest 2026-06-10), `continuous-control` (locomotion/manipulation RL with continuous state-action spaces, MuJoCo/D4RL-style; added at coral ingest 2026-06-10), `open-domain-mds` (open-domain multi-document summarization — retrieve query-relevant documents from a large corpus, synthesize a long-form summary, MSRS-style; added at msrs ingest 2026-06-10), `response-evaluation` (scoring/critiquing LLM responses against rubrics, HelpSteer2/Feedback-Bench-style; added at prompt-opt-coinflip ingest 2026-06-10), `instruction-following` (open-ended instruction following on real-user queries, WildBench-style; added at prompt-opt-coinflip ingest 2026-06-10), `agentic-task-execution` (end-to-end agent execution of artifact-producing tasks using tools/skills, scored by deterministic verifiers, SkillsBench-style; added at skillrae ingest 2026-06-10), `math-reasoning` (mathematical problem solving, MATH500/Minerva-style, where retrieval, if any, targets definitions/theorems; added at ur2 ingest 2026-06-10), `embodied-agent-task` (long-horizon decision-making in interactive embodied/simulated environments — text or physics household/science worlds — where an agent issues executable actions over many turns, scored by deterministic binary task success, ALFWorld/VirtualHome/ScienceWorld-style; distinct from `agentic-task-execution` (artifact-producing, SkillsBench-style) and from `grid-world-navigation`/`continuous-control` low-level control; added at hipif ingest 2026-06-14), `text-classification` (assigning inputs to a fixed label set; here the ONLINE variant — an LLM receives labeled examples one at a time, updates memory, and is scored on a held-out test set, LawBench/Symptom2Disease/GoEmotions/Banking77-style; the dominant task of harness/context-engineering optimization papers; added at meta-harness ingest 2026-06-14).

## `domains` — on paper, playbook

Corpus/application domain of the evidence. Seed: `encyclopedic`, `legal`, `biomed`, `finance`, `code`, `scientific`, `enterprise`, `mixed`, `government` (civic-administration corpora, e.g. e-government service documents; added at kag ingest 2026-06-10), `literary` (fiction/biography/philosophy corpora — the domain where GraphRAG-vs-LightRAG ordering flips; added at memgraphrag ingest 2026-06-10), `conversational` (personal multi-session dialogue corpora — agent-user or human-human logs; added at hmem ingest 2026-06-10), `simulated-control` (simulated RL environments — grid worlds, physics locomotion — rather than document corpora; added at coral ingest 2026-06-10).

## `stages` — on entity (techniques), comparison, playbook

Pipeline stage a technique lives in (generic key; seeded for RAG). Seed: `indexing`, `chunking`, `embedding`, `query-transformation`, `retrieval`, `reranking`, `generation`, `evaluation`, `orchestration`, `memory`, `training` (training-time policy optimization of RAG/agent components — RL fine-tuning, verifiable-reward training; added at rexrag ingest 2026-06-10), `prompt-optimization` (offline tuning/search over component prompts in an LLM pipeline; added at prompt-opt-coinflip ingest 2026-06-10), `context-compilation` (post-retrieval organization of retrieved evidence into the context artifact handed to the generator; added at skillrae ingest 2026-06-10), `planning` (agent-side decomposition of a high-level goal into subgoals / a plan that constrains subsequent action generation — hierarchical planning, as opposed to query-transformation; added at hipif ingest 2026-06-14), `harness-optimization` (optimizing the agent harness itself — the non-parametric scaffolding of prompts, tools, runtime control, recovery — via proposal+validation loops, as opposed to prompt-optimization or context-compilation; added at self-harness ingest 2026-06-14).

## `modalities` — on paper, entity, playbook

Seed: `text`, `code`, `tables`, `images`, `graph`, `multimodal`.

## `scale_regimes` — on playbook, comparison

Corpus size band guidance applies to (**closed**, defined):

| Value | Definition |
|---|---|
| `toy` | < 1k documents |
| `small` | 1k – 100k documents |
| `large` | 100k – 10M documents |
| `web-scale` | > 10M documents |

## `paper_type` — on paper (single value)

`method` · `survey` · `benchmark` · `analysis` · `position` · `engineering-report`

## `kind` — on claim blocks (single value)

Evidence kind, ordered roughly by weight:

| Value | Meaning |
|---|---|
| `benchmark-result` | Quantitative result on a named dataset/metric |
| `ablation` | Component-removal evidence |
| `replication` | Independent reproduction of someone else's claim |
| `negative-result` | No-effect / failure finding (first-class — pruning depends on these) |
| `theoretical` | Proof or complexity argument |
| `qualitative` | Case study, error analysis, examples |
| `author-claim` | Asserted by authors without direct evidence shown |
| `observation` | Our own synthesis-time observation (must say so) |

## `maturity` — on playbook (single value)

`experimental` · `emerging` · `battle-tested` · `deprecated`

## `eval_setting` — claim qualifier (single value)

`zero-shot` · `few-shot` · `fine-tuned`
