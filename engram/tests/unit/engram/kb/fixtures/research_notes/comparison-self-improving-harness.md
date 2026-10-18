---
id: cmp.self-improving-harness
type: comparison
title: "Self-improving harnesses — who optimizes, what gets edited, and where the gain lives"
aliases: [harness evolution comparison, self-improving harness, harness optimization comparison]
summary: "Four ways to improve a fixed model's scaffolding — Self-Harness (self-edit), AHE (full-component evolution), Meta-Harness (external code search), ACE (prompt playbook). Shared finding: the gain lives in tools/middleware/memory, not the prompt."
status: draft
created: 2026-06-14
updated: 2026-06-14
compares: ["[[self-harness]]", "[[ahe]]", "[[meta-harness]]", "[[ace]]"]
dimensions: [who-optimizes, edit-surface, feedback-channel, terminal-bench-2, cross-model-transfer, key-finding]
synthesizes: ["[[self-harness-2026]]", "[[ahe-2026]]", "[[meta-harness-2026]]", "[[ace-2025]]", "[[terminal-bench-2026]]"]
tasks: [agentic-task-execution, text-classification]
stages: [harness-optimization]
---

## TL;DR

Four 2025–2026 systems all improve a fixed model's [[agent-harness]] without touching weights, and they split on three axes: **who optimizes** (the agent itself — [[self-harness]], [[ahe]] — vs an external searcher — [[meta-harness]]), **what is edited** (the full component set — AHE — vs bounded single surfaces — Self-Harness — vs prompt-borne playbook only — [[ace]]), and **how feedback is consumed** (falsifiable change-manifest vs weakness-mining+regression-gate vs raw-history filesystem vs reflective playbook). The strongest convergent result: the gain lives in **tools/middleware/long-term memory, not the system prompt** — AHE's ablation and Self-Harness's "no generic prompt padding" agree, and prompt-only ACE is *beaten on the harness benchmarks* (68.9% under AHE's seed). Caveat up front: the Terminal-Bench-2 numbers below are **not cross-comparable** (different task subset, attempt count, and metric per paper).

## Scope

In: methods that optimize the non-parametric scaffolding ([[agent-harness]]) of a fixed model. Core rows: [[self-harness]] (Weakness-Mining → Proposal → regression-gated Validation, three base models), [[ahe]] (observability-driven joint evolution of all [[nexau]] components, one fixed model), [[meta-harness]] (external outer-loop search over harness code via a [[filesystem-feedback-channel]], frozen model), and [[ace]] (evolving in-context playbook — a *context*-only harness, included because both AHE and Meta-Harness use it as the self-evolving baseline they beat). Out: model-side RL/fine-tuning (the [[hierarchical-rl-credit-assignment]] cluster), and pure prompt-optimization framing (see [[prompt-optimization]]). **Commensurability warning:** [[terminal-bench-2]] appears here under three regimes — AHE (full 89 tasks, [[pass-at-1]] mean-over-k-rollouts), Self-Harness (64-task subset, Pass% over 2 attempts), Meta-Harness ([[pass-rate]] on Opus 4.6 / Haiku 4.5) — plus different base models. Cells carry their source; columns are not a ranking.

## Matrix

