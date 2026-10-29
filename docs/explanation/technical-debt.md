---
id: technical-debt
aliases: [technical-debt]
type: explanation
app: root
status: current
tags: [debt, record]
title: "Technical debt, what we knowingly left worse than it should be and what fixing it looks like"
---

# Technical debt

The record of things this repo knows are wrong or weaker than they should be, and left standing anyway. An entry belongs here
when the code works but should not stay as it is: a guard that is missing on one path and present on its sibling, a duplicate
surface, a test that pins the wrong thing, a name that no longer describes what it does. A deliberate choice not to adopt
something is not debt, it goes in [deferred-decisions.md](deferred-decisions.md).

Add an entry in the change that creates the debt or the review that finds it, while the evidence is fresh. Remove it in the
change that pays it off, naming the commit.

Entry shape: what is wrong, where, why it is debt, what it costs while it stands, what paying it off looks like,
when it was found.

## TD-2. The files plugin and the shell are two surfaces for one job

- **Where:** `harness/src/harness/features/authoring/agent.py`, the `shell` and `files` plugin mounts.
- **What is wrong:** the files plugin mounts eight tools (`read_file`, `write_file`, `edit_file`, `list_directory`, `search_files`,
  `find_files`, `create_directory`, `file_info`) beside a shell that does all eight with `cat`, `ls`, `grep`, `find`, `mkdir`, `stat`.
- **Why it is debt:** sixteen ways to do eight things. Agent-ux principle 8 says a listing where two entries could be confused is a
  design defect. A live trace shows the model searching with `run_command` and reading with `read_file` in one turn.
- **Cost while it stands:** tool-listing tokens on every request, and a real choice the model has to make with no help beyond the
  plugin copy added in `f17d3ed3`.
