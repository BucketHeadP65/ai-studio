---
name: saving-a-workflow
description: >-
  Turn a settled discussion into a workflow: the node types, how to define a step this app lacks, the validator findings you will hit, and how to write applicability prose with a real 'do not use when' half. Read it before drafting or saving anything.
---

# Saving a workflow

Use this after a discussion has settled what the person wants, and only then. A workflow saved
from a half-understood request is worse than no workflow: someone will find it later, believe
the applicability note, and run the wrong thing.

## What you are producing

A **workflow**: data, not code. `run` nodes each naming one step, `gate` nodes where a human decides,
and wires saying where every input comes from. You never write orchestration logic. The builder
turns the workflow into a real graph and the machinery executes it. **This process registers no steps**,
so every `run` node names one the workflow defines itself (see below), and those definitions travel inside
the workflow, which is what lets it still run tomorrow, elsewhere.

```yaml
name: kebab-case-name          # this becomes the store id, so make it searchable
version: 1
description: One sentence, in the language of the person's problem.
parameters:
  entity:
    description: What the caller supplies.
    json_schema: {type: string}
nodes:
  - kind: run                  # `kind` is required, always, in JSON and in YAML
    id: orient
    work: orient               # names a step defined under `inline` below
    inputs:
      entity: {parameter: entity}
  - kind: gate
    id: approve_publish
    reviews: {node: orient}    # what the human reads
    rejected_to: orient        # must be upstream of the gate
  - kind: run
    id: publish
    work: write_the_file
    inputs:
      dossier: {node: approve_publish}   # from the GATE, never from `orient`
output: {node: publish}
```

## Reuse a step from the library before defining one

The library holds steps other workflows already run: `search_steps` finds them by what they do, `fetch_step` shows one
whole (its name, inputs, output and tools). When one fits, name it in `save_workflow`'s `uses` list and wire your `run`
or `map` node to its `name`. The step is copied into your workflow at save time, so the workflow still carries
everything it needs, and the library remembers where the step came from. Define a step under `inline` only when nothing in
the library fits. A step you fetched may be typed `{like: <its name>}` by any step you define beside it.

Read a reusable step's `source:` before you reuse it, and write your own the same way: inside a program every granted tool
is async, so each call is `await <tool>(...)`. A call without `await` hands you a coroutine, which either blows up on
the next line or is quietly counted as a result nobody wrote.

## Defining the steps

Every step is defined here. This process registers none. A definition is built, typed and checked
before the run starts, exactly like code would be:

```yaml
inline:
  - name: weigh_contradictions          # lower_snake, unique within this spec
    instructions: |                     # the whole briefing, written to whoever performs it
      You are given a dossier. Rank the places its sources disagree by how much the
      disagreement would change a reader's decision, and say what would settle each one.
      Work only from the dossier. Do not soften a conflict to make the summary tidier.
    inputs:
      dossier: {like: build_dossier}    # the EXACT type another step in this spec produces
    output:
      json_schema:                      # a shape you are inventing
        type: object
        title: Contradictions
        properties:
          ranked: {type: array, items: {type: string}}
          would_settle_it: {type: string}
        required: [ranked]
    tools: [run_sql, run_command]       # optional, and only names the host offered
```

Rules that are enforced, not suggested:

- **`{like: <step>}` whenever one step consumes another's output, always.** Types are compared by
  identity, not by shape. Re-describing the producing step's fields yourself produces a *different*
  type and earns a `type_mismatch` you will not be able to explain from reading your own workflow.
- **`tools` may only name tools this run offered you.** Retrieval is read-only SQL over the server's
  `api` surface, which reaches its hybrid search, full text and typed graph as callables, one tool
  that composes rather than four that do not.
- **A step that must ACT asks for `run_command`.** That is the shell, and it is how a step writes a
  file, runs a formatter, or checks what it just produced. There is no bespoke write verb: a shell is
  what an agent writes files with.
- **Wire an acting step from a gate, never from the step that produced the value.** `inputs: {report:
  {node: <the gate>}}`. A gate passes its reviewed value straight through, so work that consumes an
  approval is structurally incapable of running without one. Wired off the producing node instead,
  the gate is decoration and the file lands either way. Say in the briefing exactly what it will
  write and where. The person approving is approving that sentence.
- **A gate's `inputs` may wire from nodes, not only parameters.** The producer runs before the gate,
  same as any other wire. One shape is refused at save time with the fix named in the finding:
  a rejection that rewinds into the middle of a fan (some producers of a wait re-run, others never
  do, so point `rejected_to` at a node every path passes through).
