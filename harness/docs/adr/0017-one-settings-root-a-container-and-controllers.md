---
id: harness-adr-0017-one-settings-root-a-container-and-controllers
aliases: ["harness-adr-0017-one-settings-root-a-container-and-controllers"]
type: adr
app: harness
status: accepted
tags: []
title: "0017: One settings root, one container, and a controller per slice"
---

# 0017: One settings root, one container, and a controller per slice

**Status:** Accepted (2026-09-06).

## Context

This app had no container. Its deployment knobs were nine settings classes, eight in `settings.py` and
`ModelSettings` in `adapters/models.py`, each sitting behind its own `@cache` reader. A cached reader with no
argument answers the same value for the life of the process, whatever changed underneath it, so a section was
whatever the process started with. Nothing could hand one in. Every path this deployment wrote was a module
constant or a module function over `CHECKOUT`, so there was no single field to point somewhere else, and a test
that wanted its own state home had to know all ten path functions by name.

The collaborators with a life longer than a request were module globals installed at boot. `session.py` held
`SESSION`, `POOL`, `REFRESH` and `DECIDE_GATE` behind five installers and two current-value readers,
`adapters/topics.py` held the change channel behind `install`, `current` and `publish`, and
`adapters/session_log.py` wrapped one process-wide log in module functions. A route body fetched what it needed
by calling one of those readers rather than being handed it. The application declared one `Depends` in total,
so the framework's own injection was not what any endpoint used. Routes were module-level functions on eight
`routes.py` modules, each closing over whatever the global readers answered.

The cost landed on the tests. Thirty-five test files patched those globals to say what the code under test
should see, which is a test that reaches into the subject rather than composing it. A patch that leaked past
its own case changed the next one, and three cases were intermittently red for exactly that reason: entering a
lifespan configured tracing with this deployment's real token and started a price-refresh thread that outlived
the block. Nothing said what an endpoint depended on, because an endpoint depended on whatever the process had
installed.

## Decision

**One settings root.** `HarnessSettings` in `settings.py` composes ten mandatory sections: `run`, `store`,
`models`, `self_authoring`, `terminal`, `web_search`, `studio_api`, `knowledge_base_guidance`,
`library_search` and `state`. Sources, highest precedence first, are the init keyword arguments, then `APP_`
prefixed environment variables with `__` between a section and its field, then `config/harness_settings.json`,
which is committed and carries every section. A field may keep a default and an optional scalar such as an API
key stays a scalar. What is banned is an optional section and a default factory, because either one lets a
process start with a section nobody declared. The cached readers are deleted, and a structural test refuses a
new one anywhere under the package. Every durable path this deployment writes is a property of the `state`
section over its `home`, so pointing that one field elsewhere moves the transcripts, the logs, the archive and
the registries together.

**One container, three composition roots.** `create_container(settings)` in `container.py` registers every
section under its own model type through `core.wiring.sections`, the input and output leaves listed in
`adapters/wiring.py`, and each slice's own `REGISTRATIONS` list. Lifetimes are singleton, always, and no
registration names one. `create_app(settings)` in `main.py` builds that container, puts a `ContainerResolver`
on the application state and mounts the controllers. There is no module-level application and no module-level
container, so calling the factory twice yields two independent applications. The ACP entry is the other
composition root and builds the same container. The CLI builds one for the verbs that reach the workflow
library, and every other console verb composes by hand off a settings root of its own, because a console verb
is a process with no container to resolve from. The boot globals are gone: what was installed is now
a registration, the lifespan resolves it once, and the two agent toolsets that reached for the pool take a
`HeldSessions` port resolved late instead. Wireup is imported only in the three entries, `container.py`, the
wiring modules and the controllers, and only a composition root may name a container type.

**A controller per slice.** Each of the eight slices with routes carries a `<Slice>Controller` whose methods
are its routes, with its own `APIRouter` carrying its prefix, mounted through
`wireup.integration.fastapi.setup(..., class_based_handlers=CONTROLLERS)`. Every router that serves HTTP sets
`route_class=WireupRoute`, which is what injects a method parameter. The terminal slice serves one websocket
and needs no route class, since a websocket carries no `APIRoute`. Four slices keep the bare `/api`
prefix because they serve several path families. A controller takes the singletons it always needs through its
constructor and anything that reaches a network as `Injected[Port]` on the method, so what an endpoint depends
on is readable at the top of the class. A slice's `ports` module is its published surface, and a controller may
declare a peer slice's port but never its concrete class. The eight `routes.py` modules are deleted.

