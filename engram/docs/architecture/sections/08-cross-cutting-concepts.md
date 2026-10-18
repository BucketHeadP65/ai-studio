<!-- GENERATED from architecture.md by scripts/docs/split_doc.py --dir engram/docs/architecture — do not edit here; edit the master and re-run the script. -->

## 8. Cross-cutting concepts
> **TL;DR:** The backbone every context rides on, the in-process CQRS bus (frozen Command/Event, one handler per command, events to the outbox for a worker to deliver), the DB foundation (`WikiBase` + custom types), the wireup composition root, pydantic-settings configuration, the observability facade, and the Typer CLI seam.
<!-- load-when: cross-cutting, cqrs bus, unit of work, db base, wikibase, wireup, composition root, settings, observability, cli, dependency injection -->

The **cross-cutting** backbone carries every context. The **CQRS bus**
(`src/cqrs/`) is framework-pure: frozen pydantic `Command`/`Event` bases
(`messages.py`), one handler per command, and events landed in the outbox with the
handler's unit of work for a worker to deliver (`bus.py`, `uow.py`). A subscriber that raises is
logged and skipped (ADR-0002 in the root chain). The **DB foundation**
(`src/engram/db/`) is engram's own `WikiBase` declarative base plus custom column types
(`types.py`). engram keeps its own metadata and alembic chain. The **wireup
composition root** (`studio/src/studio/knowledge_base/container.py`) is the only module that knows all slices:
each slice's `@wireup.injectable` factories live in its own `studio/src/studio/knowledge_base/wiring/<slice>.py`
module. Composition is app-side, so no library slice imports wireup, and
cross-slice routing lives here. `bus_provider` injects every slice's handler and
builds the `MessageBus` **at construction** from one declarative route table plus
one subscription table, no late registration anywhere
([ADR-0021](../../adr/0021-explicit-eager-composition-root.md), conventions in
[ADR-0038](../../adr/0038-dependency-injection-conventions.md)). **Configuration**
is pydantic-settings (`studio/src/studio/knowledge_base/settings.py`, `ApplicationSettings` over typed
sub-settings, env-prefixed `APP_*`). **Observability** is a single facade
(`src/observability.py`, `get_logger` / `configure_logging`) rather than scattered
logfire imports. The **Typer CLI** seam injects a test container through a
zero-arg `ctx.obj` factory (`studio/src/studio/knowledge_base/cli_support.py`), so the CLI needs no monkeypatching.

### Cross-cutting components

These are the backbone every bounded context (sections 3 to 7) sits on, not a feature slice:
the CQRS bus + unit-of-work port, the DB foundation, the wireup composition root
+ the `Resolver` port, pydantic-settings configuration, the observability facade,
and the Typer CLI. The one C4 L3 diagram below shows the backbone and how any slice
plugs in: its `studio/src/studio/knowledge_base/wiring/<slice>.py` module contributes `@wireup.injectable`
factories to the composition root. The entry points resolve everything through the `Resolver` port.
Write intents route through the `MessageBus` (one handler per command, events
to the outbox for a worker to deliver) while reads bypass it entirely.

**The CQS asymmetry is the headline fact (F4): writes go through the bus, reads
go direct.** `index` / `sync` / `export` / the evals commands are frozen
`Command`s dispatched through `MessageBus.handle` to exactly one handler, then the
handler's `events` land in the outbox with its unit of work, for a worker to
deliver once per subscriber (so a subscriber observes only committed state,
ADR-0002 in the root chain, a subscriber that raises is logged and skipped). The read path (`search` / `fetch` /
`graph_expand` / `find_paths`, section 5) never touches the bus. The CLI and MCP server
resolve the query handlers and the read-only gateway straight from the container.

Two deliberate seams enforce the project's code-style bar. The **`Resolver` port**
(`core/src/core/wiring/resolver.py`) is used *instead of* an `isinstance`/concrete-container check: ask
for a type, get an instance of it. `ContainerResolver` adapts a wireup
`AsyncContainer` (whose `get` is typed `T | None` because its key may be `None`)
into a plain `T`, raising `LookupError` on a missing provider. So callers never
suppress a type error or re-check at runtime. The **observability facade**
(`observability.py`) is the only sanctioned logging surface: modules call
`get_logger(__name__)` and get a backend-agnostic `Logger` Protocol. `logfire` is
imported in exactly one place (inside `configure_logging`, lazily), never scattered
across modules.

