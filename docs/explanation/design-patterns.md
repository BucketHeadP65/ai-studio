---
id: design-patterns
aliases: [design-patterns]
type: explanation
app: root
status: current
tags: [patterns]
title: "Design patterns: why the code is shaped this way"
---

# Design patterns: why the code is shaped this way

The repo-wide patterns, each with intent, where it's used, and the trade-off
that was accepted. Slice reference docs link here from their "Patterns used"
sections. The contracts themselves live in those reference docs, and the
decision records in the ADRs.

## Vertical-slice DDD

- **Intent:** keep each feature's whole stack (contracts, persistence,
  service, HTTP) in one package, so a change is reviewed in one place and
  slices can be built by parallel agents without shared-file conflicts.
- **Where used:** `engram/src/features/{compile,conversion,mirror,serve,sync,zotero}`,
  and the same shape in a sibling app. Each slice: `domain.py` (pydantic contracts +
  protocol ports), `deps.py` (wireup providers), `repository`/`orm`, `service`
  or `handlers`, `endpoints`.
- **Advantages:** locality of change. Protocol ports make cross-slice calls
  explicit. Per-slice test trees.
- **Disadvantages:** some duplication across slices. Cross-slice features need
  a deliberate seam (cross-slice calls go through `domain.py` models and
  protocol ports, not direct imports of another slice's internals).
- **See also:** [ADR 0003](../adr/0003-absorb-zotero-sync-into-wiki-app.md)
  (preserving slice isolation when the Zotero sync tool was absorbed).

## CQRS message bus

- **Intent:** decouple engram's ingestion pipeline into commands with one
  handler each and observe-only events, so pipeline stages can grow without
  entangling callers.
- **Where used:** the kernel lives in the shared library as `core.cqrs`
  (`{bus,messages,uow,worker,postgres,memory,events}.py`), extracted from
  engram's `src/cqrs/` and consumed unchanged (root ADR-0006), frozen pydantic
  `Command`/`Event` bases. A handler records its events on its unit of work, or
  leaves them on its own `.events` list for `core.cqrs.bus` to hand to the
  outbox after it returns. Either way the events land in the
  `coordination.outbox` table with the transaction, and `core.cqrs.worker.Worker`
  delivers each once per subscriber (`core.cqrs.postgres` and `core.cqrs.memory`
  hold the Postgres and in-memory adapters), recording each delivery in
  `coordination.event_deliveries`. `core.cqrs.events` separately broadcasts live
  change topics over Postgres LISTEN/NOTIFY for readers to refetch by. Subscriber
  failures on the delivery path are isolated (logged, not surfaced).
- **Advantages:** framework-pure, additive late-route seam
  (`MessageBus.register`). The outbox survives a crash between the handler
  returning and delivery. A natural place for job orchestration.
- **Disadvantages:** raises the onboarding bar (a reader used to plain
  request/response services won't pattern-match it). Hidden ordering
  dependencies are possible. The sync slice's `resolve_sync_bus` funnel exists
  precisely because routes register as a side effect of handler resolution.
- **See also:** [ADR 0002](../adr/0002-wiki-cqrs-bus-and-wikinote-kernel.md)
  (the bus's original engram home), [ADR 0006](../adr/0006-engramai-shared-ai-library.md)
  (its extraction into the shared library, originally `engramai`, since
  consolidated into `core`).

## DI seams and fakes over mocks

- **Intent:** every dependency arrives through a constructor or container
  seam, so tests inject hand-written fakes with scriptable public state.
  Never monkeypatches, never framework-echo mocks.
- **Where used:** composition roots `engram/src/container.py` and a
  sibling app's container (wireup. Slices contribute `@wireup.injectable`
  factories from `deps.py`). FastAPI via `wireup.integration.fastapi`. The
  CLI's `ctx.obj` zero-arg container-factory seam. Narrow protocol ports
  (`SupportsCommit`, `SqlGateway`, `VaultReader`, `BatchEmbedder`). Fakes under
  each app's `tests/support/fakes/`. Polyfactory factories for arrange data.
- **Advantages:** tests state intent (a fake's state is the assertion
  surface). No patch-target string coupling. Container overrides give
  integration tests real wiring minus the expensive edges.
- **Disadvantages:** fakes must be kept honest against the real adapter
  (integration tiers exist for that). More upfront design than `patch()`.

## Ports vs interfaces: Protocol, ABC, or callable

- **Intent:** three ideas that look interchangeable answer different questions,
  and keeping them apart decides how a collaboration is typed. An *interface* is
  a design role (what operations a collaboration involves). A *seam* is a place
  where behaviour can be substituted without editing the code at that point
  (Feathers). Dependency injection is what creates it. A *Protocol* or *ABC* is
  the language mechanism that enforces the shape. They co-occur, so the load-
  bearing distinction is **who owns the contract**:
  - An **interface** is defined by its implementations, a family authored in
    one place and expected to grow, with the contract living on the provider side
    (each concrete declares "I am a `SearchDialect`"). This is the
    [Strategy registry](#strategy-registry) case.
  - A **port / seam** is defined by its consumer's need, the narrowest slice
    the caller uses, the contract living on the consumer side, satisfied by a
    provider that need not know the port exists. This is Interface Segregation
    and Dependency Inversion taken literally: the abstraction is owned by the
    high-level consumer and there is no import edge from the concrete back up to
    it. `SupportsRows`, `SupportsExecute`, and `SupportsCommit` are ports, one
    method, named for what the caller wants.
- **Protocol (structural) vs ABC (nominal):**
  - **Protocol**, conformance by shape. The provider needn't import or know it.
    Stdlib and vendor types conform for free. No coupling edge. The default for
    consumer-owned ports and anything injected for a test.
  - **ABC**, conformance by explicit inheritance or registration. Can ship
    shared concrete / mixin methods. Enforces "you opted in" at instantiation.
    Reserve it for a family authored here that also wants to share
    implementation. A closed app rarely needs one (structural ports suffice). A
    *shared library* whose value is extend/reuse/override wants ABCs far more.
    An ABC that owns invariant behaviour with a few overridable hooks (Template
    Method) hands a consumer reuse, where a bare Protocol would force them to
    reimplement the whole flow. The ergonomic move at a library extension point
    is to ship both: the Protocol as the true contract and an ABC base
    implementing it with hooks and sane defaults, so a consumer either brings
    their own from scratch or subclasses the base and overrides one method.
  - **`@runtime_checkable`** grants a Protocol `isinstance`, but it checks method
    *names* only, not signatures. A weak guard, fine for a registry-binding
    sanity check, not a contract test. One case where it is genuinely
    load-bearing rather than ceremony: a DI container that resolves by
    `container.get(SomeProtocol)` runs `isinstance(service, SomeProtocol)`
    internally, so any Protocol fetched that way (wireup `.get()` does this)
    *must* be `@runtime_checkable` or resolution raises `TypeError`. There the
    container is the isinstance user even though no app code names it, so
    "nothing checks it" is the wrong test for whether the decorator is dead.
- **Single-method port: Protocol or a `Callable` alias?** Decide on two axes:
  does the name carry meaning, and does the argument shape need keywords,
  defaults, or overloads?
  - Faceless "just call it" with simple positional args becomes a `Callable` alias
    (`type Clock = Callable[[], datetime]`). A Protocol would be ceremony, and a
    lambda, closure, or bound method all fit.
  - A named method called as `x.verb(...)` gives a single-method **Protocol**. The
    method name is the documentation. `Callable[[str], User]` throws it away and
    the call site loses all intent.
  - Keyword-only args, defaults, or overloads gives a **`__call__` Protocol** as the
    only faithful option: `Callable[[str], T]` cannot express keyword arguments,
    defaults, or overloads. There is no syntax for it. `SupportsRows.rows(sql,
    params=None, *, cap=50)` earns its Protocol on both axes (named method plus
    a keyword-and-default argument) and would be unrepresentable as a `Callable`.
  - Corollary: a Protocol whose only member is `__call__` with plain positional
    args is a named `Callable` written the long way. Collapse it to the alias
    unless the signature needs what `Callable` cannot say, or the port is a DI
    injection key. Several zero-arg or single-arg factories share one structural
    `Callable` type. A container distinguishes and resolves them by *nominal*
    type, so each must stay a named Protocol (a `Callable` alias would collide),
    and if it is fetched via `container.get(...)` keep it `@runtime_checkable`
    (see above).
- **Advantages:** the consumer states exactly the slice it depends on. Providers
  stay decoupled. A fake conforms by shape with no cast. The narrowest port is
  the smallest surface a fake must implement.
- **Disadvantages:** many tiny single-method ports proliferate. A name-only
  structural match can pass `@runtime_checkable` while mismatching a signature.
  Each port must be kept honest against the real adapter (the integration tiers
  exist for that).
- **See also:** [DI seams and fakes over mocks](#di-seams-and-fakes-over-mocks)
  (the injection discipline these ports serve),
  [Strategy registry](#strategy-registry) (the provider-family case), root
  `CLAUDE.md` section Type Hints (tight ports).

## Zod at the boundary

- **Intent:** make backend drift loud. Every HTTP response is `.parse()`d
  against a zod schema at the fetch boundary, so a contract change surfaces as
  a `ZodError` there instead of `undefined.something` deep in a component.
- **Where used:** the typed fetch factory `client-core` `api/client`
  (`createApiClient`). Schemas in `studio-client/src/api/` and the
  sibling client's. Never `as`-casts. The Python analogue is
  `BaseModel(frozen=True, extra="ignore")` for response shapes at httpx
  boundaries.
- **Advantages:** drift detection at the seam. Parsed types flow inward with
  no casts.
- **Disadvantages:** schema duplication against the server's OpenAPI (the
  clients mitigate with generated `schema.d.ts` + zod on top). A parse
  cost per response.
- **See also:** root `CLAUDE.md` section API response validation,
  [ADR 0004](../adr/0004-client-core-shared-frontend-package.md).

## Immutable mirror

- **Intent:** treat derived copies of external sources as disposable,
  immutable artifacts, never edited in place, so the upstream system stays
  the single source of truth and rebuilds are always safe.
- **Where used:** engram's `Sources/` paper mirrors (Zotero is the source of
  truth. Mirrors are title-named, identity stays the `zotero_key`),
  `engram/src/features/mirror/store.py`. The same stance scales up to the whole
  engram DB, a disposable derivative of the knowledge base rebuilt by `studio index`.
- **Advantages:** no merge problem between upstream and derivative. Sync and
  indexing are idempotent reconciles. Deletion is cheap recovery.
- **Disadvantages:** any local annotation on a mirror is lost by design.
  Rebuild cost grows with the knowledge base (mitigated by hash-gated embedding).
- **See also:** engram ADRs
  [0002](../../engram/docs/adr/0002-disposable-mirror-zotero-source-of-truth.md)
  and [0004](../../engram/docs/adr/0004-title-named-mirrors.md).

## Strategy registry

- **Intent:** when one port has several provider implementations, bind them in
  a frozen dataclass wrapping `Mapping[Enum, Adapter]` at the composition
  root. Adding an implementation is one more row in that mapping, not an `if` chain.
- **Where used:** the dual-dialect BM25 seam, a `SearchDialect` Protocol bound
  through a `SearchDialects` registry (now a thin subclass of the generic
  `core.ai.registry.Registry[K, V]`, root ADR-0006) that selects the ParadeDB
  vs `pg_textsearch` builder, each of which *generates* its BM25 SQL from a
  `CorpusSchema` descriptor rather than hand-writing it (see
  [Descriptor-driven SQL generation](#descriptor-driven-sql-generation), engram
  ADR-0035), selected by `WIKI_DB_DIALECT` (see
  [tech-stack.md section on the managed Postgres target](tech-stack.md#managed-postgres-target-and-the-dual-dialect-bm25-seam)).
  The dual graph backend (the relational `cte` walk vs Apache AGE, engram ADR-0007) and the
  evals' named retrieval-strategy axis (engram ADR-0012) are the same pattern on
  other closed sets.
- **Advantages:** closed for modification at call sites. The registry is a
  single override seam in tests. Exhaustiveness is checkable against the enum.
- **Disadvantages:** indirection for the two-implementation common case. Per-
  dialect config still has to be threaded to each adapter's factory.
- **See also:** root `CLAUDE.md` section Strategy Registry Pattern,
  [Descriptor-driven SQL generation](#descriptor-driven-sql-generation) (what the
  dialect/backend adapters now generate their SQL from).

## Descriptor-driven SQL generation

- **Intent:** when the *same* SQL skeleton must serve more than one knowledge base, lift
  the knowledge-base-specific tokens out of the SQL text into a declarative descriptor and
  *generate* the SQL from it, so a new knowledge base is data rather than a hand-written
  copy. This is the Tier-2 inversion beyond the [Strategy registry](#strategy-registry):
  the registry abstracts the DB-engine axis but leaves the knowledge base baked into the
  string. The descriptor lifts the knowledge base out too.
- **Where used:** `core.ai.retrieval`, `SearchSchema` /
  `GraphSchema` descriptors carry the searchable surfaces, facet pools, facets, the
  tuning relation, and the graph edge/node shape. `BaseHybridSearchBuilder` / `BaseGraphTraversalBuilder`
  ABCs own the invariant skeleton (weighted RRF fusion over the tuned constant, the leg
  budgets, the `k` cap, the rank policies, the per-surface legs, the reached-node walk and
  its hop caps, the RETURNS contract) with a dialect hook for the BM25 grammar and a
  backend hook for reading one hop. engram supplies `KB_SEARCH_SCHEMA` / `KB_GRAPH_SCHEMA`
  (`engram/src/engram/retrieval/{knowledge_base_schema,graph_schema}.py`) and its
  dialect/backend adapters delegate body generation to the builders, so
  `api.search` / `api.graph_*` are generated, not hand-written.
- **Advantages:** the knowledge base is data (a second consumer brings its own descriptor).
  The invariant skeleton lives once. engram serve is a thin adapter. The
  Protocol+ABC pairing (see [Ports vs interfaces](#ports-vs-interfaces-protocol-abc-or-callable))
  lets a power user satisfy the Protocol from scratch while most subclass the base
  and override one hook.
- **Disadvantages:** the generated SQL *text* differs from a hand-written body, so
  no-regression is proven behaviourally (result parity on every DB bed), not by
  text-identity. The descriptor must faithfully carry every schema-specific token
  or the generated SQL is wrong (caught by the parity tiers).
- **See also:** [Strategy registry](#strategy-registry) (the DB-engine axis this
  extends), [Ports vs interfaces](#ports-vs-interfaces-protocol-abc-or-callable)
  (the Protocol+ABC library-extension-point rule), engram ADRs
  [0035](../../engram/docs/adr/0035-retrieval-sql-from-descriptors.md) (the seam) and
  [0036](../../engram/docs/adr/0036-behavioral-parity-generated-sql.md) (the parity
  gate), root [ADR 0006](../adr/0006-engramai-shared-ai-library.md) (the original
  library split, since consolidated into `core`).
