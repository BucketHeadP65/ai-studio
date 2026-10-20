# core

`core` is the shared Python library of this repository. It holds the general-purpose building blocks that more than
one package needs, and it imports no application. `engram`, `connectors`, `studio` and `harness` install it from this
folder as an editable path dependency.

## What is inside

| Module | What it holds |
| --- | --- |
| `core.db` | Async engine and session factories, the read-only engine and batched bind parameters. |
| `core.files`, `core.markdown` | Atomic writes, a cooperative file lock, heading slugs and section splitting. |
| `core.settings` | The `infrastructure` section every app shares: one model per external system, with its address and credential. |
| `core.net` | The page fetcher that refuses a URL resolving to a private address. |
| `core.cqrs` | The message bus: commands, events, the unit of work, the outbox, the job coordination schema and the worker. |
| `core.observability` | The logging facade and Logfire tracing. |
| `core.connection_failures` | Whether an error is a database connection failure, a statement timeout or a lock timeout, and the message naming the service a failed connection reached. |
| `core.resolver`, `core.wiring` | The dependency-injection port, and the helpers that build a wireup container from a settings tree. |
| `core.fingerprints`, `core.statistics`, `core.column_types`, `core.supervisor` | Text fingerprints, a paired bootstrap bound, Postgres column types, and a child process kept alive with backoff. |
| `core.ai` | Model and embedding factories, the retrieval strategy and its SQL builders, progressive disclosure, format maps for large tool output, the conversation record, transcripts and run inspection. |
| `core.ai.workflows` | The workflow engine: the workflow vocabulary, the registry of steps, the validator, the graph builder, the library port, evidence and trust, lineage, and the folder, collection and Temporal adapters. |
| `core.testing` | Test doubles every package's tests import. |

Outside code imports a slice through its `contracts` module, such as `core.ai.workflows.contracts`.

## Extras

The workflow engine ships behind two extras, and `core.ai` never imports it on its own:

- `core[workflows]` adds `pydantic-graph` and `pydantic-handlebars` for the vocabulary, the validator, the builder and
  the adapters that need no server.
- `core[workflows-temporal]` adds the Temporal SDK for the Temporal adapter as well.

## Develop

```bash
uv sync
uv run ruff check
uv run pyright
uv run pytest
```

`tests/unit/` needs no Docker, and `tests/integration/` runs against a Postgres that it skips without. A change here
reaches every package that depends on `core`, so run the same gate in `engram/`, `connectors/`, `studio/` and
`harness/` before calling it done.

## Docs

- [`docs/reference/core-ai.md`](docs/reference/core-ai.md), the contracts of `core.ai`.
- [`docs/adr/`](docs/adr/), this library's decisions.
- [`CLAUDE.md`](CLAUDE.md), the full module map.
