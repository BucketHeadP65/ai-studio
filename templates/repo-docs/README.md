# repo-docs

A template for a repository's own docs-as-code knowledge base: initiatives, ADRs, and architecture docs indexed so an
agent can search and compound over them. This is this repository's own docs knowledge base, `docs/`, packaged as the
reusable template for any repository that wants the same compounding passes over its documentation.

## Contents

- `knowledge-base-schema.json`, the schema: page types (initiative documents, ADRs, architecture masters), frontmatter
  contracts, vocabulary.
- `_meta/documentation-policy.md`, `_meta/compounding-policy.md`, the guidance pages this repo's own docs knowledge base
  runs on, copied as the starting default for a new knowledge base.
- `workflows/workflows/docs-notes-to-analysis.md`, "Fold notes into analyses": turns every open owner thread on an
  initiative's files into a numbered finding in that initiative's analysis, then resolves the thread.
- `workflows/workflows/docs-promote-initiative.md`, "Promote executed designs": turns each executed design that no
  ADR absorbs yet into a proposed root ADR for the owner to accept.
- `workflows/steps/`, the steps only these two workflows use.
- `workflows/evals/`, datasets for the steps that ship one (`check-adrs.yaml`), and `workflows/evals/examples/`, a
  worked example of "Fold notes into analyses": an authored analysis draft in the shape the steps return, beside the
  real output of the workflow's own deterministic programs over it, which is the write payload, the thread replies
  and the counts. Nothing there is a record that the workflow was run. That workflow is one of the two reference
  automations, the one that changes the knowledge base. Its counterpart is "Settle one decision from the reading" in
  the `research` template, which answers a question and writes nothing.
- `evals/workflow-automation-development.v1.json`, the development cases for automating work in this knowledge base
  (asking for an automation, running it, repairing it, and answering from what it wrote), beside
  `evals/workflow-automation-development.v1.stamp.json`, the governance stamp for that version. Every case is
  author-visible, so it is development evidence and never independent generalization evidence, and the stamp says so:
  it names no held-out cases and no verified holdout. Each case carries the request a run is given, and its grader
  labels (the success criterion, whether no work is a right answer, the checked outcome and refusal it expects) sit in
  a `labels` block that never reaches a run. A reader passes the path in: nothing in the applications knows where a
  template keeps its evaluation data.

  A case may also name the kind of work it is about, in `task_family`, and the file declares which kinds of work it
  covers in a top-level `task_families` list beside `name` and `version`. That list is what closes the set: a case
  naming a kind of work the file does not declare is refused by the file itself, and the stamp beside it has to name
  the same list. The set declares two: the research decision brief, whose worked example is the `research` template's
  own reference automation, and documentation analysis (reading this quarter's initiative documents into one analysis,
  answering from documents that do not settle the question, and a week with no thread to fold, where doing nothing is
  the right answer). The rest of the cases are about the journey itself and name no kind of work. The names are this
  set's own words for the work: the applications carry them through and never read anything into them.

## Initialize a knowledge base from this template

```bash
studio init --template repo-docs
```

This copies the template's `knowledge-base-schema.json` and `_meta/` guidance pages into the new knowledge base before the scaffold
runs. AI Factory Studio offers the same starting point: the create-a-knowledge-base dialog lists every template from
`GET /api/templates`, and `POST /api/knowledge-bases` takes a `template` slug in place of an inline schema.

The knowledge base owns its copy from that moment and may drift from the template. The template stays a starting point and the
workflows' home, not a live dependency of any knowledge base built from it, and nothing is ever pushed back the other way.

## Import the workflows

Against the running stack, from `harness/`:

```bash
uv run harness import ../templates/repo-docs/workflows
```

This imports the steps first, then the two workflows that use them, into the library. Import the steps the templates
share first, with `uv run harness import workflows`, because these workflows use a step that ships in
`harness/workflows`.

Each workflow file carries a `presentation` block: what the workflow is for, what a finished run leaves you
holding, what you yourself do during a run, and what to reach for something else for, plus a label for every
parameter. Those are the author's own words, and the Studio shows them rather than composing a sentence of its
own. The `result` block beside them declares the run's primary work and the artifacts a finished run offers.
Editing one of those blocks is editing what a person reads. Editing a node id or the graph is a new
workflow version.
