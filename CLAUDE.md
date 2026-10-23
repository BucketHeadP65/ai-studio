# CLAUDE.md

This file provides shared guidance to Claude Code and Codex when working in this repository.
`AGENTS.md` is a symlink to `CLAUDE.md` at the root and in each package. Edit the shared
`CLAUDE.md` file. **Before working in a package, read its `CLAUDE.md`** for commands and
package-specific conventions, even when the session started at the repository root. This
root file carries only what is monorepo-wide.

Project skills are shared through `.claude/skills/` and `.agents/skills/` symlinks to the same
source folders. Edit the source, keeping its scripts and references beside `SKILL.md`.
Use the current client's tools and permissions. Claude settings and skill metadata do not
configure Codex. When adding a package instruction file or project skill, add its counterpart
symlink so both clients discover it.

## Repository Structure

Two Python applications, three Python libraries, one SPA, and the knowledge base templates the
applications are kept clean of:

- **`engram/`**: domain-general markdown knowledge-base indexer, **the library** (Python 3.13): note
  grammar, index-to-Postgres, retrieval tools, evals, sources, init/export. Ships one package,
  `engram`, which other projects import. It has no console script. The CLI/HTTP application over it
  is `studio/`. Also a Claude Code plugin (`engram/plugin/`) and the repo's one alembic chain.
  See `engram/CLAUDE.md`.
- **`studio/`**: the knowledge base application (Python 3.13, package `studio`, console script `studio`):
  the knowledge base CLI plus its HTTP API (`/api` on :8100). See `studio/CLAUDE.md` and `studio/CONTEXT.md`.
- **`harness/`**: the coding-agent application (Python 3.13, package `harness`, console script
  `harness`): the agent and workflow runtime on :8770 (workflows as data), the
  self-authoring harness, plugins and profiles, the terminal bridge, and the ACP entry. The workflow
  engine itself is `core.ai.workflows`. See `harness/CLAUDE.md` and `harness/CONTEXT.md`.
- **`studio-client/`**: **AI Factory Studio**, the SPA (Next.js) over the `harness`
  backend (:8770) and the `studio` knowledge base API (:8100): the workflow studio (main entry point), the graph,
  knowledge bases, and the harness control room. Its app-agnostic frontend code (the typed fetch and zod
  boundary, the UI primitives, the utilities and the test support) lives in `studio-client/src/shared/`,
  which imports nothing from the rest of the app (root ADR-0021). See `studio-client/CLAUDE.md`.
- **`core/`**: THE shared Python library (`core.db`, `core.files`, `core.markdown`, `core.cqrs`,
  `core.observability`, `core.resolver`, `core.ai.*`, root ADR-0007). See `core/CLAUDE.md`.
- **`connectors/`**: the connectors and their document converters (Python 3.13, package
  `engram_connectors`, distribution `engram-connectors`): Zotero, YouTube and a general URL connector over
  engram's `Connector` / `DocumentConverter` ports, one subpackage per connector behind its own
  optional extra. engram never imports it and indexes with none of it installed. A host discovers
  what is installed through the entry-point groups `engram.sources.connectors` and
  `engram.sources.converters`, which `engram.sources.registry` resolves by name. Both `studio/` and
  `harness/` depend on it (the tools that classify a pasted URL run in both processes).
  See `connectors/CLAUDE.md`.
- **`templates/`**: one folder per knowledge base template (`research`, `repo-docs`): its `knowledge-base-schema.json`,
  its `_meta/` guidance pages, its `workflows/{steps,workflows}/`, optional evals, and a README.
  Data, not code. `studio init --template <slug>` starts a knowledge base from one and `harness import
  templates/<slug>/workflows` imports its workflows. Nothing outside `templates/` imports or reads a
  template except the `studio init --template` resolver and `engram.testing.specs`, held by an
  import-boundary test in each Python project.
