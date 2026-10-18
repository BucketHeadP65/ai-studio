---
id: harness-adr-0020-inspection-evidence-and-outcomes
type: adr
title: Inspection uses complete evidence and independently checked outcomes
summary: Independent execution and terminal reconciliation protect coverage, explicit verdicts protect uncertainty, and paired outcome evaluations support proposed improvements without treating diagnoses as causal proof.
status: proposed
aliases:
- harness-adr-0020-inspection-evidence-and-outcomes
app: harness
created: '2026-09-12'
---

## Context

Inspection feeds proposed repairs from recorded conversations. A slow scan can interfere with scheduling when job kinds share execution capacity. Incomplete evidence can produce a convincing but unsupported diagnosis, and a failed attribution can be mistaken for dismissal. The owner requested the corrections and an outcome-based improvement loop. The existing record, coordination jobs and inspector remain the foundation.

## Decision

D1. Scheduling and dispatch, reconciliation, rules, attribution and scanning run in independent loops. Reconciliation freezes an event watermark, pages live and sweep watches, and tracks the final transcript with terminal event markers. A queued final scan is not proof of completed inspection.

D2. Every recorded instruction and tool/output schema is reachable through the inspector's existing paged reading verbs. Missing recordings remain unknown. Output validation occurs inside the agent run so rejected answers appear in the trace.

D3. A finding or an explicit dismissal is a terminal lead decision, finalized under a shared row lock. Uncertainty, invalid output and unprocessed output are distinct. An attribution is a causal hypothesis, not experimental evidence that a repair works.

D4. Claims of generalization require independently withheld task outcomes with repeated paired trials, preserved failures and resource usage. The comparison framework verifies disjoint inputs and ids, not author blindness. Expected verdict and cause labels stay with the grader. A suspected symptom may be supplied as input when evaluating attribution. Artifact identities include code, taxonomy, data, prompts and execution conditions, while an unknown model revision stays unknown.

D5. A durable learning record retains comparable supporting and contradictory outcomes, successful alternatives, and every accepted or rejected proposal. Failed trials stay in their rejected attempt and do not become directional repair evidence. Nothing is automatically activated by a score.

## Consequences

Inspection can proceed without holding scheduled work and can recover terminal work missed by event delivery. Model judgments stay inspectable and uncertainty remains visible. The wider evidence surface costs bounded additional reads. Reconciliation is eventually consistent within its configured window and may repeat work after a crash.

The evaluator can verify mechanical outcomes immediately, while agreement with human labels and broader task generalization require separately reviewed cases. The first live ablation improved inspector diagnostic task success from 11/18 to 18/18 on nine synthetic agent-reviewed cases, using the same configured and observed model. All cases were authored and seen by the implementer. Eight supplied a suspected symptom. Both variants ran the same updated inspector, and the baseline removed information that the previous attribution path already supplied. The result is an evidence-removal demonstration, not a before/after release benchmark or an independent test of generalization. It does not establish downstream agent improvement or a provider-pinned model revision. The dataset and compact result are under `harness/evals/inspection/`, and full local reports and traces remain in the harness state directory.
