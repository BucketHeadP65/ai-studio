---
id: engram-adr-0021-explicit-eager-composition-root
aliases: ["engram-adr-0021-explicit-eager-composition-root"]
type: adr
app: engram
status: accepted
tags: []
title: "ADR-0021 — Explicit eager composition root (the declarative bus route table)"
---

# ADR-0021 — Explicit eager composition root (the declarative bus route table)

**Status:** Accepted (2026-06-30). Delivered in the `engram-domain-remediation` initiative (Slice A, the
composition-root refactor, tasks A1 through A7). Reworks the CQRS bus assembly first established with the
app placement and core split ([root ADR-0001](../../../docs/adr/0001-wiki-app-placement-and-core.md)) and the
configuration-via-`application_settings.json` decision ([ADR-0008](0008-configuration-via-application-settings-json.md)),
and is consumed unchanged by the incremental-ingestion command path
([ADR-0019](0019-true-incremental-ingestion.md)). Scope: the composition root (`src/container.py`), the
settings entry point (`src/settings.py::fetch_application_settings`), the dispatchers
(`src/cli.py`, `src/cli_support.py`, `src/cli_evals.py`, `features/serve/http.py`), and the Ingestion slice
`deps` modules (`features/conversion/deps.py`, `features/zotero/deps.py`, `features/mirror/deps.py`,
`features/sync/deps.py`). No schema, `api` SQL surface, or `wiki_reader` role change. Findings F6, F49, F65,
F66, F59, F64, F20, F44, F50.

**Context.** The CQRS `MessageBus` was assembled by side effects rather than declared. The compile slice
*built* the bus inside its own provider with only `CompileVault` routed, and the four other command handlers
(`SyncMirrors`, `ExportOkf`, `GenerateEvalDataset`, `CalibrateRetrieval`) plus the `PapersChanged`
QA-generation subscriber late-registered themselves onto that already-built bus via `bus.register` and
`register_subscriber` calls scattered through their own wireup providers in the composition root. Because the
bus existed before it was fully routed, the dispatchers could not simply resolve it. They funnelled through
four `resolve_*_bus` helpers whose only job was to force the handler-resolution-before-bus ordering the late
seam required, so the resolution order was load-bearing and invisible. Vault selection had a parallel problem:
choosing a vault mutated `os.environ["APP_VAULT__PATH"]` and the settings fetch was a zero-argument call behind
an `lru_cache(maxsize=1)`, so a second vault path requested in the same process was silently ignored (the first
result stayed cached, a stale-cache bleed). Separately, the three Ingestion adapter slices (conversion, zotero,
mirror) had no `deps` of their own. The driving constraint, confirmed with the user, was that the bus must be
reorder-safe and explicit, two vault paths must resolve independently in one process, and each fact about how a
command reaches its handler must have exactly one visible home rather than being reconstructed from a trail of
`register` side effects.

**Options considered.**

- **Keep the late-registration seam, document the ordering.** Rejected. The ordering stays load-bearing and a
  new handler that forgets to register is silently unroutable at runtime rather than caught at construction, the
  exact fragility the remediation findings flagged.
- **Lazy bus assembly behind a one-shot guard.** Build the full route set the first time the bus is dispatched.
  Rejected. It trades the registration trail for hidden first-call work and a new ordering hazard (the guard),
  and it still cannot be asserted as a pure data structure at construction.
- **A declarative eager route table built at the composition root (chosen).**

**Decision.**

Four load-bearing choices carry the refactor.

**1. The bus is fully routed at construction from declarative tables.** `src/container.py` builds the
`MessageBus` from one `{CommandType: handler}` route table (`build_command_routes`) and one
`{EventType: [subscriber]}` subscription table (`build_event_subscriptions`) at container creation. Both
builders are pure dict constructions with no bus or container coupling, so the route set is asserted directly as
data. There is no `bus.register` or `register_subscriber` late mutation anywhere, the full route set is fixed
the moment the singleton is created, and there are no `resolve_*_bus` funnels because the resolved bus is already
complete.