**Four gates hold it.** A route baseline pins the served `(method, path)` set at 103 pairs, because the studio
client calls it, and a second gate counts the pairs off the router tree so a pair declared twice fails rather
than being folded away. One request goes through each controller's cheapest route against the real
registrations, so a graph that cannot be resolved fails in the test rather than at the first request in the
browser. The wiring gate refuses a container type outside a composition root, a declared lifetime, a `wireup`
import outside its seams, a settings reader cached for the whole process, and a field a controller stores that
no route reads. The environment-name gate reads `harness/` and `scripts/stack/` whole, decision records
included, and refuses a retired export name wherever it appears.

## Consequences

**The app boots with no environment at all.** The committed defaults file carries all ten sections, so a
deployment exports only what it wants to differ. An export names one field of one section, and
`nested_model_default_partial_update` means it does not have to redeclare the rest of that section.

**Every export changed its spelling.** Before this, each settings class read its own flat name with no section
in it, and one of them read a bare vendor name with no prefix at all. Every one of those is retired outright,
and the environment-name gate refuses any of them wherever it appears in this app's tree or in the stack
scripts, decision records included, which is why this record spells none of them. The exports a deployment
types now are the whole set below, one row per field of every section, so a deployment reading only this file
can rewrite its own environment against it. Anything it still exports that is not on this list is read by
nothing.

| Section | Export | Default |
|---|---|---|
| `run` | `APP_RUN__RUN_ENGINE` | `in_process` |
| `run` | `APP_RUN__TEMPORAL_ADDRESS` | `localhost:7233` |
| `run` | `APP_RUN__TEMPORAL_NAMESPACE` | `default` |
| `run` | `APP_RUN__TEMPORAL_TASK_QUEUE` | `pydantic-ai-workflows` |
| `run` | `APP_RUN__ARCHIVE_DAYS` | `30` |
| `store` | `APP_STORE__HYBRID_SEARCH` | `true` |
| `models` | `APP_MODELS__DEEPSEEK_GATEWAY_ROUTE` | `DeepSeek` |
| `models` | `APP_MODELS__MAX_TOKENS` | `8192` |
| `self_authoring` | `APP_SELF_AUTHORING__CUSTOM_ROOT` | unset, which resolves to `<checkout>/custom` |
| `self_authoring` | `APP_SELF_AUTHORING__SAVED_BY` | `human:owner` |
| `terminal` | `APP_TERMINAL__ENABLED` | `false` |
| `web_search` | `APP_WEB_SEARCH__TAVILY_API_KEY` | unset, which searches keyless |
| `studio_api` | `APP_STUDIO_API__BASE_URL` | `http://127.0.0.1:8100` |
| `knowledge_base_guidance` | `APP_KNOWLEDGE_BASE_GUIDANCE__GUIDANCE` | the retrieval guidance floor in `settings.py` |
| `library_search` | `APP_LIBRARY_SEARCH__CROSS_KNOWLEDGE_BASE_SEARCH` | `false` |
| `state` | `APP_STATE__HOME` | `<checkout>/.state` |

`APP_STATE__HOME` and `APP_SELF_AUTHORING__SAVED_BY` are new and replaced nothing. The three
checkout-geometry names (`APP_ENGRAM_REPO`, `APP_STUDIO_REPO`, `APP_WORKSPACE`) are unchanged, because they
are read at import to find sibling checkouts and are not a section of anything.

The gate matches a whole export name with `[A-Z0-9_]` boundaries rather than a substring, because three of the
retired names are a prefix of the name that replaced them. A substring search would read each replacement as
the retired name inside it and refuse the fix.

**A test composes instead of patching.** It builds its own settings, overrides the one leaf it wants a fake
for, and leaves the rest of the graph real. Test support offers a container with the input and output leaves
already answered, so the common case is a client over an application whose only fakes are the ones that would
otherwise reach a database or the network. Pointing `state.home` at a temporary directory is what isolates a
test's files, one field instead of ten path functions. Entering a lifespan no longer changes the process
either: tracing and the price updater are registrations the lifespan resolves rather than things it starts, so
a test that composes an application ships no spans with this deployment's token and leaves no polling thread
behind it.

**A new slice has four obligations.** It writes a `wiring.py` exporting `REGISTRATIONS`, adds that module to
`WIRING_MODULES` in `container.py`, writes a controller whose router carries its prefix and `WireupRoute`, and
adds the class to `CONTROLLERS` in `main.py`. Its new routes go into the route baseline in the same change,
which is the point of pinning the set: a route appears in the served surface only when somebody wrote it down.

**Two things this does not do.** The layering tiers are unchanged apart from gaining `container.py` and the
wiring modules as composition, so a slice still reaches inward and into itself and never into a peer. And a
section is registered under its own model type, not under the settings root, so a consumer the container
resolves declares the slice of the configuration it reads and never the root. The console verbs in
`features/selfauthoring/routes_cli.py` are the one exception: four of them build a `HarnessSettings` of their
own, because a console verb is a process with no container to resolve a section from.