- **The briefing is the whole contract.** It is all that step will ever see besides its typed
  inputs: no conversation, no memory, no chance to ask. It also travels inside the workflow, so it
  is what someone reads in six months to decide whether to trust the step. Write it for that reader.
- **One step, one job.** Two jobs in one briefing is how a step ends up doing the first well and the
  second not at all. Split them and wire them. That is what the graph is for.

## Distilling from your own trace

Sometimes there is no discussion to shape into a workflow, there is a trace. You explored something
yourself, in this same conversation, reached a real answer, and the person asked you to keep the
procedure that got you there. Read your own trace once more, the way you would read someone else's
request, and put everything you find on exactly one of three piles, **the braid rule**:

- **What the person supplied: a parameter.** Their question, not your paraphrase of it. If they
  asked something and you built the answer around it, the question text is `question: str`, the
  exact words, wired in, not folded into a briefing where a later run cannot see them separately.
- **What you yourself did: a node.** Every query, every read, every weighing of sources you
  actually performed. That work is what the workflow preserves. The sentences you used to narrate it
  to yourself while doing it are not.
- **A dead end: dropped.** A query that came back empty, a line of reasoning you abandoned, it
  shaped your judgment on the way to the answer, but it produces nothing downstream needs wired,
  so it is not a node. Keeping it anyway, to be faithful to the trace, is how a distilled workflow
  ends up doing work nobody asked for, forever.

Once the piles are sorted, decide each surviving node's type by what it actually did.

**Deterministic glue is a program, never a briefing.** A step in your trace where you deduped ids,
merged two ranked lists, filtered by a score, truncated to k, or reshaped a payload for the next
step did arithmetic, not judgment, write it as a `source:` step (`Program`), not an
`instructions:` one:

```yaml
inline:
  - name: fuse_and_truncate
    source: |                          # arithmetic, not a briefing -- last line is the result
      scores = {}
      for doc_id in dense['hits'] + lexical['hits']:
          scores[doc_id] = scores.get(doc_id, 0) + 1
      ranked = sorted(scores, key=scores.get, reverse=True)
      {'ids': ranked[:10]}
    inputs:
      dense: {like: dense_search}
      lexical: {like: lexical_search}
    output:
      json_schema: {type: object, properties: {ids: {type: array, items: {type: string}}}, required: [ids]}
```

A briefing for this step pays a model call, on every future run, forever, to re-derive the same
dedupe, and it will answer slightly differently each time. A program is paid for once, by you,
right now, and after that it is arithmetic.

A program is a **straight-line script**, never a function definition. Its inputs arrive as plain
globals (`dense`, `lexical` above, read them directly), and its **last line is the result
expression itself**. Do not wrap the work in `def`/`async def` and end with the function's name:
the step's result would be the function object, not its return value. `check_draft` refuses the
shape cold, so you will see the finding before it can cost you a run. Granted tools
are likewise called directly (`await run_sql(sql=...)` as a top-level statement).

**Judgment stays a briefing, unless one step's character earns declaring.** Reading, weighing, and
synthesizing are still `instructions:` steps, granted only the tools they name. But the step where
your actual answer took shape (the one whose own settings, name, or description are worth stating
rather than folding into a generic briefing) can instead be an **agent step** (`AgentStep`): a real
pydantic-ai `AgentSpec` document, carried verbatim under an `agent` key, wired by the same `inputs`
/ `output` / `tools` slots every inline step uses:

```yaml
inline:
  - name: synthesize_answer
    agent:
      name: synthesizer
      description: Weighs retrieved evidence and writes the grounded answer.
      instructions: |
        Answer the question using only the given evidence. Cite the ids you relied on and say
        plainly where the evidence does not settle it.
    inputs:
      question: {json_schema: {type: string}}
      evidence: {like: fuse_and_truncate}
    output:
      json_schema: {type: object, properties: {answer: {type: string}}, required: [answer]}
    tools: [run_sql]
```

The document proposes. The host retains reach, exactly as everywhere else in this design. Model
and output type are always the host's, never the document's, and `tools:` grants by name just like
a briefing's. Never declare `output_schema`, `deps_schema`, a templated `{{...}}` instruction, or a
`capabilities` list inside the document, each is refused cold as a `bad_task_definition`, same as
any other step that could not be built, because each is a way of asking for reach the host has not
granted. The run's own knobs are refused the same way: `model_settings`, `retries`, `tool_timeout`,
`end_strategy`, and `metadata` all configure the host's run, not your agent step, `model_settings` in
particular would ride the host's authenticated request, so declaring any of them is refused cold
too. Reach for an agent step only for the one step whose character deserves declaring. Every other
judgment step is a plain briefing.

