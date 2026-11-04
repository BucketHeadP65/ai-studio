# research

A template for an indexed research knowledge base: source mirrors get digested into typed, claim-grounded pages, related
to what the knowledge base already holds, and new digests are read for ideas worth the owner's attention. Domain-general
by structure. The worked example here is retrieval and context engineering for LLM systems, but a knowledge base started
from this template can register its own vocabulary for any research domain.

## Contents

- `knowledge-base-schema.json`, the schema: page types, frontmatter contracts, vocabulary.
- `_meta/retrieval-policy.md`, `_meta/compounding-policy.md`, proven mechanism pages, copied verbatim as the
  knowledge base's starting default.
- `_meta/purpose.md`, `_meta/ingestion-policy.md`, skeletons: each section names what to write there, followed
  by a worked example. The knowledge base plugin's `/knowledge-base:init` interview fills these in for a new knowledge base.
- `workflows/workflows/research-digest.md`, "Digest new sources": turns the sources that have arrived into typed
  digest pages, linked to what the base already holds, with their references queued behind them.
- `workflows/workflows/research-compound.md`, "Flag ideas from new digests": reads the digests the base has gained
  recently, writes what the owner should see as pending flag pages, and repairs the defects it finds in the base's own
  digests in the same run.
- `workflows/workflows/research-decision-brief.md`, "Settle one decision from the reading": turns one decision
  question into a brief a second reviewer has checked against its own sources, plus a proposed question note nothing
  writes until you say so.
- `workflows/workflows/research-stance-refresh.md`, "Refresh stances from the docs": rewrites the stance pages a changed
  records page bears on, started by an `on index repo-docs` schedule with `collection=research`, `records=repo-docs`
  and `progress=adr:accepted, adr:superseded, architecture:current, spec-doc:approved, spec-doc:executed`, or by hand
  with `changed_page_ids` empty for a full pass. A topic no stance covers is proposed as a new stance only when its
  records page takes a position on a question research literature addresses. A placement under a stance the base does
  not list, such as a misspelt id, changes no stance and is named on the run card and in the run's output. Once every
  rewrite is written, one worker per proposal names the claims of the base's digests (its paper, talk, article and post pages) that support
  or contest that position. The run reads each cited digest and keeps a claim only when the digest is not at status
  skipped and holds that claim, copying the claim's text from the page. It drops a proposal left with claims from fewer than two digests, and a
  judge of its own rejects a claim that does not bear on the position or a proposal that duplicates a stance. An
  implementation choice with no research question behind it is skipped. The run card shows every approved proposal
  with the records pages behind it and each finding (the claim's reference and text, whether it supports or contests
  the position, and one sentence on how) among what the run left behind (the `proposed_stances` artifact), marking
  one the literature only argues against, so you draft the ones you pick by hand. Each approved stance proposal is also
  written as a pending stance flag, `flag.stance-` and the proposed id without `stance.`, in `Wiki/Flags`, with its
  records pages, the reason placement gave for each and its findings as claim links. You accept, dismiss or complete
  it there, and the run never writes a flag you decided again, even before the base is indexed again. A pending flag
  you moved is written where it sits, and a page of another kind under the same id is left alone and named. A proposal whose worker changed its
  id or title, or added or left out a page, is left off. A run whose proposals fail keeps its rewrites: each is
  written to its file at once, a knowledge base indexed once per run indexes them when the run ends, and under a
  `wait` gate policy a proposals judge that cannot decide holds that index until you answer. Under the opt-in Temporal
  engine writes are never held for the run, so even such a knowledge base indexes after each write.
- `workflows/steps/`, the steps only these workflows use.
- `workflows/evals/examples/`, worked examples: an authored brief in the shape the steps return, and beside it the
  real output of the workflow's own deterministic program over that brief.

Not included: `schema.md` (generated from the schema at init) and `dashboard.md`/`log.md` (knowledge-base-local, never
templated).

## Initialize a knowledge base from this template

```bash
studio init --template research
```

This copies the template's `knowledge-base-schema.json` and `_meta/` guidance pages into the new knowledge base before the scaffold
runs. AI Factory Studio offers the same starting point: the create-a-knowledge-base dialog lists every template from
`GET /api/templates`, and `POST /api/knowledge-bases` takes a `template` slug in place of an inline schema.

The knowledge base owns its copy from that moment and may drift from the template. The template stays a starting point and the
workflows' home, not a live dependency of any knowledge base built from it, and nothing is ever pushed back the other way.

## Import the workflows

Against the running stack, from `harness/`:

```bash
uv run harness import ../templates/research/workflows
```

This imports the steps first, then the workflows that use them, into the library. Import the steps the templates
share first, with `uv run harness import workflows`, because these workflows use steps that ship in `harness/workflows`.

Each workflow file carries a `presentation` block: what the workflow is for, what a finished run leaves you
holding, what you yourself do during a run, and what to reach for something else for, plus a label for every
parameter. Those are the author's own words, and the Studio shows them rather than composing a sentence of its
own. The `result` block beside them declares the run's primary work and the artifacts a finished run offers.
Editing one of those blocks is editing what a person reads. Editing a node id or the graph is a new
workflow version.

## Reference automations

Two workflows are kept as worked examples of what a readable automation looks like, one here and one in the
`repo-docs` template. They exercise different shapes on purpose.

- "Settle one decision from the reading" here answers one question and stops. It reads, it has a second reviewer
  check its evidence against the sources, and it writes nothing. What a finished run leaves you is a brief and a
  proposed note you may keep.

  Keeping it is your own separate act. The run's `note_draft` artifact is a proposal: this template's
  `render_decision_brief` step builds it as a `question` page, with that page type's canonical sections and the
  `question_status` the decision implies, and nothing writes it anywhere. On the run's page, **Keep this as a question note**
  opens a preview against whichever knowledge base you choose, shows the note as that knowledge base would write it, and
  writes only once you confirm. The draft never names a file or an id: the destination mints the id from the run and
  the value it came from, so editing the words before you keep it does not move where it lands. A page already at
  that path is never replaced, whether it is this same note kept earlier or your own edit of it.
- "Fold notes into analyses" (`templates/repo-docs/workflows/workflows/docs-notes-to-analysis.md`) changes the
  knowledge base. It runs unattended with no gate, rewrites a page per initiative and resolves the threads it folded.

`workflows/evals/examples/` holds a worked example of each, so you can read the shape of what a workflow leaves you
holding before you start one. No model wrote any of them: the brief or the analysis draft in each is authored, in the
exact shape the steps return, and what sits beside it is the real output of that workflow's own deterministic program
over it, reproducible byte for byte. Nothing in either folder is a record that a workflow was run. The `repo-docs`
template holds the same folder for its own example.

"Settle one decision from the reading" is also one of the two kinds of work the development cases cover. Those cases
live with the `repo-docs` template, in `templates/repo-docs/evals/workflow-automation-development.v1.json`, and the
ones naming this kind of work state what a decision brief has to do to pass: leave a brief and a proposed note and
write no page at all, have its second reviewer refuse the evidence once and accept the rework, and recommend nothing
at all when the reading only covers one side. Every one of them is author-visible development evidence, so a result
from any of them says nothing about a case nobody wrote.
