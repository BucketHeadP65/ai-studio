---
id: core-ai
type: reference
app: core
status: current
tags: []
title: "core.ai: the shared AI-stack module contracts"
summary: 'Typed shared contracts for model access, retrieval tools, progressive disclosure, history and workflow execution across the Python applications.'
---

# core.ai: the shared AI-stack module contracts

## Purpose

`core.ai` is the app-agnostic AI building-block package every Python app in this monorepo may
depend on (root ADR-0007): model/embedding factories, a generic capability registry, the
retrieval strategy axis with its tool ports and toolset builders, and the provider-agnostic
progressive-disclosure protocol. `core.cqrs` (the message-bus
kernel), `core.observability` (the logging/tracing facade), and `core.resolver` (the DI `Resolver`
port) live beside it as plain infrastructure, not AI concerns, and are covered here too since they
share the same "shared library, apps own their adapters" contract. This doc gives full
reference-level detail for `core.ai.disclosure` (the newest module, the progressive-disclosure
protocol). The sibling modules get a contract-level summary, see each module's own docstrings for
the full picture.

engram is `core.ai`'s reference consumer today (`engram/CLAUDE.md`). A change here ripples into
every consumer (`core/CLAUDE.md`'s ripple rule). The acceptance gate for any change is `core/`,
`engram/`, AND `workflows/` all green.

## Module map

| Module | What it is | Key files |
|---|---|---|
| `core.ai` (root) | Model/embedding factory re-exports (`create_model`, `create_azure_model`, `create_embedding_model`, `create_gateway_model`, `create_azure_openai_client`). `core.ai.models` also holds `chat_http_client` and re-exports `Thinking` | `ai/models.py` |
| `core.ai.embedding` | The embedding port contract and `QueryEmbedder`, the port that places one query in a knowledge base's vector space | `ai/embedding.py` |
| `core.ai.empty_answer` | `EmptyAnswerRetry`, the capability that asks the model to answer again, by name, when it reasons and then answers with only whitespace | `ai/empty_answer.py` |
| `core.ai.format_maps` | `FormatMaps`: a large text return of a configured tool answers with its format's map, and the tool gains a `part` argument that slices the run's cached copy. Stock outliners for `markdown`, `csv`, `listing`, `diff` and `json` | `ai/format_maps/{contracts,internal_contracts,domain,capability,toolset}.py`, `ai/format_maps/infrastructure/file_cache.py` |
| `core.ai.filesystem` | `UnnumberedFileSystem`: pydantic-ai-harness's `FileSystem` whose `read_file` returns each line without its number | `ai/filesystem.py` |
| `core.ai.tool_call_limit` | `ToolCallLimitWarning`: warns the model before a request once the run's tool calls reach 70 percent of the run's own tool-call limit (URGENT), and again once the calls left are at most the larger of 3 and a tenth of the limit (CRITICAL), as a request-scoped system prompt. The count is whatever the run's usage records as a tool call. With `counts_program_calls`, for an agent under `CodeMode`, the warning says a program's calls count and names how many more calls a program may make, one fewer than remain | `ai/tool_call_limit.py` |
| `core.ai.sandbox` | `SandboxDiscipline`: mounted beside pydantic-ai-harness's `CodeMode`, never around it. It states the sandbox's dialect as instructions: a program calls only the functions listed as available inside the sandbox and every other tool is its own tool call, a program is one top-level script already inside the host's event loop, `json.dumps` takes no `default=`, a generator expression is a list, there is no file system, and a program uses none of `dir`, `vars`, `callable`, `hasattr` and `getattr` and tests a value's kind with `type()` or `isinstance()`. It appends a way out to a `run_code` refusal of an unknown name that is not a Python builtin, in the runtime's voice or the type checker's: call a direct tool as its own tool call. A mount says what the agent has. With `loads_capabilities`, the dialect and the way out also send the model to `load_capability`, and without it the way out says the tool list and the sandbox's functions are all it can call. With `answers_a_person`, a spent `run_code` retry budget comes back as a result the model answers from, and without it the run fails on `UnexpectedModelBehavior` as it would with no discipline. No text names a person or `load_capability` for an agent that has neither. It adds no tools | `ai/sandbox.py` |
| `core.ai.content_filter` | `ContentFilterGuard`, the wrapper around every generation model the repo builds, `create_azure_model` and `create_gateway_model` included, which ends a turn with `ContentFilterError` for a response that finishes on `content_filter` and for a prompt Azure refuses with HTTP 400, `content_filter_message`, the sentence naming the categories the filter flagged, and `FilterReportingResponsesModel`, which keeps Azure's content filter results in a response's provider details. See below | `ai/content_filter.py` |
| `core.ai.history` | The conversation record behind the step-store port, plus `StepUsage` and `StepUsageTap` (one model response's cost as its `data-step` chunk carries it) and `recording_capabilities` (the step recorder and the request parameters recorder for one agent) | `ai/history/{contracts,internal_contracts,domain,exceptions,read_models,conversation_record,message_key,request_parameters_recorder,recording,step_usage}.py`, `ai/history/infrastructure/{checkpoints,database,postgres_step_store,queries,reads,rows,schema,writes}.py` |
| `core.ai.registry` | The generic capability/model registry | `ai/registry.py` |
| `core.ai.capabilities` | Capability descriptors (`AbstractCapability` seam, `SupportsInstructionBlocks`) | `ai/capabilities.py` |
| `core.ai.retrieval` | The retrieval strategy axis + policy, tool ports and toolset builders, the descriptor-driven SQL builders (`SearchSchema`/`GraphSchema` to `api.search`/`api.graph_*`: search legs bounded and read from a tuning row with rank policies, graph walks over a reached-node set with `graph_reach`, every function pinning `pg_catalog` first and `pg_temp` last), the guard that checks a model's SQL statement before it runs (`single_statement`, `RefusedStatementError`), and the token-based `vector_query` rewrite (`VectorQueryRewrite`) | `ai/retrieval/{strategy,policy,contracts,domain,internal_contracts,names,toolset,schema,search_builder,graph_builder,descriptions,statement,sql_tokens,unreadable_forms,vector_query_rewrite}.py` |
| `core.ai.evaluation` | The general outcome-evaluation kernel: provenance and resource-usage primitives, paired task-outcome comparisons with alternating variant order, immutable delivered evidence and the execution trace over it, owner-review events, and immutable local learning records over `JsonLearningStore`, generic over the execution type a comparison carries. `core.ai.inspection` re-exports only its own specializations, parametrized with its own execution type | `ai/evaluation/{contracts,internal_contracts,exceptions,service}.py`, `ai/evaluation/domain/{models,outcomes,evidence,trace,budget,reviews,learning}.py`, `ai/evaluation/infrastructure/{file_budget,file_evidence,file_reviews,file_learning,pydantic_ai_trace,pydantic_ai_usage}.py` |
| `core.ai.workflows` | The workflow engine: vocabulary (`MapNode.turns`, gates, wires), validator, builder to pydantic-graph, step identity, birth checks, trust evidence adapters, Temporal adapter (core ADR-0002), `copy_reusable_steps`, the one copy of a library's reusable steps into a workflow's `inline` that a check and a save with `uses` both call, and `fetch_reusable_step`, the one read of a reusable step by id that `fetch_step` and that copy share, which refuses an id no step has by naming `search_steps`. `step_content_address` is a reusable step's content address (the rule `spec_identity` follows, defaults left out), `canonical_step_yaml` is the same document as YAML, and `consumer_record` reports the saved version each copy of a reusable step matches, or the version a diverged copy diverged from. `automode_refusal` is what automode answers a judge gate with on both engines: a rejection credited to the judge carrying its note, or `JUDGE_NOTE_FALLBACK` when the note is blank. `LibraryCollection.put_step` keeps one step content per reusable step version (an unchanged step writes nothing, a different one raises `StepVersionChangedError`), links every live lower version forward to a new one, and saving the newest live version again links forward any live lower version a failed link left behind. It keeps the step's cases as JSON text. Cases given under an unchanged step replace a live version's cases in place, with no new version and no move of its content address. An empty mapping removes them, and no cases given keeps them. A version above every saved version of the name, live, superseded or archived, hands the ingest one `ReusableStepSaved` (the step's name, version, page id and content address, who saved it and the live lower versions it supersedes) with its page, through the `events` argument of the `Ingest` port, so the event reaches the outbox in the page's transaction or not at all. No other save passes an event. `StepVersionQueries` reads every saved version of a name with its content address (`step_versions`, `step_versions_of`) and a version's cases (`fetch_step_cases`). `LibraryCollection.put` writes a workflow's consumer record under `reusable_steps` on every save, read against every saved version of its inline steps in one query and the prior record (the page's own when the same version is saved again, else the lineage parent's), so a decline and a diverged copy's version carry over while the copy is unchanged, and never writes `uses`. It refuses to write over a superseded version or an archived one, and over a version on live trial with other content (an equal one saves again, keeping its stored marker when the caller passes none), and each refusal of an archived version or one on live trial names the next free version number. `next_version(name, above=, versions=)` is the one rule a new version of a name is numbered by: one above the version it is made from and every saved version of the name, archived and superseded ones included. Step maintenance's landing, `WorkflowContract`'s `adapt_workflow` (when its `versions` is set), the harness's authoring adaptation and its draft save, and these refusals all number by it, so an adaptation of a version a revert made live again never lands on the archived version above it. `ConsumerRecords` answers who holds a copy of a step (`consumers_of`, a `ConsumerCard` per live workflow version), the record one version was saved with (`fetch_consumer_record`) and the one write over it, `record_decline`, which refuses a superseded page. A maintenance flag (`MaintenanceFlag`, `FlagKind`) is a `maintenance-flag` page in the library, under the stable id `flag_id` mints, and `flag_to_write` is the one rule every writer follows: no page writes it open, an open page takes new evidence, a page a person resolved stays resolved while the step version's content address is the same, and a page one of `AUTOMATIC_RESOLVERS` (the library and step maintenance, whose provenance is `STEP_MAINTENANCE_AGENT`) resolved opens again on any new flag. `MaintenanceFlags` writes one by that rule (`put_flag`, which refuses an id holding another page type with `FlagIdTakenError`), lists them newest first as `FlagCard`s (`flags`, each card built by `FlagCard.from_flag`, the one mapping from a flag and its id to its card), reads one (`fetch_flag`) and resolves one with a name and a reason (`resolve_flag`, `FlagUnknownError` for no such flag). `put` raises a `diverged_copy` flag for each diverged copy, with the diff of `canonical_step_yaml` in its evidence, and resolves the open one of each step the save no longer holds diverged, archiving resolves those its version raised, a revert raises again those the version it makes live still holds, and each warns `FlagWriteIncompleteWarning` when a flag is not written, the save or the archive standing. An adoption on live trial carries a `LiveTrialMarker` under `live_trial`: every step version on live trial (`steps`, each a `LiveTrialStep`, each step name once), the version whose runs are the baseline (`previous`) and the runs that close it, and `with_steps` is the marker an adoption stacked onto it carries, its baseline kept and an earlier step of the same name replaced. `live_trial_verdict` reads one run of it as ignored, failed, worse or counting: failed when a touched node alone did not produce in a run that failed in execution (its outcome is failed), ignored when a person stopped it or it failed in execution anywhere else (a node a touched node wires from produced nothing, or every touched node produced), baseline or not, worse against the previous version's newest completed runs when a completed run, whose checked status of failed ranks lowest, has a checked status ranking below all of theirs, a gate both versions hold was rejected more often than in any of them, or a structural grade falls more than 0.1 below their mean for its measure, and counting otherwise, with an empty baseline leaving only failed and ignoring a completed run whose checked status is failed. `LiveTrialLibrary` saves the adoption with its marker (`put(..., live_trial=)`), reads it (`fetch_live_trial`) and reverts it (`revert`, which writes the version the marker names as `previous` back without its link, then archives the version, leaves a version between them superseded, carries onto the restored version the newest decline the retired versions recorded for each step off the marker, and refuses a version that is not the newest live one of its name or carries no marker). `LibraryCollection.saved_by` reads who saved one workflow version, as its page records it, superseded and archived versions included, and refuses an id no workflow page has with `WorkflowUnknownError`. `LibraryCollection.restamp` writes a new `fingerprint` on one workflow version and keeps every other key (the lineage, the live trial marker, the consumer record), the card and the body as stored, writes nothing for an equal stamp or for an empty stamp on a page that holds none (the store drops an empty stamp), and refuses a superseded version, whose successor link a rewrite would drop. `gate_rejections` counts each `reject` a run's gate decisions recorded once, whoever gave it, and both engines record it as `RunEvidence.gates_rejected`. Trust is counted per version and in total: `WorkflowVersionQueries.versions` lists every saved `(version, id)` of a workflow name, live, superseded or archived, in one query (`LibraryCollection` answers it), `version_hashes` maps the content address of every version that reads to its version number (the version asked about from the workflow given, the others read at once, one that does not read or whose workflow names another version than its id left out), `EvidenceLog.history_for_any` answers the events of a set of content addresses in one scan of the log, and `trust_across_versions` folds them into a `TrustAcrossVersions`: the current version's `TrustLevel`, the `total` over every version and each version's own record, lowest first, each event counted once under the version whose content address it carries. Its `level` is the current version's, and the total never sets it. `WorkflowContract.versions`, when set beside `evidence_log`, adds the total to the trust line of a workflow with more than one counted version, naming the version each approval was for | `ai/workflows/{contracts,internal_contracts,exceptions,read_models,service,registry,authoring,graph_builder,birth,voice,contract_port,reusable_steps,automode}.py`, `ai/workflows/domain/{vocabulary,parameter_values,run_events,identity,outcome,evidence,events,intent,fingerprint,consumers,flags,lineage,live_trial,validator,task,document,presentation}.py`, `ai/workflows/infrastructure/{folder,library_collection,evidence_log,temporal}.py` |
| `core.ai.disclosure` | The provider-agnostic progressive-disclosure protocol, see below | `ai/disclosure/{contracts,domain,capability,result_disclosure}.py` |
| `core.ai.artifacts` | Immutable value references, canonical bytes, and the resolver port, see below. Imports nothing from `core.ai.workflows` | `ai/artifacts.py` |
| `core.cqrs` | The message-bus kernel (bus, messages, unit-of-work) | `cqrs/{bus,messages,uow}.py` |
| `core.observability` | The logging/tracing facade (`get_logger`, `span`), plus `Tracing`, `start_tracing`, `PriceUpdater` and `start_price_updates` for a host that ships its own spans | `observability.py` |
| `core.resolver` | The DI `Resolver` port (apps own their adapters) | `resolver.py` |
| `core.thinking` | `Thinking`, the thinking levels a model is built at, with no dependency beyond the standard library, so a module that needs only the type loads no model client | `thinking.py` |

## The five records about a workflow run

Five records describe one piece of automation, and four of them are prose about the same work. A
reader who reaches for the wrong one gets a plausible answer to a question they did not ask, so the
first thing to know is which record answers which question. Three of the five are defined here.

| The question | The record | Defined in |
|---|---|---|
| What will actually run? | `Workflow`, the executable graph, hashed by `spec_identity` | `core.ai.workflows` |
| How is this put to a person? | `WorkflowPresentation`, on the library card | `core.ai.workflows`, below |
| What did somebody ask for? | `WorkflowIntent`, on the library card | `core.ai.workflows`, below |
| What did this run do? | `Outcome`, derived from the run's own facts | `core.ai.workflows`, below |
| Was the work any good, and did the owner take it? | a graded assessment, and `OwnerReview` | `core.ai.evaluation` |

Three rules keep them from collapsing into each other:

- **Only the graph is hashed.** A presentation and an intent live on the card, never on the spec, so
  rewriting either changes no identity and orphans no evidence. An edit to the graph mints a
  version, because it is a different thing to run.
- **Only the presentation is checked against the graph.** `verify_presentation` refuses a labelled
  input naming no parameter and an action naming no declared artifact. An intent is checked against
  nothing, because a request is not a claim about the graph, and it grants nothing: it selects no
  work, names no tool, and leaves the fingerprint's grants as they were.
- **Only the outcome is derived.** Nobody writes an `Outcome`. It is classified from item facts,
  gate decisions and the execution status, and it is never rewritten afterwards. Whether the work
  was worth having is a different record entirely, produced by a different layer, over bytes a
  resolver answered with.

## Workflow result declarations

`core.ai.workflows.contracts.Workflow` can declare the checked values a caller may use after a run through
its optional `result: WorkflowResultSpec`. An absent declaration is omitted from the serialized
spec, preserving existing workflow documents and their content identities.

- **`WorkflowResultSpec`** names the result with a nonblank `summary`, selects its `primary_work`
  map, records whether an empty primary result is `"noop"` or `"unknown"`, and lists required
  gate checks and selectable artifacts.
- **`RequiredGateCheck`** names a graph gate and the accepted decision sources:
  `"judge"`, `"person"`, or `"orchestrator"`. Validation confirms the named node is that gate
  kind and that its actual source is accepted.
- **`ResultArtifactSpec`** gives a stable artifact `id`, a reader-facing `label`, a graph `source`,
  and one supported media type: `"text/markdown"`, `"application/json"`, or `"text/plain"`.
  An optional `field` selects a named field from an object output. Text artifacts require a string
  field. A missing field, a non-object source, or an unknown graph wire is refused before a run.
  When `field` is absent, the artifact declares the source's whole value and serializes without a
  `field` key.

### The checked outcome of a run

`core.ai.workflows.contracts.observed_outcome` classifies what a run actually did. It is pure: item facts,
the active key set, gate decision facts, the execution status and the declared artifact references
go in, one `Outcome` comes out. It reads no report counter and no produced value, because a
non-empty value is not evidence that any useful work happened.

- **`WorkItemFact`** is one piece of one node's work settling, under the identity
  `(node_id, generation, item_key)`. `retry_attempts` counts how many times that identity was
  reworked before it settled, and is never folded into the work counts.
- **`MapBatch(generation, ordered_digest, items)`** is one resolved input to a node and the item
  keys it minted. An item key is `f"{generation}:{index}:{digest[:16]}"`, so two equal values at
  different indices are two distinct pieces of work. A node that does its work exactly once uses
  generation zero and the item key `""`. Rewinding onto an identical ordered input keeps the
  generation and every key, so a repair is a second attempt at the same work. Any changed value,
  reorder, addition or removal opens the next generation and leaves the old facts as audit.
- **`GateReview(generation, reviewed)`** is what one required gate's own `reviews` wire resolves to
  in the run's final values, and which reach it is on. A gate passes only when the last decision at
  that exact generation approves, names an accepted source, and carries the digest of those bytes.
- **`OutcomeCheck`** is one required acceptance: `expected_sources`, the `observed_source` actually
  on file (including `auto`, `timeout` and `unknown`), the generation and digest it was given over,
  and, for a refusal, the feedback, the requested repair and the reviewed reference.
- **`Outcome`** carries `status` (`succeeded`, `partial`, `noop`, `failed`, `unknown`), a `summary`,
  the counts (`attempted` is always `succeeded + skipped + failed`), `retry_attempts`,
  `primary_input`, the generation the counts belong to, the checks and the artifact references.
  Every count is an `int`, never absent. The counts are the pieces of the current generation that
  settled, also when the outcome is `unknown` because other pieces never did, as after a map aborts.
  They are `0` when the workflow declares no result, when the primary never took an input, and when
  two facts contradict each other. `primary_input`/`generation` are `None` in the first two of those.
  A consumer therefore reads the counts as numbers always, and reads "not known" off the status
  rather than off a missing count.

`status` is `succeeded` only when every piece of the current generation succeeded and every required
gate passed, `noop` only when the primary was handed nothing and the workflow declared
`empty_primary: noop`, `partial` when some succeeded and some did not, and `failed` when none
succeeded or a gate spent its last reach. Everything else is `unknown`: an incomplete generation,
contradictory facts, a run that did not complete, and a workflow that declared no result. `unknown` is
never rewritten into a verdict afterwards.

`RunEvidence.result` carries the `Outcome` beside the execution's own `outcome`, and `RunEvidence.provider_error`
says whether the run stopped because its model provider refused the reply or could not answer, as the runner that
saw the error read it (false when its engine does not say). `trust_level` folds
a completed run into `runs_with_evidence` only when its result is `succeeded` or `noop`. Every other
completed run counts in `unconfirmed_runs`, and a workflow that declares no result can never be more
than `unverified`. `last_evidence_at` is the time of the most recent run that confirmed, so it is the
age of the confirmation and not the age of the last run. Approval is unchanged and independent.

A run executed on the durable (Temporal) engine records `unknown` and stays there: its item
dispositions live in the workflow's own state on the server and nothing reads them back. A
deployment running on that engine therefore holds `unverified` for every workflow however often it
runs, and rises above `unverified` only through approval, or through runs on the in-process engine.

**What a consumer of `Outcome` and `ArtifactRef` owes.** `status == "succeeded"` is checked producer
success only: every piece of the declared primary work settled and every required gate's decision
covered the exact bytes the run ended holding. It is not a claim that the work was useful, and it is
not an owner's acceptance of it -- a layer that scores either records it as its own separate event,
never by reinterpreting `status`. A presentation surface built over these types offers an artifact
action only from a closed, first-party set of intents, each naming a fixed handler and route, never a
caller-supplied URL, method, or request body.

## Workflow arguments

A run is given one JSON value per parameter name. `WorkflowArguments = dict[str, JsonValue]` is
that shape, and it is what `WorkflowState.params`, `RunEvidence.params` and the Temporal
`RunRequest.params` all carry.

`normalize_arguments(spec, supplied)` is the one admission every launch path shares. Each declared
parameter's value is checked against that parameter's own `json_schema` with
`jsonschema.Draft202012Validator`, so every keyword the author wrote holds: bounds, item
constraints, `uniqueItems`, object requirements, combinators, and references into the schema's own
`$defs`. `format` stays an annotation and is never enforced. Validators are built once per distinct
schema and kept by the schema's content address.

What it does and does not convert:

- A JSON value is admitted exactly as it arrives. A boolean is never read as an integer, a number
  is never read as a boolean, and nothing is made to fit.
- Supplied text under a parameter whose schema cannot hold text is read once with `json.loads`, and
  the decoded value is then checked like any other. That is what lets a caller with nothing but
  strings (a stored schedule, a form) reach a parameter declaring an array, a
  number or a boolean. Whether a schema can hold text is read off the schema: its `type`, its
  `enum` or `const`, its local `$ref`, or its `anyOf`/`oneOf`/`allOf` branches.
- A string parameter keeps the string it was given, whatever the text looks like. `'true'` under
  `{"type": "string"}` stays the four characters `true`.
- Text that is not JSON under a parameter that cannot hold text is refused, never coerced. A Python
  repr such as `"['research']"` is a refusal.
- An empty string is a value, never an omission: it cannot satisfy `minLength: 1`.
- A declared parameter nobody supplied stays absent, except an optional one declaring
  `"type": "string"`, which arrives as `""` so the wire reading it resolves.
- An argument naming no declared parameter travels untouched. This admits values. It does not
  decide which arguments a workflow may be sent.

A refusal is an `ArgumentRefusedError` carrying `parameter`, the JSON `path` inside that value, and
the validator's own `detail`, so a caller can say which field and which item is wrong.

`ParameterSpec` checks its own schema when it is validated: `Draft202012Validator.check_schema`,
plus a refusal of anything that would have to be resolved elsewhere -- a `$ref` or an `$id` that is
not a fragment of the schema itself, and `$dynamicRef` in any form. A schema that fails either
raises `ParameterSchemaError`. Admission therefore reads nothing but the schema in front of it and
can perform no retrieval.

Membership of a first-party selector -- whether a name is one of the knowledge bases a deployment
is registered to read -- is not decided here and never will be. It belongs to the host holding that
registry, which checks it at its own presentation boundary after admission, against the control the
saved card declares (see `InputPresentation.control` below).

## Library presentation metadata

A library card (`core.ai.workflows.contracts.SavedWorkflow`) may carry an optional
`presentation: WorkflowPresentation | None`. It holds the words whoever wrote the workflow chose
for the surfaces a person meets it on. `None` means the card carries none, and no layer over these
types composes prose to fill the gap. `metadata` is unchanged and still carries whatever a
particular library knows that the protocol does not model. `presentation` is the validated half.

- **`WorkflowPresentation`** carries four prose fields (`purpose`, `produces`,
  `owner_participation`, `not_for`, each defaulting to empty), the labelled `inputs`, the
  `result_fields` a reader is shown, and the `actions` offered over the run's artifacts.
- **`InputPresentation`** gives one parameter a reader-facing `label` and an optional `control`.
  Its `key` must name a parameter the workflow declares. `control` is the closed set
  `InputControl = Literal["text", "textarea", "knowledge-base", "json"]`, so a host implements all
  of them and a card can never name one nothing knows. An unknown control is refused where the card
  is parsed, which is why no unknown selector can reach a launch. `None` leaves the choice to
  whatever reads the parameter's schema. `knowledge-base` is the one member that means more than a
  shape: it says the values are names of knowledge bases the host is registered to read, and a host
  holding such a registry checks them against it before a run starts. This lives on the card and
  never on the parameter, because `spec_identity` hashes the parameter schemas: a change to how a
  person is asked must not re-key the workflow or orphan its evidence.
- **`ResultFieldPresentation`** gives one recorded-output fact a `label` and a nonblank `path`.
  The path addresses a recorded outcome for display only: it selects no work, grants no access,
  and is never resolved against the spec.
- **`SaveNoteAction` and `RecordOwnerReviewAction`** are the whole artifact-action vocabulary,
  discriminated on `kind` as `ArtifactActionSlot`. Each names a first-party intent the host
  already implements. There is no endpoint, method, path, or request body to supply, and
  `extra="forbid"` refuses one that tries. `SaveNoteAction.destination` is `"user_selects"` and
  nothing else: the action opens the artifact for the person to place, it does not write anywhere
  on its own.
- Every constrained string is stripped on the way in, so a value of nothing but spaces is refused
  as blank and the stored value is the one validation checked. An action needs an `id` that is
  nonblank after stripping and unique within the presentation, a review must name at least one
  artifact, and two inputs may not label the same parameter. Those are parse-time facts, so an
  unaddressable action never reaches a store.

`verify_presentation(spec, presentation)` is the gate that needs the spec: it refuses a labelled
input naming no parameter, and an action naming an artifact the workflow's `result` does not
declare, raising `PresentationRefusedError` and listing every one. `save` and both shipped
library adapters (`FolderLibrary`, `LibraryCollection`) run it before they write anything, so a
refused presentation leaves no file and no page.

Reads are deliberately the other way round. A stored presentation that no longer parses reads as
absent, the same rule `fetch_fingerprint` and `fetch_lineage` already follow: a hand-edited file
or page must not take down the listing that would have shown it. A card saved without a presentation
parses as `None` and re-saves byte for byte.

### The intent: what the person asked for

`WorkflowIntent` is the second optional record a card may carry (`intent: WorkflowIntent | None`),
and it answers a different question from the presentation beside it. The presentation is what a
reader is shown. The intent is what was asked for: the `request` in the person's own words, the
`expected_result` they expect back, one `success_example` that would count, `empty_input_behavior`
and `failure_behavior`, whether the trigger is `manual` or `scheduled`, and the `assumptions`
somebody made on their behalf. Only `request` is required, because an incomplete statement is the
normal state of one still being written.

`intent_issues(intent)` names which of `expected_result`, `success_example`,
`empty_input_behavior` and `failure_behavior` are still blank, in the order they are asked for. It
reads four of the six optional fields and neither `trigger` nor `assumptions`, which have defaults
rather than blanks. It is the only function `core` offers over an intent at all: a pure
`tuple[str, ...]` of field names, raising nothing, and the whole of what this library will say
about an intent, because there is no validator, no gate and no grade behind it. Nothing here
promotes an intent into a presentation or back either. The four sentences a card is described by
are the author's, the request is the asker's, and a surface that showed one in place of the other
would be answering the wrong question.

Both records read the same forgiving way a fingerprint does. A stored intent that no longer parses
reads as absent rather than taking down the listing that would have shown it, and a card with no
intent parses as `None` and re-saves byte for byte.

### Writing a presentation onto a workflow that already exists

`WorkflowLibrary.redescribe` takes an optional `presentation`. Absent, the library keeps whatever
words it already holds, so a caller rewriting a title cannot silently drop the author's sentences.
Given, it replaces the kept presentation whole, after `verify_presentation` has checked it against
the stored spec, and a refusal leaves the file or page exactly as it was. Both shipped adapters
implement it. This is what lets a workflow saved before anybody described it be described later,
without minting a version nobody asked for: a spec edit is still an adaptation, and this is still
only the prose a chooser reads.

### The host's own bar: `SaveGuard`

`SaveGuard` is a `Protocol` in `core.ai.workflows`, taking `(workflow, title, summary, applicability,
presentation)` and answering the refusal text or `None`. What a keepable workflow is belongs to
whoever runs the library, so nothing in `core` decides it: a host that will not keep an
undescribed workflow installs a guard, and a host with no such rule installs nothing and sees no
change.

`WorkflowContract` holds one as an optional `save_guard` and asks it at the top of
`_verified_write`, which is the single path both `save_workflow` and `adapt_workflow` write
through, so neither tool can land what the host's own save route would refuse. Installing a guard
also stops the port deriving a blank card field from the spec (a title composed there would walk
straight past a bar set to catch exactly the workflow nobody bothered to name). Both write tools
take `purpose`, `produces`, `owner_participation` and `not_for` beside the card fields, and the
four travel to the library as a `WorkflowPresentation` on `WorkflowWrite`. A call that writes none
of them sends `None`, which records that nobody described the workflow rather than four blanks
that read as an answer. The tool count is unchanged.

## Artifacts: `core.ai.artifacts`

An `ArtifactRef(id, label, run_id, node_id, turn, value_digest, media_type)` is an address plus the
digest of what lives there. It carries no URL and no transport: whoever holds one asks a resolver,
and the resolver answers with the exact bytes the digest names or refuses. A reference is minted
only after the value it names is fixed somewhere a reader can reach again.

Canonical bytes are the whole contract:

```python
json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
```

Every identifier inside a reference (`id`, `label`, `run_id`, `node_id` and `turn`) is an
`ArtifactText`: trimmed, never blank, and at most 200 characters, one bound for all five.

`canonical_bytes` raises on a nonfinite number, `artifact_digest` is the SHA-256 of those bytes in
lowercase hex, and `MAX_CANONICAL_BYTES` is 1,000,000. `ArtifactDocument(ref, value)` checks all
three on construction: a text media type (`text/markdown`, `text/plain`) carries a string, the
canonical bytes stay under the ceiling, and their digest is the one the reference addresses. A
document that exists is therefore a document a holder need not re-verify. `ArtifactResolver` is the
one-method async port over it, raising `ArtifactUnavailableError` when nothing answers.

`ResultArtifactSpec.field` selects one declared top-level property of an object source, never a
dotted path or an expression. `None` selects the whole source and is omitted from serialization so
existing spec hashes stay pinned.

### What a holder of a reference owes

A reference is an address, not a value. Anything that shows a run's output to a person, or writes a
judgement about it down, resolves every `ArtifactRef` it holds first and reads the resolved
`ArtifactDocument`. Quoting the label, the digest or a value carried alongside a reference is not
resolving it, and neither is resolving one reference and assuming the rest.

This binds the evaluation surface in particular: an owner review is appended to what the resolver
answered, never to a value the caller already had. An unresolvable reference is a refusal to review,
not a review of nothing. That is what makes a recorded judgement a judgement of bytes anyone can
re-derive, rather than of whatever happened to be in front of the reviewer.

`referenced_steps(workflow)` is the workflow engine's complete executable-work traversal. It returns
run work, every map turn, and judge work in first-appearance order with duplicates removed.
Fingerprinting uses that traversal, so every registered implementation the graph can execute is
included in its environment stamp.

## Delivered evidence and the execution trace: `core.ai.evaluation`

A grade is only as honest as the record it is made from. `core.ai.evaluation.domain.evidence` and
`core.ai.evaluation.domain.trace` fix what that record is: the exact bytes a trial's tools handed the
model, and what each tool call and each model turn cost to get them.

### What is stored, and when

`DeliveredEvidenceStore.append(trial_id, ordinal, payload_json, sources=...)` takes the payload
after every bound the tool applies to its own answer and before the model reads it. The bytes stored
are the bytes the model receives, hashed as stored, and `DeliveredEvidenceRef` names them:
`evidence_id`, `ordinal`, `payload_sha256`, `payload_bytes` and the `DeliveredSource` entries the
payload carried. `DeliveredEvidenceReader.read(ref)` answers with those bytes or refuses. It never
falls back to a source, so a source edited after the trial cannot change what the trial is graded
on.

A `DeliveredSource` records `source_ref` plus a nullable `source_content_sha256` and
`source_version`. Both stay `None` unless the result itself supplied them. Neither is ever derived
from a snippet or a fetched body, because a hash over a bounded excerpt is not the source's hash.
A grade that depends on source integrity reads `unknown` when they are absent rather than assuming
the source was intact.

### The two bounds, and what crossing one means

One payload is at most `DELIVERED_PAYLOAD_BYTES` (262,144) and one trial at most
`TRIAL_DELIVERED_EVIDENCE_BYTES` (1,048,576) of accumulated payload bytes. Crossing either raises
`DeliveredEvidenceBoundError` and invalidates the trial. Nothing is truncated to fit: a grader
reading a shortened payload would be grading evidence the model never saw, which is worse than
having no trial at all. Nothing partial is written for a refused payload either.

### Immutability

Appending the same trial, ordinal and bytes again returns the reference already filed and does not
admit those bytes toward the trial bound a second time. Appending anything else under that same
identity raises `DeliveredEvidenceConflictError`, whose message names what differs: different bytes,
a different byte count, or the same bytes with different sources. A read whose file is missing, whose
byte count or hash differs, which the trial's own manifest does not file, or whose manifest no longer
parses at all raises `DeliveredEvidenceUnreadableError` rather than returning partial content. An IO
failure at either end raises `DeliveredEvidencePersistenceError`.

All five errors are `EvaluationError` subclasses, and they are the only errors a stored record can
raise. A caller folding one trial's evidence to unknown catches `DeliveredEvidenceError` and is done:
no filesystem error and no validation error escapes the store, so one unreadable trial cannot crash a
comparison. Calling the store with an argument it refuses outright, a negative ordinal say, is a
caller's own mistake and raises as one.

`FileDeliveredEvidenceStore` is the adapter over one POSIX state root shared by co-located
processes. Each trial owns a directory named by the SHA-256 of its id, holding one
`<payload_sha256>.json` per payload, a `manifest.json`, and a `.lock`. A payload's bytes are written
and flushed to a temporary file in that directory and linked into place only once every byte is on
disk, so a crash part way through a write leaves a stray temporary rather than a short file at the
content address, and the next append of those exact bytes still succeeds. Linking fails when the
address is taken, which is what keeps the write-once property. Every append holds the trial's
cooperative lock across reloading the manifest, checking both bounds, creating the payload and
swapping the manifest in, so concurrent appends to one trial accumulate against one running total
rather than each against a stale copy. Trials never share a lock, so concurrent trials under one
root keep independent ordinals and independent byte totals.

### The trace

`ToolCallTrace` and `ModelTurnTrace` are separate on purpose. A tool call records its name,
position, `status` (`ok`, `refused` or `failed`), the evidence it delivered, the references that
evidence carried, its serialized size, its duration, and `withheld_exposure`. A model turn records
input, output, cache-read and cache-write tokens, its duration, its model and provider names, and
`cost_usd`, which stays `None` whenever the price cannot be resolved. No model turn's price is ever
attributed to a tool: `ToolCallTrace.direct_cost_usd` is for a tool billed directly, and is `None`
for a retrieval tool.

`ExecutionTrace` holds both halves plus `run_duration_ms`, and refuses two rows claiming one
position. `ExecutionTrace.withheld_exposures` names every call whose own arguments reached for a
withheld reference. A trial with any exposure is contaminated however well it otherwise scored.
`TaskExecution.trace` is `None` when the run was not traced, which is not the same as a run that
made no tool calls.

`TraceSink` is the port a running trial writes through. It hands out positions as well as collecting
rows, so concurrent tool calls in one trial get distinct ones from a single place. `TraceRecorder`
is the in-memory sink for one trial. `TracingModel` is the pydantic-ai adapter that times each plain
model request into a turn. A streamed request passes through untraced, so a run that wants model
turns in its trace runs its model without streaming.

## The evaluation budget: `core.ai.evaluation.domain.budget`

A live evaluation spends real money across processes that share nothing but a directory. `budget`
is the record that admission and settlement go through, and `infrastructure/file_budget.py` is the file
behind it.

### Cycle, trial, request

A **cycle** covers one whole evaluation and holds the `EvaluationLimits` it runs under. A **trial**
is one attempt at one case inside it, with its own start, its own deadline, its own attempts and its
own requests. Every model request any role makes belongs to exactly one trial through a
`BudgetScope(cycle_id, trial_id)`, so a designer, a worker, a reviewer and a grader in four
processes spend one budget rather than four.

`EvaluationBudgetRecord` has five verbs. `begin_cycle` opens a cycle and refuses
`pricing_known=False` outright. `begin_trial` opens a trial and sets `deadline_at` to its start plus
`trial_deadline_seconds`. `begin_attempt` counts one attempt at the trial's work. `admit` asks for
one request and persists it before the caller may make it. `settle` records what the response
actually cost. Re-opening a cycle or a trial is accepted only as the identical call: different
limits, a different root or a different start are a `BudgetConflictError`, never a reset of the
counters.

### What admission refuses, and why

`admission_refusal(state, scope, at=...)` is the whole rule, as a pure function over the recorded
state, and every adapter asks it rather than reimplementing it. It names one of
`request_limit`, `deadline`, `trial_cost_stop`, `cycle_cost_stop`, `request_in_flight` or
`unresolved_charge`. Count, deadline and attempt are hard rules. The two dollar rules are not.

### Accrual, not a ceiling

The dollar figures in `EvaluationLimits` are **accrual stop thresholds over measured
generative-model response charges**. A request admitted below a threshold may cross it, because
nothing can refuse a charge a provider has already earned. The record stores the actual total and
`max(total - threshold, 0)` as overshoot, and refuses every later admission. One request at a time
is in flight across the whole cycle, which is what bounds the overshoot to one priced call. A
`BudgetSnapshot` reports the trial and cycle spend, both overshoots, the in-flight request id and
whether any charge is unresolved.

An unresolved charge is a charge nothing could price. It is not zero, it is recorded as
`charge_status="unresolved"` with no figure, and it blocks every later admission. Neither an
unresolved charge nor an in-flight request is ever cleared by restarting: a process killed between
admitting and settling leaves a request pending, and the next process is refused rather than handed
a clean slate.

### Measured model charges only

`trial_measured_model_spent_usd` and `cycle_measured_model_spent_usd` count settled generative-model
response charges and nothing else. An embedding or third-party charge is an `OtherCostObservation`
of its own, with `cost_usd=None` when nothing could price it. `other_cost_total` returns `None` for
a set with any unknown in it, so a report cannot add an unknown as zero. No comparison may state a
full invoice cost while those observations are unknown.

`BudgetCoverageManifest` is what proves the total means anything: `require_full_coverage` refuses a
manifest naming any `unmetered_model_paths` at all, and refuses unless
`set(generative_model_paths) == set(budgeted_model_paths)`. An external CLI agent, or any model path
a budgeted wrapper cannot cover, refuses the trial rather than being measured around.

### `FileEvaluationBudgetRecord`, and where it is supported

One cycle is one document, `<state_root>/evaluation-budgets/<sha256(cycle_id)>.json`, beside its own
`<sha256(cycle_id)>.lock`. A cycle id never becomes a path segment. Every mutation takes that
cycle's cooperative lock, reloads and validates the document, applies the transition, writes the
whole new document to a temporary file that is fsynced before it is swapped in, fsyncs the directory
and only then releases the lock. Two processes admitting at the same moment serialise, and the
second sees the first's request in flight and is refused.

It is supported for **co-located processes on one verified POSIX device and canonical state root**.
The cycle records both when it opens, and every mutation re-checks them, so a process that mounted
the same path elsewhere is refused with `BudgetPreflightError(refusal="unverified_state_root")`
rather than trusted. A remote worker that cannot verify the device is refused before execution. No
claim is made about a network filesystem.

Failures are typed and are the only ones a caller has to handle: `BudgetRefusedError` (admission
refused, carrying its `refusal`), `BudgetPreflightError` (refused before the trial ran, carrying its
own `refusal`), `BudgetConflictError`, `BudgetNotOpenError`, `BudgetUnreadableError` and
`BudgetPersistenceError`. All of them are `EvaluationError` subclasses.

## Continuation checkpoints: `core.ai.history.contracts`

A durable run schedules each turn of a multi-turn map as its own activity, possibly on another
worker. The conversation those turns share therefore cannot live in a process. `core.ai.history`
carries it instead, in two additive tables the existing `create_record_schema` bootstrap owns:
`continuation_attempts` (one row per logical attempt at one turn) and `continuation_heads` (how far
each conversation has been carried). There is no Alembic revision, and the package imports nothing
from `core.ai.workflows`: a conversation here is a uuid, an attempt is a string, and what they
address is a run and a step of the record.

`ContinuationCheckpointStore` is the port, with four moves:

| Move | What it does |
|---|---|
| `begin_attempt(attempt)` | Claims one turn against the revision it believes is current. `None` admits it. A `ContinuationCommit` is the output an identical attempt already settled, and the turn runs no model call. |
| `read_checkpoint(ref)` | The exact step the ref names, verified against the conversation and the digest it travelled under. |
| `commit_attempt(attempt, ...)` | Settles the turn against the step it wrote and carries the conversation one revision forward. |
| `latest_checkpoint(run_id)` | The newest settled step of one run: the sequence a commit points at, and the digest of what it holds. |

**The record is the only authority on messages.** Neither continuation table holds a transcript, and
neither does anything crossing an activity boundary. A `ContinuationRef` is an address plus a
digest: `(conversation_id, revision, run_id, snapshot_seq, checkpoint_digest)`. The messages behind
it stay in the `messages` and `steps` tables the step store already writes, and a read addresses one
step by `(run_id, seq)` exactly, never by step index and never by "the latest".

**Bounded payloads are contracts.** `CONTINUATION_OUTPUT_BYTES` is 1,000,000 and
`CONTINUATION_REF_BYTES` is 512. A commit canonicalizes its output before it touches the database
and refuses a larger one with `ContinuationPayloadTooLargeError`. A ref that serializes past its
ceiling is refused rather than returned.

**Transactions are short and hold no model call.** Begin opens one transaction, takes
`pg_advisory_xact_lock` on the conversation, reads the attempt, refuses any other unfinished
attempt, checks the head, inserts one started row, and commits. The lock is gone before the turn
runs. Commit opens a second transaction, takes the same lock, re-checks the head, verifies the step,
writes the settled row, and advances the head with a predecessor-revision predicate that must affect
exactly one row.

**Uncertain effects are refused, not guaranteed away.** This does not promise exactly-once external
effects. It returns the stored output after a crash that followed a commit, and it refuses an
automatic second run in three cases: `ContinuationInFlightError` when another attempt on the
conversation is unfinished, `ContinuationUnsafeReplayError` when this attempt itself began and never
settled, when the checkpoint was saved interrupted, or when the run left a tool call `started` in the
record's tool effects, and `ContinuationConflictError` when the conversation is not where the attempt
believed it was. A refusal names what is uncertain. It never claims a guarantee.

`run_id` is plain text on the attempt row on purpose: begin happens before Pydantic AI registers the
run, so a foreign key there would point at a row nothing has written yet. Commit joins the
registered run and refuses a mismatch. Deleting a conversation drops its head and its attempts
before the runs those attempts name.

## Worker context: tool and skill unions

A multi-turn `MapNode` gives one worker one conversation per item, and every turn of it is asked
with the same standing surface. `TurnFacts` carries two unions for that, computed once per map from
the briefings its `turns` name: `tools`, and `skills`. `TurnOnTheWire` carries both across the
activity boundary, `skills` empty when the turns hold none. Neither union reaches the identity a durable line is addressed by: a conversation id and an
attempt id are derived from where a turn stands, never from what it holds, so mounting one more
skill cannot orphan the checkpoints a line has already committed.

**Why the union rather than the turn's own declaration.** A skill mounts as a capability the model
is told about in its instructions, and a tool the sequence needs later changes the tool list. Picking
either per turn rewrites the request prefix between turns of a conversation that exists to keep one:
the provider's cache reads collapse, and the model is told mid-conversation that a capability it was
offered is gone. The union is bounded to that one item sequence and stays subject to the host's
grants: a step still holds only what the run offers, and two unrelated workers in one run share
nothing.

**Sharing a conversation and unioning capabilities up front are the defaults, not rules.** Split the
conversation, and with it the surface, when one of four things is true:

| Exception | Why a split beats one conversation |
|---|---|
| Permission change | A later turn holds reach an earlier one must not have. Unioning would hand it over from turn one. |
| Independent criticism | A critic that sat through the drafting tends to agree with it. A fresh conversation is what makes a second opinion a second opinion. |
| Irrelevant context | A later turn works on something the earlier turns say nothing useful about, and carrying them only crowds the window. |
| Retry isolation | A rejected turn is repaired against the refusal, and the failed attempt in the history is what the repair is trying not to repeat. |

**The engine's own voice names the whole grammar.** `process_instructions(check_tool=...)` renders
the process an authoring agent reads, and it names all five node kinds -- `run`, `map`, `gate`,
`judge` and `orchestrator-gate` -- because a kind the voice never names is a kind nobody authors. It
claims nothing about any host's registry: the engine cannot see one, so a host renders its own live
inventory beside this text, off the registry and off the registry `prepare` built for the draft in
hand.

**Deciding between them takes measurement, not preference.** A workflow that changes how its turns
share context reports four figures before and after, together: cache read tokens, total tokens, cost,
and output quality on the same inputs. One of them moving alone settles nothing -- a shorter prompt
that answers worse is not an improvement, and a higher cache-read rate paid for with a wider tool
surface is not either.

## A turn whose model connection broke

The model endpoint can close a streamed answer part way through. pydantic-ai then raises `ModelConnectionError`, and
nothing below the engine sends the request again. The HTTP retry of `chat_retry_config` covers only a connection that
fails before a response begins, and `FallbackModel` hands a stream over after its first event and lets a later failure
through.

The engine performs such a turn again. Every turn it runs goes through one helper in `graph_builder.py`: a run node's
step, each turn of a map item, a judge gate's step, a person gate's answer, a judge's escalation when it cannot judge,
and an orchestrator gate's decision. A gate a person answers never raises `ModelConnectionError`. One a host answers with
a model, as the harness does under its `orchestrate` gate mode, gets the same attempts as any other turn. A turn that
fails with `ModelConnectionError`, its subclass `ModelTimeoutError` included, is performed again with the same arguments
under the same `StepIdentity`, three attempts in all. The waits between attempts are 2 and 4 seconds, through the `sleep`
that `build_graph` and `run_workflow` take (`asyncio.sleep` when none is passed). Each dropped attempt is logged at
warning level with the node id, the item key when there is one, the attempt number and the error.

- A `ModelConnectionError` whose `phase` is `pool` or `connect` gets one attempt. Its request never left for the
  provider, and `chat_retry_config` has already retried it for up to 300 seconds. Every other phase, `None` included, is
  retried.
- Only the last attempt's failure counts. It reaches the map's `on_error` and its `abort_after_repeats` streak exactly
  as any failure did before, and an attempt that succeeds is a success.
- The observer sees one turn: one `started` and one `finished`, whatever happened between the attempts.
- A turn that fails any other way gets one attempt, as before.
- Cancelling the run during a wait ends it there, with no further attempt.
- A host that keeps a step conversation starts every attempt from the history the turn began with. The harness keeps
  each attempt that lost its connection on the line apart from that history (`StepConversation.dropped_attempts`): the
  run archive files it where it began, ahead of the attempt that settled its turn, found by how many user prompts the
  history held rather than by the prompt's words, its model responses reach the run's spend rows, and a steer it had
  taken in goes back on the line's inbox for the next attempt.
- An attempt's effects are not undone. A tool the broken attempt ran has run, and the next attempt may run it again. That
  is safe as long as every write a turn can make lands the same when made twice. Every step file under
  `harness/workflows/` and `templates/*/workflows/steps/` keeps to it: the write tools an agent turn holds replace a page
  by its id (`write_note`, `decide_page`, `skip_digest`, and `digest_skeleton`'s id for a source digested again) or
  find what an earlier call made (`resolve_entities` by name, `request_source` by URL). A program step's tool error ends
  inside its sandbox as a `RuntimeError`, never as a `ModelConnectionError`, so a program is never performed again, and
  a tool that appends, such as `resolve_note` with a reply, is safe in one. A new step that grants an agent turn a write
  that appends or counts breaks this.
- `WorkItemFact.retry_attempts` does not count these attempts. It counts how many times an identity settled again after a
  rewind, and every attempt at a turn belongs to one settle.
- A step a durable run schedules as an activity has Temporal's own attempts, and its failure reaches the engine as
  Temporal's `ActivityError`, which the engine never performs again.

## A model response a content filter cut

Azure's content filter can cut a model's answer. The response then finishes on `content_filter`, and it may still carry
text, such as the stock sentence "I'm sorry, but I cannot assist with that request." pydantic-ai raises
`ContentFilterError` by itself only for a filtered response with no text. Otherwise it validates the text as the run's
output, and with a structured output type every output retry is spent on a request the filter refuses again.

Every model `create_azure_model` and `create_gateway_model` build, `TestModel` aside, is a `ContentFilterGuard` around
the model itself, and so is every model a host builds on its own (the harness's OpenRouter models, the connectors' xAI
model). Any response that finishes on `content_filter`, whatever text it carries, ends the turn with
`ContentFilterError`. A streamed request raises it when its body exits. A request hands the response back with its
parts removed, so the agent records its usage like any other response's and raises `ContentFilterError` itself. A
caller of `request` outside an agent run gets that empty response. The response never reaches output validation and no
output retry is sent for it. Both engines perform the turn once. The in-process engine performs a turn again only when it
lost its model connection, and `ContentFilterError` is not a `ModelConnectionError`. The Temporal engine's default retry
policy lists `ContentFilterError` among the errors it never retries. A durable step a filter ends fails its activity with
an `ApplicationError` typed `ContentFilterError` (`CONTENT_FILTER_FAILURE`), whose message is `content_filter_message`'s
sentence and whose detail is the response, and workflow code raises `ContentFilterError` again from it. A run it ends
fails with that same `ApplicationError`.

- `content_filter_message(error)` answers one sentence read from the response in the error's body. It opens as
  pydantic-ai's own (`Content filter triggered. Finish reason: 'content_filter'.`) and ends by naming each filter
  category that filtered or detected, with its source and severity, for example
  `Filter results: completion violence filtered at severity medium.` When the response holds no filter result, it says
  so. On either engine, a run node or a map item that fails this way records that sentence as its failure reason, a
  skipped map item records it among the run's skipped reasons too, and the chat shows it whole. One that fails with any
  other `UnexpectedModelBehavior` records the error's message without its body in the same places.
- The error's body is the whole response as JSON, provider details included, and its text too when it was streamed.
- The response stays in the run's history with its usage: a streamed one marked `interrupted` with its text, a whole one
  with no parts.
- A prompt Azure's content filter refuses answers HTTP 400 with the error code `content_filter`. pydantic-ai turns that
  into a filtered response only for an `AzureProvider` (UB-19), so the guard raises `ContentFilterError` for the
  `ModelHTTPError` at once, with the provider details pydantic-ai would have built, and the sentence names the categories
  the refusal lists.
- The Responses models are `FilterReportingResponsesModel`. Azure sends its filter results as a top-level
  `content_filters` list the OpenAI SDK has no field for, and this model keeps that list, as sent, in the response's
  provider details under `content_filters`, streamed or not. pydantic-ai keeps the result of a filter that refused the
  request itself under `content_filter_result`, and the error names that too.
- `decision_model` builds a decision model, which is not guarded.

## Disclosure: `core.ai.disclosure`

### Purpose

The provider-agnostic progressive-disclosure protocol: context machinery, not knowledge-base-specific.
Any provider of a progressive-disclosure surface (engram's indexed knowledge base is the first)
returns the same `DisclosureEnvelope` shape for one disclosed unit, whatever the knowledge base behind it,
and any pydantic-ai agent holding a provider's descent tools can opt into the same governance
(descent-protocol instructions plus a per-run token budget) via one capability.
This doc is the shipped contract.

### Entry points

| Surface | Entry | Defined in |
|---|---|---|
| Contracts | `DisclosureEnvelope`, `ChildSummary`, `DisclosureMode`, `DisclosureStatus`, `approx_tokens`, re-exported by `contracts.py` | `core/src/core/ai/disclosure/domain.py` |
| Capability | `ProgressiveDisclosure` (a pydantic-ai `AbstractCapability`) | `core/src/core/ai/disclosure/capability.py` |
| Result disclosure | `ResultDisclosure` (a pydantic-ai `AbstractCapability`), `Binding`, `Disclosable` | `core/src/core/ai/disclosure/result_disclosure.py` |
| Package re-export | `from core.ai.disclosure.contracts import Binding, ChildSummary, Disclosable, DisclosureEnvelope, DisclosureMode, DisclosureStatus, ProgressiveDisclosure, ResultDisclosure, approx_tokens` | `core/src/core/ai/disclosure/contracts.py` |
| engram's provider | `EnvelopeHandler` (the MCP `fetch` tool, which reads by level when an envelope handler is wired, and the `GET /api/envelope` HTTP route) | `engram/src/engram/retrieval/envelope.py`, `mcp_server.py`, and `studio/src/studio/knowledge_base/http/api.py`, see [serve.md](../../../engram/docs/reference/serve.md#public-contract) |

### Public contract

- **`DisclosureEnvelope`** (frozen pydantic model), one disclosed unit: `ref` / `kind` / `title`
  identify it, `summary` and `content` carry its disclosed body at whatever depth the caller
  asked for, `children` are `ChildSummary` stubs a caller can disclose further, `mode`
  (`DisclosureMode`) records the mode it was rendered under.
- **`ChildSummary`** (frozen pydantic model), one child stub: `ref` / `kind` / `title` / `summary`,
  enough to decide whether to disclose it further (never its body).
- **`DisclosureMode`** (`Literal["strict", "auto", "search_only"]`), `strict` returns exactly the
  requested depth, `auto` lets the provider widen or narrow it, `search_only` returns child stubs
  with no body (the shape a budget-reached stub uses).
- **`DisclosureStatus`** (`Literal["active", "superseded", "outdated", "pending"]`), the shared
  vocabulary a provider-side unit's lifecycle MAY be drawn from. **This is shared vocabulary, not
  an enforced contract** (slice-1 honesty note, see the module docstring): no field on
  `DisclosureEnvelope` or `ChildSummary` carries a status today, and a provider may legitimately
  serve non-`active` nodes with nothing in the envelope shape distinguishing them. engram's
  `EnvelopeHandler` deliberately does not status-filter its collection/page queries. Consumers must
  not assume every disclosed or listed node is `active`. Envelope-level status filtering is
  deferred to a later slice.
- **`kind`** on an envelope is the provider's real per-unit type where one exists, not a fixed
  literal, for engram's page envelopes it is `kb.pages.page_type` verbatim (e.g. `"memory-unit"`),
  while a collection envelope's `kind` is always `"collection"` and a section envelope's is always
  `"section"`. A consumer that needs to know "is this a page" tests the ref prefix
  (`ref.startswith("page:")`), never `kind == "page"`.
- **`approx_tokens(envelope)`**, the shared cheap budget estimate: `len(envelope.model_dump_json())
  // 4` (~4 chars/token), the metric the capability's budget wrapper meters against.

### The `ProgressiveDisclosure` capability

`ProgressiveDisclosure` is the "guaranteed" enforcement tier, a thin, `@dataclass`
`AbstractCapability[AgentDepsT]` (modeled on engram's `SqlCorpus`) that ships **no content** of its
own (no summaries, no storage, no provider vocabulary), only three provider-agnostic concerns:

1. **Orientation**: `get_instructions` / `instruction_blocks` teach the model the descent
   convention (start at the root ref, read `summary`/`children` before `content`, descend
   selectively). The shared mechanics text is deduplicated across instances via a keyed
   `instruction_blocks` entry (`core.ai.capabilities.Multiplex` prints it once however many
   disclosure-capable providers an agent holds). Each instance appends its own tool name and
   budget as an unkeyed block.
2. **Budget (count-then-check, not check-then-count)**: `get_wrapper_toolset` wraps the agent's
   assembled toolset in a fresh per-run `_EnvelopeBudget` (`WrapperToolset` subclass). Its
   `spent: list[int]` is built fresh per run (never shared across runs) and appended to, never
   reassigned, so a `WrapperToolset` dataclass-copy (`for_run`/`for_run_step`) shares the same underlying list. Every
   conformant result's `approx_tokens` is recorded via `spent.append(...)` **first**. Only then does
   the wrapper check whether `sum(spent) > max_envelope_tokens`. If that recording pushed the run
   over budget, **that same result** (not the next one) is replaced by a budget-reached stub in
   `search_only` mode. This means a single envelope larger than the whole budget is stubbed on its
   own first call (it is still recorded, then immediately found over budget), and one that exactly
   fits still passes. The budget bounds what actually enters context, never what came before it.
   - **`ToolReturn` handling:** a provider is conformant if its tool returns a bare
     `DisclosureEnvelope`, OR a pydantic-ai `ToolReturn` whose `return_value` is one
     (`_unwrap_envelope`). `ToolReturn.content`/`.metadata`/`.tools` ride along unmetered on the way
     through for a result under budget. On a stub substitution they are DROPPED along with the rest
     of the original result. The stub is re-wrapped in a fresh `ToolReturn(return_value=stub)` only
     when the original result was one, so the return shape (bare vs. wrapped) is preserved, but none
     of the original's extra fields survive a stub swap. Any other result shape (no `DisclosureEnvelope`
     anywhere) passes through untouched and uncounted. The wrapper recognizes a disclosure exchange
     by the RESULT's shape, never by which tool name produced it.
   - **Per-run state:** the budget wrapper is built once per run inside `get_wrapper_toolset`
     (called once per run, after `for_run`), so `spent` and the cumulative spend never leak between
     runs or between two `ProgressiveDisclosure` instances on the same agent (each provider's
     capability gets its own wrapper and its own `spent` list).
3. **Trace (the `observability.span` seam)**: every call through `_EnvelopeBudget.call_tool` runs
   inside `core.observability.span("disclosure tool call", tool=name)` (the Logfire facade),
   wrapping the actual delegate call so the span carries the exchange's REAL latency (the wrapper
   cannot know a result will be conformant before it returns, so it cannot avoid calling first). A
   conformant result stamps four attributes on the active span before returning: `disclosure.ref`,
   `disclosure.kind`, `disclosure.mode`, and `disclosure.budget_reached` (`bool`). engram's eval
   harness reads these spans back out via `RetrievalMetrics` (`n_disclosure_calls` counts
   `"disclosure tool call"` spans, `disclosure_budget_reached` is `True` if any span stamped
   `disclosure.budget_reached = True`). See
   [evals.md](../../../engram/docs/reference/evals.md#public-contract). A non-conformant result
   still runs inside the span (real latency captured) but stamps none of the four attributes and is
   returned/recorded unchanged.

Configuration: `disclose_tool_name: str = "disclose"` (named for the instructions text only, the
budget wrapper matches by result shape, not tool name) and `max_envelope_tokens: int = 20_000` (the
per-run cumulative cap).

### Testing seams

- Fakes/doubles: none needed for the contracts (frozen pydantic models, construct directly in
  tests). The capability is exercised via pydantic-ai's `TestModel`/`FunctionModel` and a stub
  toolset in `core/tests/unit/disclosure/`.
- `observability.span` degrades to local-only span recording with no Logfire token configured
  (`enable_local_span_recording`), so a disclosure-capability test needs no network or token.

## Testing seams (package-wide)

- **Wireup DI**: every module above is app-agnostic. An app (engram, workflows) wires the concrete
  adapter (embedding provider, DB-backed toolset, `EnvelopeHandler`) at its own composition root, per
  `core.resolver`'s `Resolver` Protocol.
- **Fakes over mocks**: `core/tests/support/fakes/` holds the structural fakes this package's own
  tests need (public scriptable attributes, no leading underscores, per `core/CLAUDE.md`). A
  consuming app's fakes for its own provider (e.g. engram's `FakeSqlGateway`) live in that app's
  own `tests/support/fakes/`, not here.
- **Tests:** `core/tests/unit/` is flat, one directory per module area (e.g. `disclosure/`), not a
  mirror of the `src/core/ai/` package tree.