**No gate decided by a person in a replayable retrieval workflow's answer path.** The save gesture is where the
human already decided to keep this procedure, a gate inside the workflow would stop and ask again,
on every headless re-run, for a decision the person made once by pressing Save. A workflow that
answers a question on its own carries no `gate` node between its input and its output.

**Say when not to reuse this one.** The applicability note is what a future search has to go on,
say what made this trace's shape fit: one well-scoped question over a knowledge base this size, say, not a
broad multi-pass audit, or a question this workflow has never seen a contested claim like. Distilled
from a single trace, it has only ever been tried on the one question that produced it. Write the
"do not use when" half as though that is true, because it is.

## The order that avoids wasted work

1. **Search first.** `search_workflows` with the person's own words. If something fits, say so
   and stop. The cheapest good outcome is reuse. `fetch_workflow` tells you whether it
   fits *this* run. A workflow written for another environment is not a candidate.
2. **Shape it in prose before YAML.** Which steps, what each one consumes, what a human must
   see before anything irreversible. No steps are registered, so there is nothing to name: say what each step must do,
   then define it.
3. **Wire it.** Two rules do most of the work:
   - Anything with a side effect takes its input **from a gate**, or the gate guards nothing.
   - Independent nodes need no wiring between them. They run in parallel because nothing joins
     them. Do not chain steps that do not depend on each other.
4. **`check_draft`.** This runs the real validator. Iterate until it is clean. Never describe a
   draft as valid without having checked it, and never argue with a finding. Every one of them
   is a mechanical fact about the wiring.
5. **Save only when asked.** `save_workflow` validates again and refuses anything unexecutable,
   and this library also refuses anything undescribed (below). The person also has a Save button.
   Deciding to keep something is theirs, not yours.
6. **Change a saved workflow with `adapt_workflow`, never a re-save.** Pass the id and edit ops
   (`remove_node`, `add_node`, `replace_node`, `rewire`, `set_output`, `remove_inline_step`,
   `replace_inline_step`, `add_inline_step`, `add_parameter`, `set_description`). The machinery applies the diff, records the parent, and the
   nodes the diff did not touch keep the trust their parent's runs earned. An edited copy saved
   fresh is an orphan: similar, unrelated, and starting from zero. If a fan-out needs the *element*
   type of a list another step produces, say `{like: <step>, element: true}`, do not declare a
   carrier step just to mint the type. To change an inline step's implementation (a briefing that
   should be a program, a program that should compute something else) use `replace_inline_step` under the
   same name, never `remove_inline_step` and a re-save: removing it first strands every `{like: <step>}`
   slot and puts `unknown_work` under the node that ran it.

## A save is refused until somebody has described it

```
save_workflow(spec_yaml, applicability, title="", summary="", purpose="", produces="",
              owner_participation="", not_for="", uses=None, intent=None)
```

Validating is half the bar. This library also refuses a workflow nobody has described, and the
refusal names every missing field at once, so a save with four blanks costs one round trip and
buys nothing. Six fields decide it:

| field | what it answers | bound |
|---|---|---|
| `title` | what the workflow is called | 1 to 80 characters after trimming |
| `summary` | one line for the card | 1 to 280 |
| `purpose` | what this workflow is for | 1 to 2000 |
| `produces` | what a finished run leaves the person holding | 1 to 2000 |
| `owner_participation` | what the person themselves does during a run | 1 to 2000 |
| `not_for` | what to reach for something else for | 1 to 2000 |

Three rules to write them under:

- **A placeholder is refused, not accepted.** The deny set is exactly `tbd`, `todo`, `n/a`,
  `(not stated)` and `coming soon`, compared after trimming, collapsing inner whitespace and
  casefolding. Writing `TBD` costs the same round trip as writing nothing.
- **Nothing fills a blank in for you.** No fallback derives a title from the workflow's name or a
  summary from its description any more. A field you leave out is recorded as a field nobody wrote.
  Ask the person for the ones you cannot answer.
- **These are the author's words, not the graph's.** `purpose` is not the `description`. The
  description says what the graph does. `purpose` says what somebody would reach for it for. A
  summary that just repeats the description is noted beside the box on the person's screen and
  refuses nothing. No tool result tells you about it, because every one of them reports errors
  only, so write a summary worth reading rather than waiting to be told.

`intent` is a different record again and is never a substitute for these. It is what the person
asked for, in their own words, and it refuses nothing: a draft carrying half an intent saves, and a
draft carrying none saves. Pass it when somebody stated one, and leave it out rather than composing
a request nobody made.

`adapt_workflow` takes the same six, because the child gets its own card.

## Adapting a saved workflow

