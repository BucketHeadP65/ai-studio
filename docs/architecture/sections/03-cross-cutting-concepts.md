<!-- GENERATED from architecture.md by scripts/docs/split_doc.py --dir docs/architecture — do not edit here; edit the master and re-run the script. -->

## 3. Cross-cutting concepts
> **TL;DR:** Vertical slices in the engram library. CQRS bus from core.cqrs, used by the knowledge base half only. Five records about one workflow run (graph, presentation, intent, checked outcome, graded usefulness and owner acceptance), each answering one question. A governed outcome benchmark refuses before it measures, and names its own model paths by reading them. zod-parse every TS boundary. Both backends local-only with no auth. Path-source (not workspace) dependency strategy. Two composition styles inside studio (wireup DI in the knowledge base half, plain composition in the agent half). One alembic chain, in engram, for both databases.
<!-- load-when: conventions, patterns, adding a new app, adding a slice, dependency strategy, DI, migrations, testing discipline, presentation, intent, checked outcome, usefulness, owner acceptance, artifact action, run arguments, outcome benchmark, case custody, metered model path, blocked report -->

One subsection per concept. The *why* for each lives in
[design-patterns.md](../../explanation/design-patterns.md) and the ADRs.

### Vertical slices
In the engram library, every feature is a self-contained package, `domain.py`
(contracts), `repository`/`orm`, `service`/`handlers`, under `src/engram/`:
`compile, retrieval, evals, sources, memory, db, init, export`. Their wireup
providers sit app-side, one module per slice under `studio/src/studio/corpus/wiring/`,
because registration is composition
([engram ADR 0039](../../../engram/docs/adr/0039-library-app-package-split.md)).
Cross-slice calls go through `domain.py` protocols, not internals.
The workflow engine keeps its own shape: a framework-pure package
(`core.ai.workflows`, grammar, validator, graph builder, library seam, trust
machinery) plus `adapters/`, with the agent half's FastAPI app over it, split
into `browsing` and `running` slices.