- **`pydantic-ai-harness`** comes from PyPI (`>=0.57.0`), released from the pydantic-ai repository
  (`src/pydantic_ai_harness`). It versions with pydantic-ai (0.57 goes with 2.57). Our additions over it live in our own code: `core.ai.format_maps`,
  `core.ai.filesystem`, `core.ai.tool_output_limits`, `harness.adapters.acp_client` and `engram.evals.harness.code_mode`. Upstream
  contributions are filed as ISSUES on `pydantic/pydantic-ai`, never PRs. Two records feed those issues
  and are updated in the same change that lands the fix or the feature: a bug found in pydantic-ai or
  the harness goes to `docs/explanation/upstream-bugs.md` (UB-n), anything we add over the libraries
  goes to `docs/explanation/upstream-extensions.md` (UE-n).

**Ownership rule:** general-purpose, app-agnostic code lives in `core/` (Python) and
`studio-client/src/shared/` (frontend). An app's domain stays in the app. Each application owns its
`DeclarativeBase`/metadata. The repo's single alembic chain lives in `engram/`.
**A knowledge base's vocabulary lives in its template** (its page types, folders, field names, statuses and
collection name), never in `engram/`, `studio/`, `harness/` or the SPA, which take all of it from
the knowledge base's own schema or from the request. **A connector owns its host** (its URL shapes, its key
derivation, its item-type vocabulary, its deep links) and lives in `connectors/`, never in the
indexer. A knowledge-base-specific or host-specific literal in a domain-general layer is a review failure
(root ADR-0018).

> **Ripple rule: `core/` is a shared dependency.** A change there must keep
> every consumer green. Run the consumers' full gates before claiming it done.

## The local stack

`scripts/stack/stack.sh` runs the local stack as launchd services on macOS: `db` :5434, `harness` :8770,
`studio-api` :8100, `studio-client` :3031 (the dev server, so frontend changes are live on save).
`scripts/stack/stack.sh status|logs|restart <service>` manages it. `scripts/stack/README.md` has the map.

> **Restart after backend changes.** After changing backend code (`harness/`, `studio/`, `engram/`, `core/`,
> a migration), run `scripts/stack/stack.sh restart harness studio-api`. The running app carries old
> code until then. Frontend needs no restart. Tests never bind the stable ports.

## Package Management

Each Python project uses **uv** with an in-project venv (`<project>/.venv`), path sources keep
lockfiles independent. Always `cd` into the project before running commands. Every project sets
`default-groups = ["dev", "test"]`, so a bare `uv sync` installs the test tooling. The acceptance
gate is identical everywhere: `uv sync`, then `uv run ruff check`, `uv run pyright`,
`uv run pytest`. **pyright runs in every project.**

## Running a single test

Python (all projects), node ids. The unit/integration/e2e split is by **directory**, no markers:

```bash
uv run pytest tests/unit/test_foo.py::test_bar     # one test
uv run pytest -k "bar and not slow"                # by name expression
uv run pytest tests/unit/                          # unit tier only (never needs Docker)
```

Frontend: `npm test -- <file-filter>`, `npm test -- -t "<name>"`. Playwright:
`npm run test:e2e -- e2e/foo.spec.ts`.

## Code Style (all Python projects)

Ruff `select = ["ALL"]` (per-project ignores in each `pyproject.toml`), line length **140**,
double quotes, Google docstrings, isort with `lines-between-types = 1`.

## Code standard (all Python projects)

One standard for docstrings, slices, CQRS, names and shared code.
`scripts/code/check_docstrings.py` and `scripts/code/check_slices.py` check what a machine can check, over the slices already converted, and each project's `tests/unit/test_code_standard.py` runs them.

### Docstrings

Docstrings, comments and names are written for a reader who has not read the code.

