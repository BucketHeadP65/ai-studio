# The rematch: proof harness

An agent explores a retrieval task once, distills its own successful procedure into a
workflow, and every similar task afterward runs the workflow (cheaper, faster,
inspectable, and trust-graded) without the library ever hardening into dogma. This harness
proves that end to end, honestly, over the research knowledge base's six withheld `question` pages:
recall is set arithmetic over the page ids each published synthesis cites, never a model's
opinion of an answer.

- **Path A (baseline / challenger)**: a fresh pydantic-ai agent, engram's read surface,
  answering each question from scratch, no memory of any prior run (`proof/baseline.py`).
- **Path B (reuse / champion)**: search the library, fetch the saved workflow
  (fingerprint-checked for drift), run it headless against the question (`proof/reuse.py`).

## The fairness rules (the whole argument)

The only variable between paths is whether a persisted procedure exists:

1. **Same tool surface.** Both paths reach the knowledge base through the identical `reads` set
   `harness.composition.build_environment` derives, the baseline agent gets it directly, a workflow's steps
   get it by grant. No path reaches a tool the other cannot.
2. **Same model, same gateway** (`app/settings.py`'s `model()`), wrapped in one shared
   counting model (`proof.instrument.RecordingModel`) that meters requests and tokens
   identically for both paths.
3. **Budgets cap runaways. Claims rest on actuals.** `BASELINE_BUDGET` is generous and fixed
before the workflow exists. Every cost figure in `proof/report.md` comes from recorded
   requests and tokens, never from the cap. The report verifies the cap was never binding.
4. **One extraction function.** `proof.instrument.surfaced_ids` reads knowledge base ids off recorded
   tool traffic for both paths, no path gets its own accounting.
5. **Withheld enforcement, mechanical.** `proof.knowledge_base.GuardedKnowledgeBase` refuses a fetch of a
   withheld question page and scrubs it from listings. A run that still surfaces one through
   raw SQL is marked contaminated and disqualified, shown in the report, never hidden.

## Reproducing it

```bash
cd engram && docker compose up -d                # the knowledge base + workflow store
cd ../workflows
uv run python -m proof.baseline                  # Path A, all six questions -> proof/out/
# then, human-driven: a studio session — explore Q1, ask the agent to distill what it
# just did into an artifact, validate, save. Hand-write proof/out/distillation.json
# ({"workflow_id", "model_calls", "validator_round_trips", "notes"}) from that session.
uv run python -m proof.reuse                     # Path B against the saved artifact
uv run python -m proof.report                    # folds proof/out/ into proof/report.md
```

## Why this harness outlives one run

This is not a one-shot proof. It is the library's standing
challenger. The library only earns dogma-immunity if something keeps re-litigating whether a
saved workflow still beats exploring from scratch, re-run this same runner pair whenever the
model stamp (`harness.adapters.models.MODEL_ID`) moves, and let the report say, in actuals, whether the
champion still wins the rematch.