### CQRS message bus (knowledge base half only)
`core/src/core/cqrs/`, frozen pydantic `Command`/`Event` bases, one handler per command. A handler's events go to the
`coordination.outbox` table (inside the handler's transaction when it has one), and the app's `Worker` delivers them to
the bus's subscriptions once per subscriber, recorded in `coordination.event_deliveries`. Background work is a row in
`coordination.jobs`, claimed under `SKIP LOCKED`. Deliberately introduced for the ingestion pipeline
([ADR 0002](../../adr/0002-wiki-cqrs-bus-and-wikinote-kernel.md)) and re-homed into the shared library by
[ADR 0007](../../adr/0007-consolidate-shared-python-into-core.md). Only the knowledge base half dispatches through it, and nothing
else should grow a second bus casually.

### Agent inspection and measured improvements

The harness uses the same durable job machinery in the sessions database for inspection. Its record's existing events
trigger dispatch, while reconciliation catches terminal events missed by the cursor. Scheduling and dispatch,
reconciliation, rules, attribution and scans each run in an independent loop. Terminal event markers distinguish an
earlier partial inspection from processing the completed transcript.

The inspector reads paged recorded messages, instructions and complete request/tool schemas. Native output validation
records rejected answers. A shared lead-row lock protects final findings and explicit dismissals against concurrent
late answers. Uncertainty and invalid output remain unresolved, and a diagnosis remains a causal hypothesis.

The owner can curate versioned diagnostic cases and run paired outcome evaluations through `harness evals inspection`.
Independent grading, repeated held-out trials, provenance and runtime fingerprints distinguish measured improvements
from plausible explanations. Learning records accumulate comparable evidence and every accepted or rejected proposal.
The evaluation command does not activate skills or publish changes. The contract is in the
[inspection reference](../../../harness/docs/reference/inspection.md) and the decision in
[inspection evidence and outcomes](../../../harness/docs/adr/0020-inspection-evidence-and-outcomes.md).

### Reliable workflow execution (outcomes, retry feedback, durable continuation)

A rejected judge, person, or orchestrator gate records one `RetryFeedback`, keyed to the target
node and turn, carrying the reviewer's exact words, the immutable value it rejected, and the
requested repair. Every item of a rewound map turn receives it, and a second use of the same work
elsewhere receives none. Each gate spends a bounded `max_attempts` (default 3, the first review
included) before it fails the run rather than rewind again. Every ended run classifies a checked
`Outcome`, kept apart from the graph's own execution status: which pieces of the declared primary
work settled, and whether every required gate's decision covers the exact bytes the run ended
holding, by an accepted source, at the current generation. A multi-turn map's durable line survives
a worker restart: the conversation lives in `core.ai.history`'s continuation tables, addressed by a
bounded `ContinuationRef` rather than a process-local store or a Temporal payload transcript. The
contract is in [core-ai.md](../../../core/docs/reference/core-ai.md) and
[workflow-monitoring.md](../../../harness/docs/reference/workflow-monitoring.md).

### Five records about one run, and the question each one answers

One workflow run leaves five records behind, and they are easy to mistake for each other because
four of them are prose about the same piece of work. They are kept apart on purpose: each answers
one question, and no layer may answer its question with another one's record.

| The question | The record | Who writes it | What checks it | What rewriting it costs |
|---|---|---|---|---|
| What will actually run? | `Workflow`, the executable graph | whoever authored the spec | the validator, before anything starts | the workflow's identity. `spec_identity` hashes the graph, so an edit mints a new version and the old one keeps its own evidence |
| How is this put to a person? | `WorkflowPresentation`, on the library card | the same author, in their own words | `verify_presentation` against the spec: a labelled input must name a declared parameter, an action must name a declared artifact | nothing. Identity and evidence are untouched, which is the reason it lives on the card and not in the graph |
| What did somebody ask for? | `WorkflowIntent`, on the library card | the person who wanted the automation, through guided authoring | nothing. A request is not a claim about the graph, so it is kept as it was stated | nothing, and it grants nothing: no work is selected and no tool is named |
| What did this run do? | `Outcome`, on the run | nobody writes it. It is derived from the run's own facts when the run ends | itself. Item dispositions, the active generation, and each required gate's decision over the exact bytes | nothing, because it is never rewritten. `unknown` stays unknown |
| Was the work any good, and did the owner take it? | `TaskOutcome` (graded) and `OwnerReview` (an event) | an independent grader, and the owner | the grader's own rubric, over resolved artifact bytes. A review is appended to what a resolver answered, never to a value the caller already had | nothing on the run. Both are separate records, joined by a read model that returns unknown when there is no review |

The three confusions worth naming, because each one has a rule attached:

- **Presentation is not intent.** Presentation is what a reader is *shown*. Intent is what was
  *asked for*. That is why one is checked against the spec and the other against nothing, and why
  the save gate reads the presentation and ignores the intent. A draft carrying half an intent
  saves. A draft carrying no presentation does not.
- **A checked outcome is not a verdict on the work.** `status == "succeeded"` says every piece of
  the declared primary work settled and every required gate's decision covered the exact bytes the
  run ended holding. It says nothing about whether the result was worth having. The evaluation
  layer scores that separately, and the owner's acceptance is a third record again, so a rejected
  review leaves a `succeeded` run `succeeded`.
- **A recorded number is not a status.** A presentation may name recorded-output fields for a run's
  page to show under their own labels. They are read off what the run recorded, they are display
  only, and they never move the status word beside them. A run that reports "12 notes written" and
  fails its gate is a failed run that recorded twelve.

**The closed artifact-intent resolver.** An action a presentation offers is one of a closed
first-party set (`save_note`, `record_owner_review`), each naming a handler this deployment already
implements. The data carries a kind, an id, a label, and which declared artifacts it concerns, and
there is nowhere in it to put a URL, an HTTP method, a filesystem path, or a request body, because
the models refuse an extra key rather than dropping it. Pressing one resolves every reference it
names through the run's own read-only route (`POST /api/runs/history/{run_id}/artifacts/resolve`)
and hands the resolved documents to that handler. The references come from the selected run, never
from the client. An intent whose handler is not installed in this deployment renders unavailable
and does nothing: it never falls back to a route the surface invented. This is what lets a template
offer an action without a template ever naming a route.

**The JSON value boundary.** A run is given one JSON value per parameter
(`WorkflowArguments = dict[str, JsonValue]`), and one admission
(`core.ai.workflows.contracts.normalize_arguments`) is shared by every launch path: the HTTP route, the agent
tool, a schedule fire, a rerun, and an adaptation. Each value is checked against that parameter's
own Draft 2020-12 schema with nothing converted to fit. An array stays an array, a boolean is never
read as an integer, and a string parameter keeps whatever text it was given. The one accommodation
is for callers that can send nothing but text (a stored schedule row, an older client, a form): text
supplied to a parameter whose schema cannot hold text is decoded once with `json.loads` and then
checked like any other value. Text that is not JSON is refused naming the parameter and the path
inside the value, so a Python repr such as `"['research']"` is a refusal rather than a guess.
Whether a name is one of the knowledge bases this deployment is registered to read is deliberately
not decided here. It belongs to the host holding that registry, which checks it after admission
against the control the saved card declares.

### Governed outcome benchmarks (what a measurement is allowed to claim)

A benchmark that compares a simple single-run baseline against workflow automation is a paid,
irreversible act, so `harness evals outcomes` is built to refuse before it measures. Every refusal
happens before a budget cycle is opened, and the world the benchmark would run in is composed only
once the cases have proved legitimate, so a set that makes no held-out claim never builds the models
it would have run.

**Cases are legitimate or the run is blocked.** A workflow-automation development set declares itself
author-visible in its own type and cannot carry a seal, so a rollout claim over it is refused
structurally rather than by a flag. A sealed comparison reads a custodian's own `TaskDataset` beside a
custody record, and `engram.evals.outcomes.contracts.audit_cycle` grades seals, freeze timing,
fingerprint match, authorship disjointness, recorded label exposure and case freshness. A cycle with
no verified held-out claim writes a blocked report and skips every paid step. That report is the
expected outcome when no independent custodian exists, not a failure.

**Three checks, and only one of them reads behaviour.** The classifier reads each declared path's own
model object, and a path that does not admit on this record and this cycle lands in the manifest's
unmetered set and refuses the run. The declaration is then held against what each of the three says:
the paths it requests through, object for object and in both directions, and whether its figures
come from this record or from another. That refuses a composer that hands one of them an object it
never declared, a declared path none of them stands behind, one that declares nothing without
claiming another record's figures, and a grader that does not itself ask through a declared
grader-role path. It compares statements, so one that misstates any of them is not caught by it.

The third check reads behaviour, and it reads less than its name suggests. After the arm and after
the grader, whether either returned or raised, the requests it reports are compared with the
requests the record admitted for that trial. It is asked only of one whose figures come from this
record and it reads over-reporting only, so what it establishes is that such a one cannot claim to
have spent more than the record admitted without stopping the sitting. One that under-reports
passes. It detects rather than prevents, and the only bound on what one slot costs is that there is
one: the thresholds live inside `BudgetedModel` and these are requests no `BudgetedModel` saw. Every
later slot refuses before its arm runs, and the sitting is never reported as measured.

The command is also the one caller that can mount the meter at all: the environment a person's
journeys run in is composed once when the process boots, long before a pilot opens, so the Studio
path cannot meter itself. This deployment's composer puts `BudgetedModel` on the baseline and on the
grader, which are the two paths that ask a model during a benchmark. The workflow arm reads the runs
a person's journeys already archived, so it asks nothing now and whatever it reads back was admitted
under the pilot's own cycle. A reader comparing the two arms' charges under the
`measured-generative-model-cost` objective is therefore reading two records, not one.

**A report says what it ran on.** `empirical_status` is `blocked`, `inconclusive` or `measured`, and
`model_kind` is `live`, `test_double` or `unbuilt`, so a measured report over stand-ins reads as
apparatus output rather than as evidence about a deployment. Every slot is filed under its own name
before a verdict is folded over it, including one the grader's own request was refused on, and a
report is written once under a name derived from its content. A report that ran carries each
slot's grade and the paired comparison's verdict, reasons, success rates, regressions and per-arm
usage. The shipped grader judges answer text, and a candidate's pass stands only when its archived
run's checked status is `succeeded`. The statistical claim is
`descriptive_only` and the recommendation is `none`, both as constants, so a reader sees them in the
record rather than inferring them from an absence. The contract is in
[workflow-monitoring.md](../../../harness/docs/reference/workflow-monitoring.md).

### zod at the boundary (TS apps)
Every backend response is `.parse()`d against a zod schema before reaching
components, never `as`-cast, so backend drift surfaces as a `ZodError` at the
boundary. The typed fetch wrapper lives in `client-core` (`api/client`).

### Local-only, no auth
Neither backend authenticates: `studio serve-http` and the agent app bind
loopback and the localhost binding *is* the security boundary. Each opens CORS
to exactly the Studio's origin it serves (`:3031`). Workflow steps that shell
out are explicitly **not sandboxed**, their mitigations are a wall-clock
timeout, an output-character cap, `start_new_session` + process-group kill, and
a resolved working directory. The documented non-goal is "never load untrusted
courses" (it runs the user's own code on the user's own machine). Programs
are the exception: they run in a Monty sandbox, never `exec`.

### Dependency strategy (`file:` / path sources, not workspaces)
Python: `core = { path = "../core" }` and `engram = { path = "../engram" }` in
the app, deliberately **not** a uv workspace, so each project keeps its own
lockfile/venv ([ADR 0001](../../adr/0001-wiki-app-placement-and-core.md)).
`engram-connectors = { path = "../connectors" }` joins them in `studio` and
`harness`, each naming the extras it uses
(`engram-connectors[conversion,source-queue,youtube,zotero]`). `engram` itself never
depends on it, and resolves a knowledge base's declared connector names through the
`engram.sources.connectors` / `engram.sources.converters` entry-point groups at
wiring time ([ADR 0018](../../adr/0018-corpus-bundles-and-provider-packages.md)).
Because that lookup is by entry point rather than by import, a stale editable
install surfaces as "connector not installed" rather than as an `ImportError`:
after a package layout change, re-sync every consumer with
`--reinstall-package`. The
workflow engine ships behind `core[workflows]` / `core[workflows-temporal]`, so
plain `core` (and therefore `engram`) never installs the engine's optional
weight ([ADR 0009](../../adr/0009-ai-factory-studio-consolidation.md), amended by [ADR 0010](../../adr/0010-harness-extraction.md)). TS:
`"client-core": "file:../client-core"` + `transpilePackages`
([ADR 0004](../../adr/0004-client-core-shared-frontend-package.md)), **pivoted
to copy-install** (`install-links=true` in `.npmrc`) after a Turbopack OOM
incident. The ADR's symlink wording is stale on this point, root `CLAUDE.md`
is authoritative. Editing `client-core` requires `npm install ../client-core`
in each consumer.

### Dependency injection (two styles, deliberately)
The knowledge base half is **wireup DI**: composition root
`studio/src/studio/corpus/container.py`. Slices contribute `@wireup.injectable`
factories from `studio/corpus/wiring/<slice>.py` via module discovery. FastAPI
integration through `wireup.integration.fastapi`. The CLI injects a test
container through a `ctx.obj` zero-arg factory seam, no monkeypatching.
Multi-implementation seams use the **strategy registry** pattern (frozen
dataclass over `Mapping[Enum, Adapter]`). The agent half is **plain
composition**: `harness_session()` in `harness/src/harness/composition.py` builds the environment on
an `AsyncExitStack`, with no container, and depends on its seams through
`Protocol`s. `tests/unit/test_layering.py` enforces the tier boundaries. The two styles coexist on purpose and neither is migrated toward
the other. The structural DI gates are scoped to the knowledge base half so the code
says so.

### Migrations: one alembic chain, in engram
`engram` owns the repo's only alembic chain and `alembic.ini`, resolved
checkout-relative, from `0001_wiki_schema` (extensions: pg_search, vector,
ltree) through the `api` surface, the read-only roles, the graph, eval and
index state, the `memory` schema rename, and the envelope surface. The
`corpus_reader` role is granted SELECT/EXECUTE on the `api` schema only,
read-only, `statement_timeout=10s`. The same chain migrates **both** databases
by URL parameter. The studio owns no chain and reads the workflow library through
that same read-only role.

### Progressive disclosure (cross-app protocol)
Any provider serves information level by level by returning a conformant
`DisclosureEnvelope` (`core.ai.disclosure`): the disclosed unit plus child
summaries and refs, deeper levels exist only as refs, and the provider's tool
accepts a ref back (the descent convention,
[ADR 0008](../../adr/0008-progressive-disclosure-protocol.md)). Enforcement is
tiered: the `ProgressiveDisclosure` pydantic-ai capability budgets and traces
envelopes in owned runtimes. engram's leveled `fetch` MCP tool and
`/api/envelope` serve server-enforced thin envelopes to uncontrolled clients.
engram is the first conforming provider (`api.envelope_*` reader surface,
indexed, governed `memory.unit_summaries`).

### Testing discipline
Pytest `asyncio_mode=auto`, tiers `unit/integration/e2e`, polyfactory
factories, hand-written `Fake*` over mocks. The library's integration tier and
the app's integration/e2e tiers run a real database via testcontainers
(auto-skip without Docker). TS: Biome, Vitest + MSW
(`onUnhandledFrame: "error"`, deliberately empty default handlers),
Playwright e2e. Ruff `ALL` @ 140, Google docstrings, no `Any`/`# noqa`
masking across every Python project.