1. The first sentence says what the thing is or does, in plain words, one idea, in under about twenty words.
2. After it, only what a caller must know to use it correctly: a constraint, a guarantee, what it refuses. One idea per sentence. Three sentences is the usual ceiling for a function, six for a class or module.
3. The rule comes first, then what the caller can rely on: "You must pass a clock. This module never chooses one."
4. A public function or method that takes parameters has a Google `Args` section naming every one. It has `Returns` when it returns a value, `Yields` when it is a generator, and `Raises` for each error a caller should handle, with when it is raised. A property describes what it holds and has no `Returns`. A public `__init__` that takes parameters has its own docstring with `Args`, and the class docstring does not repeat them.
5. A private function, method or class has one line, or no docstring when its name says everything. It never has a section. Private means its name, an enclosing class or its module path starts with an underscore, or it is defined inside a function.
6. A port's method carries the full contract. An implementation's method opens with one line saying what is specific to it, followed by its sections.
7. A CLI command's or HTTP route's docstring is its help text: plain words for the person running it, with no sections. Its parameters are described where the framework reads them, in `typer.Option(help=...)` or in the request model. Every request field with a shape or a constraint has an attribute docstring that states it.
8. A tool's docstring is what the model reads. It keeps every instruction the model relies on: what the tool does, when to use it, what each argument means and what it refuses.
9. A public dataclass field or module constant has an attribute docstring when its name and type do not say what it holds.
10. The contract only. No reasons for the design, no history, no process or paperwork references (no ADR, spec, plan, task, review or wave, not even a bare `ADR-NNNN`), no file paths and no other layers' class names in prose. Reasons live in ADRs. References to other code, such as modules, migration revisions and database roles, are fine.
11. No abstract stand-ins: "the clock", not "what a run measures time with".
12. Comments follow the same rules and say only what the code cannot. A one-line comment may state what breaks when the code changes ("an unqualified name can resolve into `ag_catalog`"), because that is a fact about the code.
13. No semicolons, no em dashes and no dash clause breaks in prose. A `noqa` reason is written `# noqa: CODE (why the rule does not apply here)`.
14. Words are defined once, in the vocabulary (`docs/initiatives/2026-09-02-vocabulary/design.md`). A word is added there before it is used.
15. These rules do not cover tests.

### No legacy code

No code exists only to cope with something an older version wrote or accepted: no reader for an old stored shape, no
alias or field kept for old callers, no default that lets an older payload parse. When a shape changes, the same change
moves existing data forward (a migration, or a one-off script outside the repository) and deletes the old reader. A
branch that current code still feeds is current behaviour and keeps a plain name.

### Slices

A slice is one folder with one purpose: a `harness` feature, a `studio` area, or a subpackage of `engram`, `core` or `connectors`. Each part exists only when the slice needs it.

```text
<slice>/
  __init__.py            its docstring only
  contracts.py           the public API: use cases, public types, identifiers, commands, events, the errors a caller handles, the agent tools another slice hosts
  internal_contracts.py  the slice's own ports: <Things> for writes, <Thing>Queries for reads
  domain/                entities, value objects, identifiers and the rules they enforce
  service.py             the use cases
  read_models.py         what queries return
  exceptions.py          the slice's errors
  infrastructure/        database, network and file adapters behind the ports
  api_models.py          HTTP request and response bodies (applications only)
  config.py              the slice's settings section (applications only)
  wiring.py              what the slice registers (applications only)
  controller.py          its routes (applications only)
```

- Code outside a slice imports only `<slice>.contracts`. An application's composition modules (its entries, its container and the modules that register adapters) may also import a slice's `wiring`, `controller` and `infrastructure` modules. engram's package-level lazy imports for outside consumers stay, and they export only names that come from contracts files.
- `domain/` does no I/O and imports no database, HTTP or container code and no `pydantic_ai`.
- Database and network access live under `infrastructure/`, behind the slice's ports. `harness.adapters` counts as the harness's infrastructure.
- A small slice may use `domain.py` in place of `domain/`, and a large one may grow `service.py` into `service/`.
- Library slices in `engram`, `core` and `connectors` have no `api_models.py`, `config.py`, `wiring.py` or `controller.py`.
- Each adapter (a controller, a tools module) maps the slice's errors to its own answers in one place.

### CQRS

Inside a slice:

- A command is a service method named with a verb. It loads an entity through the write port, calls the domain methods that enforce the rules and saves through the same port. It returns nothing, or the least the caller needs.
- A query is a method on the service or the read port. It reads through `<Thing>Queries`, returns read models and never writes. A read model may be the entity itself when it would be an exact copy of it.
- The write port is a collection with `get`, `add`, `save` and `remove` when its table carries a version to compare and set. Over a table without one, the port keeps one targeted write per command, named for the write it does (`insert`, `update`, `stamp`), so two writers never overwrite each other's fields.
- A collaborator the container provides goes into the service's constructor. One that belongs to the current session or request is a method argument.