| entity | who-optimizes | edit-surface | feedback-channel | terminal-bench-2 | cross-model-transfer | key-finding |
|---|---|---|---|---|---|---|
| [[self-harness]] | the agent edits its own harness (3 base models) | bounded single-surface edits per failure pattern | weakness-mining + regression-gated acceptance ([[self-harness-2026#^c5]]) | 64-subset, 2 attempts: held-out 61.9 / 38.1 / 57.1 ([[self-harness-2026#^c4]]) | model-specific *by design* ([[self-harness-2026#^c7]]) | edits must be executable changes, not generic prompt padding ([[self-harness-2026#^c10]]) |
| [[ahe]] | an evolution agent, base model fixed | **full** component set (tools/middleware/skills/sub-agents/memory/prompt) | 3 observability pillars; falsifiable change-manifest ([[ahe-2026#^c6]]) | full 89, pass@1: 69.7→77.0, > Codex 71.9 ([[ahe-2026#^c1]]) | **+5.1–10.1 pp cross-family** ([[ahe-2026#^c7]]); frozen transfer to SWE-bench-verified ([[ahe-2026#^c5]]) | gain localizes to tools/middleware/memory; **system prompt alone regresses −2.3pp** ([[ahe-2026#^c8]], [[ahe-2026#^c9]]) |
| [[meta-harness]] | external coding-agent proposer (Opus 4.6), frozen base | whole harness **code** | [[filesystem-feedback-channel]]: raw history of all prior candidates ([[meta-harness-2026#^c6]]) | pass-rate: 76.4 (Opus 4.6) / 37.6 (Haiku 4.5), > Terminus-KIRA ([[meta-harness-2026#^c10]], [[meta-harness-2026#^c11]]) | re-search per frozen model; one harness transfers on math across 5 models ([[meta-harness-2026#^c8]]) | full-trace access >> scores-only ≈ scores+summary; summaries can hurt ([[meta-harness-2026#^c6]]) |
| [[ace]] | the agent's own reflect-curate loop | prompt-borne **playbook only** | reflector lessons from execution feedback ([[ace-2025#^c2]]) | 68.9 under AHE's NexAU seed (**below** 69.7 seed) ([[ahe-2026#^c2]]) | ? (strong on its *own* AppWorld/finance, +10.6/+8.6 — [[ace-2025#^c1]]) | dominates on pure context-adaptation tasks but cannot reach the tool/memory gains on harness benchmarks |

## Analysis

- **Convergence on where the gain lives.** AHE's ablation ([[ahe-2026#^c8]], [[ahe-2026#^c9]]) — system prompt alone *regresses*, gain is in tools/middleware/long-term memory — independently agrees with Self-Harness's finding that edits must be executable, model-specific changes "rather than generic prompt padding" ([[self-harness-2026#^c10]]). Two papers, two methods, same conclusion: prompt tweaking is not where harness performance comes from. This is the cluster's most robust result, and it is *why* prompt-only [[ace]] is beaten on the harness benchmarks (68.9% under AHE's own seed; [[ahe-2026#^c2]]).
- **The main fork is who searches.** Self-Harness and AHE have the *agent itself* evolve the harness from its own traces; Meta-Harness runs an *external* coding-agent proposer over harness code for a frozen model. Both directions beat strong hand-engineered baselines (AHE > Codex 71.9; Meta-Harness > Terminus-KIRA) — so "human-designed harness" is not the ceiling, however you automate.
- **Feedback richness is the second axis, and it points the same way as the wiki's own thesis.** Meta-Harness's ablation ([[meta-harness-2026#^c6]]) shows raw full-history access beats scalar scores *and* beats LLM summaries — summaries can actively hurt. AHE reaches the same place from the other side with its falsifiable change-manifest ([[ahe-2026#^c6]]) and "distill traces to consumable root causes" pillar. Preserve the evidence, let the consumer query it — see [[filesystem-feedback-channel]].
- **Self-attribution is unreliable.** AHE finds edit self-attribution is evidence-driven for fixes (~5× random) but near-blind to regressions (~2× random) ([[ahe-2026#^c10]]) — which is exactly why Self-Harness needs an explicit **regression gate** ([[self-harness-2026#^c5]]). The two papers describe the same hazard and the same mitigation.
- **The numbers do not stack.** No row's Terminal-Bench-2 cell is comparable to another's: 89-vs-64 tasks, pass@1-vs-2-attempts, and different base models. Treat the column as "each method beats its *own* strong baseline," not a leaderboard — the [[terminal-bench-2]] entity records the protocol divergence.

## Verdict

**No single winner; the choice is set by who controls the loop and what you can edit.** (1) If the agent must improve itself online from its own traces, **Self-Harness / AHE** — and AHE if you can expose the *full* component set as editable files ([[nexau]]), since the gains it localizes (tools/middleware/memory) are unreachable from the prompt. (2) If you can afford an external search budget and want to optimize the harness *code* for a frozen model, **Meta-Harness** — its edge is the [[filesystem-feedback-channel]], not code-space search per se. (3) **ACE** is the right tool for pure context/memory adaptation (AppWorld/finance), but is dominated on harness benchmarks because it edits only the prompt-borne playbook. Cross-cutting rule for co-design: **put the gain in tools/middleware/memory, gate every edit against regressions, and feed the optimizer raw traces, not summaries.** Open question on whether any of this reliably beats hand-design across regimes: [[when-does-optimization-beat-hand-design]]. Feeds the [[agent-self-improvement]] map; lineage still queued (ADAS, Darwin-Gödel Machine, STOP).