```mermaid
C4Component
    title Cross-cutting backbone (C4 L3), bus, UoW, DB, DI/Resolver, settings, observability, CLI
    Person(owner, "Owner / agent", "Runs the CLI, queries via HTTP/MCP")
    System_Ext(logfire, "Logfire (local recording)", "send_to_logfire=False, console=False")
    ContainerDb_Ext(pg, "Postgres", "kb.* + api schema")

    Container_Ext(slice, "A feature slice", "studio/src/studio/knowledge_base/wiring/<slice>.py", "Contributes @wireup.injectable factories, handler exposes events: list[Event]")
    Container_Ext(query, "Query handlers + gateway", "engram/retrieval", "Read path, resolved directly, NOT via the bus (F4)")

    Component_Boundary(cc, "Cross-cutting (src/)") {
        Component(cli, "Typer app + ctx.obj seam", "studio/src/studio/knowledge_base/cli.py / cli_init.py / cli_support.py", "_build_container(ctx): ctx.obj factory (tests) else _default_factory, configure_logging()")
        Component(container, "create_container", "studio/src/studio/knowledge_base/container.py", "Wires every slice's wiring module, bus_provider builds the fully-routed MessageBus at construction (declarative route + subscription tables)")
        Component(resolver, "Resolver / ContainerResolver", "core/src/core/wiring/resolver.py", "DI port: get[T](type) -> T (no isinstance), adapts wireup AsyncContainer, raises on missing provider")
        Component(bus, "MessageBus", "cqrs/bus.py", "Command -> one handler, events to the outbox for a worker to deliver, routes fixed at construction (ADR-0021)")
        Component(messages, "Command / Event / Message", "cqrs/messages.py", "Frozen pydantic bases (ConfigDict frozen=True)")
        Component(uow, "UnitOfWork", "cqrs/uow.py", "Async-context Protocol: commit explicitly, else rollback on exit")
        Component(settings, "ApplicationSettings + fetch_application_settings", "studio/src/studio/knowledge_base/settings.py", "pydantic-settings, init kwargs > APP_* env (__ nested) > application_settings.json (the knowledge base is init-kwarg only, a command names it with --knowledge-base or runs inside one), resource shapes re-exported from engram.config, cache-free fetch (ADR-0021)")
        Component(obs, "observability facade", "observability.py", "get_logger -> Logger Protocol, configure_logging (imports logfire, the one place)")
        Component(dbbase, "WikiBase + custom types", "engram/db/base.py, core/column_types.py", "DeclarativeBase (own MetaData), HALFVEC (asyncpg bind) + Ltree column types")
    }

    Rel(owner, cli, "invokes")
    Rel(cli, settings, "fetch_application_settings() (cached)")
    Rel(cli, container, "create_container(settings)")
    Rel(cli, resolver, "_build_container -> ContainerResolver")
    Rel(container, slice, "discovers wiring-module factories")
    Rel(slice, settings, "inject typed sub-settings (KnowledgeBaseSettings, …)")
    Rel(slice, obs, "get_logger(__name__)")
    Rel(slice, dbbase, "ORM subclasses WikiBase, HALFVEC/Ltree columns")
    Rel(container, bus, "late-register routes (register / register_subscriber)")
    Rel(cli, resolver, "get(MessageBus) for a write intent")
    Rel(resolver, bus, "resolve MessageBus singleton")
    Rel(bus, slice, "Command -> handler.__call__, hands its events to the outbox")
    Rel(slice, uow, "async with uow: … , commit() (then events observe committed state)")
    Rel(slice, pg, "writer engine (admin)")
    Rel(cli, query, "search/fetch, resolved directly, bypasses the bus (F4)")
    Rel(query, pg, "reads via kb_reader (api schema)")
    Rel(obs, logfire, "configure_logging (local recording only)")
```

Source: `src/cqrs/bus.py`, `src/cqrs/messages.py`, `src/cqrs/uow.py`, `src/engram/db/base.py`, `core/src/core/column_types.py`, `studio/src/studio/knowledge_base/container.py`, `core/src/core/wiring/resolver.py`, `studio/src/studio/knowledge_base/settings.py`, `src/observability.py`, `studio/src/studio/knowledge_base/cli.py`, `studio/src/studio/knowledge_base/cli_init.py`, `studio/src/studio/knowledge_base/cli_support.py`, `studio/src/studio/knowledge_base/wiring/indexing.py`, `studio/src/studio/knowledge_base/wiring/sync.py`

**Entity note (infrastructure, not domain, a component diagram suffices).** The
key cross-cutting types are: `Message` to `Command` / `Event` (frozen pydantic
bases, `cqrs/messages.py`). `MessageBus` (`cqrs/bus.py`) holding `dict[type[Command],
CommandHandler]` + `dict[type[Event], list[EventSubscriber]]`, with `CommandHandler`
/ `EventSubscriber` / `_EventCollector` as the structural Protocols it dispatches through.
`UnitOfWork` (`cqrs/uow.py`), the async-context-manager Protocol the indexing slice's
`SqlAlchemyWikiUnitOfWork` (section 4) satisfies. `WikiBase` (`engram/db/base.py`), engram's own
`DeclarativeBase` owning the `wiki` schema's `MetaData` (its own alembic chain,
never shared). The `HALFVEC` + `Ltree` column types (`core/column_types.py`). `Resolver` /
`ContainerResolver` (`core/src/core/wiring/resolver.py`), the DI port + wireup adapter. `Logger` (a
Protocol) behind `get_logger` (`observability.py`), and `ApplicationSettings`
(`studio/src/studio/knowledge_base/settings.py`), a `BaseSettings` composing typed sub-settings (`KnowledgeBaseSettings`,
`RetrievalSettings`, `InfrastructureSettings`, …) each registered as a wireup singleton.
The bus and the bus-built-here arrangement are owned by the indexing slice's
`message_bus_provider` (it constructs `MessageBus({IndexKnowledgeBase: handler}, {})`).
Every *other* command is late-registered from `studio/src/studio/knowledge_base/container.py`. There is **no**
cross-cutting domain class diagram, these are infrastructure ports, not entities.