Between contexts, `core.cqrs`:

- A direct call goes through the other context's `contracts.py`.
- Work that must survive a crash or run later is a `Command` with exactly one handler, in the context that owns it.
- Something that happened is an `Event`, named in the past tense and written through the outbox in the same transaction as the change. The sender never knows who listens.
- A handler calls its slice's service and holds no rule of its own. A query is never a message.

### Names

- One word per thing and one thing per word, as the vocabulary defines them. The word a person says is the word in the code.
- A class is a noun for what the thing is. A write port is the plural of what it holds (`Schedules`), a read port is `<Thing>Queries`, an error is `<Thing><Problem>Error`, a command is `<Verb><Thing>` and an event is `<Thing><PastVerb>`.
- A function or method that changes something starts with a verb. One that reads names what it returns. One that answers yes or no reads as a question (`is_running`).
- Arguments, fields and variables use whole words. Single letters appear only in comprehensions and short loops.
- No invented metaphors and no literary names, and an obscure word is never swapped for a longer one.
- An identifier that is also an external name keeps its name: an HTTP path, an OpenAPI operation id or schema name (FastAPI takes these from route function and model class names), a request or response field, a CLI command or option, a database table or column, an MCP or agent tool name or argument, a change-notification topic, a file format. A refusal or error message a person or a model reads is external text too: it changes only in its own fix commit.

### What belongs in core

A thing moves to `core` when two or more packages need it, or when it holds no domain word, and only if `core` still imports no application afterwards. Two things that do one job merge into one. A move lands in one commit with every caller's new import.

## Type Hints (all Python projects)

- **Never** `Any`, `object`, or `# noqa` to mask a type-checker complaint. Fix the type
  relationship (narrow with `isinstance`, define a `Protocol`/`TypedDict`, restructure).
- Closed string sets become `Literal[...]` or `StrEnum`, never a commented `str`.
- `cast(...)` narrows what WE can prove. `Model.model_validate(payload)` validates data crossing
  a boundary we don't control. `httpx.Response.json()` gets validated, never cast.
- **Tight ports:** a service depending on a slice of a dependency takes a `Protocol` for that
  slice, not the full type.

## Testing (all Python projects)

pytest + pytest-asyncio (`asyncio_mode = "auto"`), paths `tests/unit|integration|e2e`,
polyfactory `ModelFactory[T]` (no redundant `__model__`). **Fakes over mocks**: hand-written
`Fake*` classes in `tests/support/fakes/` implementing the relevant Protocols, scriptable state
as public attributes.

## Documentation

Docs are **docs-as-code** (root ADR-0005), organized by Diátaxis + arc42-lite + C4 (Mermaid) +
MADR. The map: **root `docs/`** holds cross-app architecture masters (`docs/architecture/`),
root ADRs (`docs/adr/`, repo-shaping decisions), and the conventions charter (`docs/README.md`).
**`CONTEXT-MAP.md`** is the index. Every governed doc has a row. **app-local docs** live under
`<app>/docs/` (architecture, explanation, reference, and the app's ADR chain, no per-app ADR
README). **Working documents** live one folder per
initiative (charter, "Working documents"): `docs/initiatives/<YYYY-MM-DD>-<slug>/` with
`analysis.md` (optional), `design.md`, `architecture.md` (optional), `plan.md`. `status` alone
carries the lifecycle, files never move. `docs/brainstorming/` is frozen history (no new
files). Landing work promotes durable knowledge into governed docs (ADR +
architecture/reference + CONTEXT-MAP row).

Before committing doc changes run both (stdlib-only):
`python3 scripts/docs/validate_docs.py` and
`python3 scripts/docs/split_doc.py --dir <docs/architecture | <app>/docs/architecture>`.
Architecture masters are split-doc'd: edit the master, regenerate `sections/` + `index.md`,
commit together.

## Commit Messages

Conventional commit format.
