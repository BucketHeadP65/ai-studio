# core: shared Python library

App-agnostic building blocks every Python app in this monorepo may depend on. If code is
general-purpose, it belongs here. An app's domain stays in the app (root ADR-0001, ADR-0007).

## Module map

- `core.db`: async engine/session factories, the reader engine (`build_reader_engine`), bind-parameter batches, `redact_db`,
  and `SessionScopedFactory` (one fresh session per call)
- `core.files`, `core.markdown`: file and markdown utilities (atomic writes, the cooperative `exclusive_lock`,
  `has_hidden_component`, heading slugs, the paragraph split of a section body)
- `core.connection_failures`: whether an error is a database connection failure, a statement timeout or a lock timeout, and
  the message that names the service a failed connection reached (an embedding endpoint, a model endpoint or the database)
- `core.fingerprints`: SHA-256 fingerprints of text and of models, and the canonical text of a set of names
- `core.statistics`: the seeded paired bootstrap lower bound
- `core.column_types`: the SQLAlchemy `LTREE` and asyncpg-bound `halfvec` column types
- `core.net`: the guarded page fetcher that refuses a URL resolving to a non-public address
- `core.settings`: the infrastructure section every application shares, one model per external system, and its reader
- `core.supervisor`: one child process kept alive with capped backoff
- `core.testing.fakes`: test doubles for core's ports (`FakeUnitOfWork`), imported by every package's tests
- `core.cqrs`: message-bus kernel (bus, messages, unit-of-work), the coordination schema and ports,
  the in-memory and Postgres adapters, the `Worker`, and the change `Broadcaster`
- `core.observability`: logging facade (`get_logger`), and the Logfire tracing and price refresh a host starts
- `core.resolver`: the DI `Resolver` port (apps own their adapters, e.g. engram's `src/resolver.py`)
- `core.wiring`: the settings-section derivation over a settings root (`sections_of`) and the resolver adapter over
  a wireup container (`ContainerResolver`, `resolver_of`)
- `core.ai`: model/embedding factories (re-exported at package root), the `QueryEmbedder` port, the generic dispatch
  registry, capabilities, and `core.ai.empty_answer` (`EmptyAnswerRetry`)
- `core.ai.retrieval`: retrieval strategy axis, policy, tool ports, knowledge-base-schema SQL builders
- `core.ai.disclosure`: the provider-agnostic progressive-disclosure protocol (`DisclosureEnvelope`/
  `ChildSummary` contracts, the `ProgressiveDisclosure` pydantic-ai capability)
- `core.ai.format_maps`: map-then-slice reading for known-format tool returns (`FormatMaps`, `MappedTool`, `command_starts`,
  the outliners and the content cache)
- `core.ai.filesystem`: `UnnumberedFileSystem`, pydantic-ai-harness's `FileSystem` with `read_file`'s line numbers removed
- `core.ai.shell`: `PathGuardedShell`, pydantic-ai-harness's `Shell` refusing a command that names a denied path
- `core.ai.tool_output_limits`: `ToolOutputLimits`, pydantic-ai-harness's capability that also keeps a reduced return's `tools`
- `core.ai.history`: the conversation record behind pydantic-ai-harness's step-store port (every message once, forms, stamps,
  step entries), its Postgres store over asyncpg, the request-parameters recorder and the DDL it ships, the per-step
  usage tap (`StepUsageTap`) and `recording_capabilities`
- `core.ai.transcripts`: a conversation as a detector reads it (`Transcript`, `Step`, `Span`, `Call`), the loader registry by
  source scheme, and the two loaders: the record (`record:<run id>`) and a Claude Code session file (`claude-code:<id>`),
  plus `RunsOfConversation`, the port for a conversation's earlier runs, and the record's adapter behind it
- `core.ai.inspection`: the taxonomy of symptoms, root causes and surfaces with the horizon each symptom is read at, the
  rules (pydantic-evals evaluators that turn a transcript into leads), the views the inspector reads, the inspector
  (a model with four reading tools that attributes a lead and scans a whole run), watches, the leads and findings store
  with the consumers' cursors, the three job lanes (`inspect_run`, `attribute_lead`, `scan_run`) and their handlers, the
  dispatch over the record's events, and the sweep over a window, the questions the inspector was asked with the
  conversation each was answered in, and the reader's port over all of it. `evaluation` holds versioned diagnostic
  cases, repeated paired outcome comparisons and validated reports. `learning` retains evidence and immutable
  accepted/rejected attempts in atomic local records. Diagnoses are hypotheses until independently evaluated.
- `core.ai.workflows`: the workflow engine (workflows as data: vocabulary, registry of steps, validator,
  graph builder, library port, trust machinery), the contract port, and `infrastructure/` (the folder and
  collection libraries, the evidence log, Temporal). Outside code imports `core.ai.workflows.contracts`, whose
  Temporal names load on first use. Ships behind the `core[workflows]` / `core[workflows-temporal]`
  extras. `core.ai.__init__` never imports it. Consumers reach it by explicit submodule import.

## The ripple rule (binding)

**A change here affects every consumer.** Before claiming any core change green, run the full
gate (`uv sync`, `uv run ruff check`, `uv run pyright`, `uv run pytest`) in `core/`, `engram/`,
`studio/`, `harness/` AND `connectors/`.

## Tests

Tiered layout: `tests/unit/` (pure, no Docker), `tests/integration/` (a throwaway Postgres testcontainer per session,
skipped without Docker, `CORE_TEST_DATABASE_URL` names another throwaway database instead),
`tests/support/fakes/` for structural fakes (public scriptable attributes, no leading underscores).

## Code standard

Docstrings, slice layout, CQRS, names and what belongs in `core` follow the root `CLAUDE.md` section of the same name.