**2. Handler and subscriber providers are plain and live in their owning slices.** Each command handler and the
QA-generation subscriber is a plain wireup factory in its own slice's `deps` module (no bus dependency), and
`container.bus_provider` is the sole `MessageBus` provider. It injects the five handler singletons plus the
subscriber by type, then hands them to the two table builders, so the composition root keeps only the
cross-slice bus wiring. Adding a slice is one new plain provider in its slice plus one entry in
`build_command_routes`, with no scattered registration to trace.

**3. Application settings are threaded explicitly, cache-free, with no environment mutation.**
`fetch_application_settings(vault_path)` builds a fresh `ApplicationSettings` tree on every call (no
`lru_cache`), threading an explicit `vault` sub-instance in when a `vault_path` is given so the selection never
mutates `os.environ`, and the rest of the tree still resolves from the JSON config and `APP_*` env. The
`json_file` test seam that retargets the single `JsonConfigSettingsSource` for hermetic callers is preserved.
Two different vault paths requested in one process therefore yield two independent settings objects.

**4. Each Ingestion adapter slice owns its `deps`, and `sync` composes ports only.** `conversion_deps`
contributes the hybrid PDF-to-Markdown converter, `zotero_deps` the Zotero repository, and `mirror_deps` the
Obsidian mirror store. The sync slice's `deps` then composes those three ports and contributes only its plain
`sync_handler_provider`, so adapter ownership lives with the adapter and the orchestrating slice wires ports
rather than constructing adapters.

**Consequences.**

- (+) **The composition root is explicit and reorder-safe.** A command's binding to its handler lives in exactly
  one place (`build_command_routes`), the bus is complete at construction, and a missing route is a visible gap
  in a data table rather than a forgotten side effect.
- (+) **Dispatchers resolve the bus directly.** `container.get(MessageBus)` (CLI) and `_resolve(request,
  MessageBus)` (serve) return a fully routed bus, so the four `resolve_*_bus` funnels and their implicit ordering
  contract are gone.
- (+) **Two vault paths resolve independently in one process.** The cache-free, environment-mutation-free
  `fetch_application_settings(vault_path)` removes the stale-cache bleed, so a second vault path is honoured.
- (−) **Every handler is eagerly constructed when the bus is built**, even when a single command is dispatched.
  This is accepted because the handlers are objects, not work, the route table is cheap wiring and needs no DB
  connection to assemble, so eager construction costs object allocation rather than I/O.
- **Reversible?** Yes, additively. The bus could fall back to a late-registration seam, the tables are pure data
  that any future assembly strategy can reuse, and no schema, `api` surface, or `wiki_reader` role was touched.

**Note.** The plan anticipated removing an F65 "QA-subscriber warm-up" hop, but no such warm-up existed in the
code, so there was nothing to remove. The remaining `os.environ["APP_VAULT__PATH"]` write in
`src/cli_init.py` is the fresh-vault init-scaffolding bootstrap (it runs before any settings tree exists), not
the runtime vault-selection settings fetch this ADR made cache-free, and is intentionally left in place.

**Related.** [root ADR-0001](../../../docs/adr/0001-wiki-app-placement-and-core.md) (the app placement and the
CQRS bus this reworks), [ADR-0008](0008-configuration-via-application-settings-json.md) (the
`application_settings.json` configuration the explicit settings threading preserves),
[ADR-0019](0019-true-incremental-ingestion.md) (the incremental-ingestion command path this composition root
now routes). Code: `src/container.py` (`build_command_routes`, `build_event_subscriptions`, `bus_provider`,
`create_container`), `src/settings.py` (`fetch_application_settings`), `src/cli.py` / `src/cli_support.py` /
`src/cli_evals.py` / `features/serve/http.py` (the dispatchers resolving `MessageBus` directly), and
`features/conversion/deps.py` / `features/zotero/deps.py` / `features/mirror/deps.py` /
`features/sync/deps.py` (the Ingestion `deps` split). Patterns:
[CQRS message bus](../../../docs/explanation/design-patterns.md#cqrs-message-bus) (the bus this assembles).
Entry point: [CONTEXT-MAP.md](../../../CONTEXT-MAP.md). Glossary: [engram/CONTEXT.md](../../CONTEXT.md).