- **Paying it off:** mount only `read_file` and `edit_file` from the files plugin and let the shell find and list, keeping NoteOverlay
  (which rides `read_file`'s return) working. Decide it from a live session with the plugin closed, not from the code.
- **Found:** 2026-08-26.

## TD-35. One commit holds two unrelated changes

- **Where:** `8191d8ac`, `feat(harness): the chat answers prose, and a deferred end is expressible on every door`.
- **What is wrong:** the commit carries the chat reply contract AND the workflow spend brake. Two agents were editing one
  working tree, and the commit used directory-wide path adds (`git add harness/src harness/tests`) rather than naming the
  files the change owned, so it swept up the other agent's uncommitted work.
- **Why it is debt:** the history now says one thing happened where two did. Reverting the reply contract would silently
  remove the spend brake, and `git log` on `composition.py` names a commit whose message does not mention it.
- **Cost while it stands:** anyone reading the history is misled about what changed together, and a revert is unsafe.
- **Paying it off:** nothing, on a shared branch with another session committing to it. Rewriting it would move another
  session's commits. The entry stands as the record that the two changes are independent, and the rule that pays it off in
  future is: never a directory-wide `git add` while another agent holds the same tree, always the file list the change owns.
- **Found:** 2026-08-27, by the implementer whose work was swept up.

## TD-37. A tier change abandons the previous model's HTTP client

- **Where:** `TierModel._current()`, `harness/src/harness/adapters/model_tiers.py`.
- **What is wrong:** `_current()` rebuilds a tier's model when the stored setting has changed and keeps the new model,
  dropping the reference to the one it replaces with no call to close it. Nothing in the codebase enters a model as a
  context manager (`Model.__aenter__`/`__aexit__`, which delegate to the provider's own httpx client lifecycle), so the
  replaced model's client was never going to close before the process did anyway. A rebuild on a tier change does not
  change that in kind, only in count: it leaves one more idle httpx client open for the rest of the process's life.
- **Why it is debt:** an object nothing ever closes is holding an open connection, and the count of such objects grows
  by one on every owner change to a tier's model.
- **Cost while it stands:** a tier change is a rare, owner-driven action rather than something a turn or a workflow
  step triggers on its own, so a long-running process accumulates at most one idle client per change, and every one of
  them is reclaimed the next time the process restarts.
- **Paying it off:** close the replaced model in `TierModel` once pydantic-ai offers a close that is safe to call from
  this synchronous rebuild path, or move model construction onto a per-turn scope so a model closes on its own turn's
  exit instead of living for the tier's lifetime.
- **Found:** 2026-08-28, in the model tiers change that gave `TierModel` a rebuild-on-change tier read.

## TD-38. A version retired by a newer save is listed nowhere in the library

- **Where:** `LibraryCollection.catalog` and `.archived`, `core/src/core/ai/workflows/infrastructure/library_collection.py`
  (`CATALOG_SQL`, `ARCHIVED_SQL`). The library page, `studio-client/src/components/workflows/workflow-library.tsx`.
- **What is wrong:** since 6af6d3a8 saving a version links every live lower version of its name forward
  (`superseded_by`), and the catalog reads only pages nothing has superseded. A superseded page is not archived, so the
  Archived section does not list it either: it is reachable only by id (its detail page, its run history), from a link
  a person already holds.
- **Why it is debt:** the catalog has three states for a version (current, superseded, archived) and the library shows
  two. A person who wants to compare a workflow's versions, or reopen an old one's runs, has no page that lists them.
- **Cost while it stands:** an old version's runs and definition are found only through the run history or a remembered id.
  Nothing is lost: the pages, their runs and their lineage stay in the library.
- **Paying it off:** a per-workflow version list on the library card or the workflow page (the newest live version with
  its older versions beneath it), read through a store verb that answers every version of a name, superseded ones
  included, so the SPA never takes an id apart. Or an "older versions" section beside Archived, sourced from a query
  over `api.cards.superseded`.
- **Found:** 2026-08-29, in the change that made the catalog offer one version per name. The owner deferred it.

## TD-45. The code step runs programs through pydantic-ai-harness's private `MontyExecutor`

- **Where:** `harness/src/harness/adapters/environment.py` (`code_runner`), which imports
  `pydantic_ai_harness._monty_exec.MontyExecutor`.
- **What is wrong:** `code_runner` drives Monty through a class the published package keeps private. Nothing exports
  it, and its `run` signature changed in 0.36.0 (it now takes a callable that starts an async feed) without notice.
- **Why it is debt:** a private module may change or disappear in any release, so an upgrade learns of a change by
  breaking.
- **Cost while it stands:** an upgrade of pydantic-ai-harness can break every code step at once. The code-step tests
  (`harness/tests/unit/test_code_step.py`) catch it before it lands.
- **Paying it off:** use the executor once pydantic-ai-harness exports it, or a public function over it. An issue
  asking pydantic/pydantic-ai for that is drafted and not yet filed. Writing our own loop is not the fix, because
  resume, gather and cancellation would then exist twice and drift apart.
- **Found:** 2026-09-29, in the final review of the branch that moved pydantic-ai-harness to PyPI.

## TD-80. The skills list parses each skill file through pydantic-ai-harness's private `parse_skill`

- **Where:** `harness/src/harness/features/projects/infrastructure/skill_files.py` (`_card`), which imports
  `pydantic_ai_harness.skills._loader.parse_skill`.
- **What is wrong:** the list reads a `SKILL.md` and validates it with the function the published package keeps private. The
  package's public `Skills` capability reads whole libraries through a run's workspace, and nothing public loads one file.
  Release 0.52 removed the earlier `load_skill(Path)` without notice, which is how the private import was found.
- **Why it is debt:** a private module may change or disappear in any release, so an upgrade learns of a change by breaking.
- **Cost while it stands:** an upgrade of pydantic-ai-harness can break the project skills list. The catalog tests
  (`harness/tests/unit/test_skills_catalog.py`) catch it before it lands.
- **Paying it off:** use a public one-file loader once pydantic-ai-harness exports one. The upstream issue draft I3 asks for it.
  Writing our own parser is not the fix, because the frontmatter rules would then exist twice and drift apart.
- **Found:** 2026-09-30, in the move to pydantic-ai 2.52 and pydantic-ai-harness 0.52.

TD-46 onward came from the close of the code standard refactor on
2026-09-30, from items the refactor left standing and a closing review of every slice it converted.

## TD-46. Renames the audits proposed wait for a ruling

- **Where:** `harness/src/harness/features/scheduling/`. For example `spec` for a value that holds a `Workflow` (`service.py:117-146`
  and `:179`), `pool` for the `Sessions` a call reads (`service.py:64-78` and `:414`) and `run` for the `RunCollaborators` a launch
  takes (`service.py:113` and `:414`). The scheduling pilot's re-audit proposed sixteen such renames, N1 to N16.
- **What is wrong:** each name says something other than what it holds. The analysis the refactor followed never ruled on the
  sixteen, so the conversion left them for a later ruling by the owner.
- **Why it is debt:** the standard asks for one word per thing, the word the vocabulary defines. A reader of the slice meets `spec`
  where the vocabulary says workflow, and `run` for a set of collaborators.
- **Cost while it stands:** three private meanings to learn in one slice, and the slices that copied the pilot's shape repeat them.
- **Paying it off:** the owner rules on N1 to N16, then one refactor commit applies the accepted ones. None is an external name, so
  no route, tool or schema changes.
- **Found:** 2026-09-30, in the closing review of the code standard refactor. First proposed in the scheduling pilot's re-audit.

## TD-53. `ruff format --check` fails in every project

- **Where:** every Python project. At `62847680` the formatter would change 80 files in core, 170 in engram, 74 in studio, 196 in
  harness and 12 in connectors. Under `harness/src/harness/features` alone it would change 35 files, 15 of them created during the
  refactor, and `harness/src/harness/features/inspection/service.py:65` runs to 174 characters against the 140 line length.
- **What is wrong:** the formatter is configured in every project and is not part of the acceptance gate (`ruff check`, `pyright`,
  `pytest`), so nothing holds the code to it.
- **Why it is debt:** a configured tool that fails everywhere teaches everyone to ignore it.
- **Cost while it stands:** running `ruff format` on one file produces a diff full of unrelated lines, which hides the real change in
  review and invites a directory-wide reformat inside an unrelated commit.
- **Paying it off:** one formatting commit per project, then `ruff format --check` in each project's gate.
- **Found:** 2026-09-30, in the closing review of the code standard refactor.

## TD-62. The research template's vocabulary sits in engram's domain-general code

- **Where:** `engram/src/engram/kb/domain/note.py:144-148` (`BaseFrontmatter` declares `tasks`, `domains`, `stages`, `modalities`
  and `scale_regimes`), `engram/src/engram/kb/domain/payload.py:259-263` (the same five in every knowledge base's payload JSON
  schema), `engram/src/engram/indexing/domain/card.py:40` (`_CARD_FACETS`), `engram/src/engram/indexing/service/_row_builder.py:74`
  (keys on `page_type == "paper"`) and `engram/src/engram/retrieval/queries.py:1237-1255` (tests for `playbook`, `paper`,
  `comparison` and `entity`).
- **What is wrong:** one template's facet names and page types are written into code that serves every knowledge base.
- **Why it is debt:** root ADR-0018 and engram's own rules say a knowledge base's vocabulary lives in its template, and engram reads
  it from the knowledge-base schema.
- **Cost while it stands:** every knowledge base's payload schema offers five research facets it may not declare, cards read three
  of them whether declared or not, and a knowledge base that names its page types differently gets none of the paper, comparison or
  playbook handling.
- **Paying it off:** read the facets a card uses, and the page types these branches test for, from the knowledge-base schema, and
  move the five fields out of `BaseFrontmatter` into the schema's declared facets. A test with a template that declares none of
  them.
- **Found:** 2026-09-30, in the closing review of the code standard refactor. It predates the refactor.

## TD-67. One request field the type rule forbids

- **Where:** `harness/src/harness/features/authoring/api_models.py` (`ops: list[dict[str, Any]]`).
- **What is wrong:** the repo's type rule says never `Any`.
- **Why it stays:** typing it as pydantic's `JsonValue` changes the published OpenAPI schema from `additionalProperties: true` to a
  reference to a new `JsonValue` definition, an external change of its own.
- **Cost while it stands:** pyright passes any shape in an op until the service validates it.
- **Paying it off:** type the ops in the change that publishes the schema change.
- **Found:** 2026-09-30, in the closing review of the code standard refactor.

## TD-72. What engram still owes the code standard: two shapes and one ruling

- **Where:**
  - `engram/src/engram/evals/harness/agent.py` sits at the slice root, outside the named parts.
  - `engram/src/engram/evals/harness/service.py` imports its `infrastructure.instrumentation` directly. `configure_logfire` holds
    process-wide state and has no port.
  - `SeedCaseDraft` (`evals/generation/domain/drafting.py`) and `ReflectiveFinding` (`evals/rca/domain/reflection.py`) are output
    types a model reads, and their docstrings changed in `05519d5b` and `2c020be0` without a ruling.
- **What is wrong:** a module outside the slice layout, a service reaching its infrastructure with no port, and model-read text
  changed without the owner's word.
- **Why these stay:** moving `agent.py` under `infrastructure/` adds a second service-to-infrastructure import, and moving it under
  `service/` turns `service.py` into a package. A port for `configure_logfire` needs wiring in studio. Both are layout calls. The two
  output types wait for the same ruling the owner gave the recommendation proposer's types.
- **Found:** 2026-09-30, in the closing review of the code standard refactor.

## TD-75. What harness still owes the code standard: one import

- **Where:** `harness/proof/reuse.py` imports `model_list` from `harness.adapters.wiring`, which `harness.adapters.contracts` does
  not export.
- **What is wrong:** a proof script reaches past the adapters' contracts.
- **Why it stays:** exporting `model_list` from `harness.adapters.contracts` makes that module import `adapters.wiring`, which the
  layering test refuses.
- **Cost while it stands:** a move of `model_list` breaks the proof script.
- **Paying it off:** move `model_list` out of `wiring` into a module `adapters.contracts` may import.
- **Found:** 2026-09-30, in the closing review of the code standard refactor.

## TD-77. A written collection's pages written before its embeddings switch turns on keep a NULL embedding

- **Where:** `engram/src/engram/indexing/service/ingest.py` (`IngestPageHandler.handle`, which embeds through
  `EmbedStage.run_scoped` with the current page's embed inputs alone) and `engram/src/engram/indexing/service/embed_stage.py`
  (`EmbedStage.run`, the one path that embeds a collection's whole `embedding IS NULL` queue, called only by an index run from
  `index.py` and `incremental.py`).
- **What is wrong:** under `mode: "auto"` with no `floor`, the embeddings switch stays off until the collection holds more than
  200 notes or 150,000 tokens, so every page written before then lands with a NULL embedding. When a write's decision turns the
  switch on, that write embeds its own rows only. Nothing embeds the earlier pages, since no index runs over a written collection.
- **Why it is debt:** the switch says the collection earned embeddings, and the vector leg holds only the pages written after it
  said so.
- **Cost while it stands:** an agent memory collection keeps its first 200 or so notes at NULL for good, and search answers
  `hybrid` over a partial vector set, so the semantic leg never finds the oldest notes.
- **Paying it off:** when a write's decision turns embeddings from off to on, embed the collection's pending rows. A written
  collection has no files to rebuild their embed inputs from. `kb.pages.card_text` already holds the page input, and the section
  and claim inputs are not stored, so they need a column of their own or a rebuild from the stored rows that matches what
  `card.section_embed_input` and `card.claim_embed_input` build.
- **Found:** 2026-09-30, in the review of the change that decides a written collection's switches on every write.

## TD-79. An authored page section over the embed budget is embedded to its first 8000 tokens and the remainder is silently unsearchable

- **Where:** `engram/src/engram/indexing/domain/card.py` (`section_embed_input`, which cuts through `truncate_embed_input`) and
  `engram/src/engram/indexing/domain/embed_strategy/stock.py` (`SectionStrategy.units`, one embed unit per section).
- **What is wrong:** a page section is one `kb.sections` row with one `embedding`, however long its body is.
  `section_embed_input` builds its embed input and cuts it to 8000 tokens, keeping the head and dropping the tail with no report.
  `core.markdown.split_section_body` splits a section body within a token budget, and only the recommendation evals call
  it. `chunk_mirror` fills a mirror's long heading section into chunks of its own.
- **Why it is debt:** one embedder has two chunkers over it, and only the mirror chunker keeps within its budget.
- **Cost while it stands:** a long authored section, such as a pasted transcript or a long design section, gets a single vector
  for its opening. Everything past roughly 8000 tokens is unreachable by the semantic leg while the lexical leg and `api.fetch`
  still reach the full text.
- **Paying it off:** give a section over the budget more than one vector, one per part that `split_section_body` makes. A section
  row holds one `embedding` and its id is `<page-id>#<section-path>`, so the parts need rows of their own or a table of section
  part vectors, and search has to fold a part's hit back onto its section.
- **Found:** 2026-09-30, in the review of the change that splits a raw mirror's long section within the embed budget.

## TD-81. The run card looks a recorded result field up by its whole path, so every template's field reads not recorded

- **Where:** `studio-client/src/app/workflows/[id]/page.tsx`, the `recorded` result fields, which read
  `activeValues?.[field.path]`.
- **What is wrong:** a run's recorded values are keyed by bare node id (`write_stances`), while every template's
  `result_fields` declares its path as `values.<node>` or `values.<node>.<field>` (`values.write_stances`,
  `values.report.written`). The lookup takes the whole path as one key, so it never finds the value.
- **Why it is debt:** the recorded fields are the author's own words for what a run produced, and the card shows "not
  recorded" for each of them across every template workflow, so it says a run recorded nothing it did record.
- **Cost while it stands:** the owner cannot read a run's result off its card, such as the stances a refresh wrote or
  each stance as it was before the run, and has to open the run's values panel instead.
- **Paying it off:** resolve `field.path` against the run's values: drop the leading `values` segment, look up the node
  id, then walk the remaining dotted segments, keeping "not recorded" for a path that resolves to nothing. Cover it with
  a component test over a template-shaped path.
- **Found:** 2026-10-29, in the final review of the stance refresh change.