```
adapt_workflow(workflow_id, ops, applicability, evidence="", predictions=None, title="", summary="",
               purpose="", produces="", owner_participation="", not_for="", intent=None)
```

`adapt_workflow` changes a saved workflow by a typed diff instead of a re-save, so the child keeps
the link to its parent: the nodes the diff did not touch keep the trust their parent's runs earned,
and a later reader can see what changed and why. A re-save of an edited fetch stores an unrelated
orphan instead.

The known ops, each small enough to check on its own before any is applied:

- `{"op": "remove_node", "id": X}`: drop a node.
- `{"op": "add_node", "node": {...}}`: a full node object, same shape as `spec.nodes`.
- `{"op": "replace_node", "id": X, "with": {...}}`: swap one node for another.
- `{"op": "rewire", "id": X, "input": name, "to": {...}}`: point one input at a new wire.
- `{"op": "set_output", "to": {...}}`: what the workflow answers with.
- `{"op": "remove_inline_step", "name": X}`: shed a step definition no node runs any more.
- `{"op": "replace_inline_step", "name": X, "with": {...}}`: swap an inline step's implementation
  under the same name (a briefing becomes a program, say), keeping the name so every
  `{like: X}` slot still resolves. Renaming instead of replacing strands those slots and puts
  `unknown_work` under the node that ran it. Every node running `X`, and every step whose
  slots say `{like: X}`, stops carrying its parent's evidence once the replacement's type
  differs from the original's.
- `{"op": "add_inline_step", "with": {...}}`: bring a whole new step definition (a briefing or a
  program) under a name the workflow does not declare yet, then `add_node` or `replace_node`
  in the same set to run it. This is how a redesign grows a step: never repurpose an existing
  step under its old name, and never re-save to get a new one. A name already declared is
  `replace_inline_step`'s business. A rename is `add_inline_step` of the new name, the nodes rewired to it,
  then `remove_inline_step` of the old.
- `{"op": "add_parameter", "name": X, "with": {"description": ..., "json_schema": {...}, "required": false}}`:
  an input the workflow never took, same shape as one entry of `spec.parameters`. This
  is how a saved workflow gains a scoping input (a hand fire over chosen items instead of the
  whole batch): add the parameter, then `replace_inline_step` the step that reads it so it takes the
  new input, and `replace_node` or `rewire` the node that runs it to `{"parameter": X}`. A name
  already taken is refused. Make it optional (`required: false`, a string, empty meaning "all")
  so every schedule and older caller keeps working.
- `{"op": "set_description", "to": text}`.

An op naming anything that is not there refuses the whole set. Nothing half-applies.

Say why you changed it (`evidence`, prose for the person who asks later, never graded) and claim
something a run can settle (`predictions`, graded off the run evidence, never by a model and never
by you. Stating a claim is yours, settling it is the machinery's). The known claims:

- `{"claim": "completes"}`: a run of the child finishes.
- `{"claim": "produces", "subject": node_id, "shape": TypeName}`: that node yields a value, of
  that type when you name one.
- `{"claim": "accepts", "subject": parameter}`: a run is given that parameter.
- `{"claim": "approves", "subject": gate_id}`: that gate releases its value.

A claim about a version nothing has run yet reads as unmeasurable, not as a miss. Claim what the
edit is actually for, not whatever is safe to claim.

## Findings you will actually hit

| finding | what it means |
|---|---|
| `unknown_work` | You named a step this run does not have and did not define. Either define it under `inline`, or say which step is missing, never substitute a lookalike silently. |
| `bad_task_definition` | A step you defined could not be built. The detail says which construct: a `like` that names nothing, a duplicate name, a tool that was not offered, or a shape using JSON Schema this builder refuses (`$ref`, `anyOf`, `oneOf`), write one concrete shape inline instead. |
| `dead_end_node` | Something's output is read by nobody and is not the workflow result. Wire it or drop it. |
| `input_mismatch` | The step takes different arguments than you wired. Check the signature. |
| `type_mismatch` | A wire carries the wrong type. The types your own `inline` steps declared are the truth. |
| `bad_rejected_to` | A gate's rejection target is not upstream of it, so rejecting could not rerun it. |
| `extra_forbidden` | You put something in the workflow that is not part of the node types, usually a card field. The card's six fields are all separate arguments to `save_workflow` (above), and the spec has nowhere to say any of them. |

## Writing the applicability note

Two halves, and the second one is the one that earns its keep:

> Use when you have a specific contested claim and want an evidence summary published only
> after review. **Do not use when** you need a broad multi-pass audit across several retrieval
> strategies (prefer `wf.evidence-audit`), or when nothing is going to be published.

A card without a "do not use when" is how a wrong reuse happens six months later.
