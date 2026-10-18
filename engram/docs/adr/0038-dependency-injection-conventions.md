---
id: engram-adr-0038-dependency-injection-conventions
aliases: ["engram-adr-0038-dependency-injection-conventions"]
type: adr
app: engram
status: accepted
tags: []
title: "ADR-0038 — Dependency-injection conventions"
---

# ADR-0038 — Dependency-injection conventions

**Status:** Accepted (2026-08-05).

**Context.** [ADR-0021](0021-explicit-eager-composition-root.md) gave engram its composition-root
shape: one `create_container`, settings decomposed into capability-group instances, each slice
contributing providers through its own `deps` module, the bus fully routed at construction. What it
did not do is write down the *conventions* that keep that shape healthy — they lived as habit and
scattered `deps.py` docstrings. Unwritten conventions decay exactly where they matter: a new slice,
a new contributor, an agent working from the code alone. Three concrete gaps forced the issue:

- **Nothing proved the container states only truths.** A registration whose DI key nothing can
  resolve — a factory returning a PEP 695 type alias, wireup happily keying on the alias object —
  sat in the graph with a fully green suite. A behavioural suite cannot see a dead registration;
  only resolving every registered key can.
- **Nothing owned the container's end of life outside the HTTP surface.** The serve lifespan
  disposed its engines and closed its container; no CLI command did, and the long-lived
  `engram mcp` stdio server resolved eagerly at startup with no shutdown hook at all. A provider
  that owns a real resource is only honest if disposal actually runs — one whose cleanup has no
  caller is indistinguishable from one that never cleans up.
- **One slice reached upward for the container itself.** The calibration evaluator factory
  imported the wireup container type and the app-level adapter into a feature slice — a defensible
  need (per-round late resolution) expressed through the wrong port.

**Options considered.**

- **Registration by decorated classes** (annotate services themselves, scan the slice packages).
  Rejected: it puts a framework import into every service module, and engram's testing doctrine —
  structural fakes over the `Resolver` port, framework-pure handlers — depends on slices that do
  not know wireup exists. It also has a lint trap (see the factories-only rule below).
- **Qualifier-based disambiguation** for same-type-twice registrations. Rejected already by
  [ADR-0025](0025-intentional-deviations-register.md) F12/F36: qualifier annotations at every
  injection site couple more than two one-line wrapper types do.
- **Conventions as prose only.** Rejected: prose decays silently. Every rule below that can be
  checked mechanically is checked mechanically (`tests/unit/test_di_gates.py`), so drifting from a
  convention fails a test naming this ADR rather than surfacing in review, or never.

**Decision.** The following conventions bind engram's DI composition. The structural gates in
`tests/unit/test_di_gates.py` are this decision's teeth; changing a convention means amending this
ADR and its gate together.

### 1. Registration shape

| What is being provided | Shape |
|---|---|
| A settings capability group (`VaultSettings`, `DatabaseResource`, …) | `wireup.instance(sub, as_type=T)` at the composition root; the root `ApplicationSettings` is **never** registered — providers ask for exactly the concern they need |
| Anything else (adapters, handlers, engines, factories-of-resources) | a **named factory function**, `@injectable(lifetime="singleton")`, in the owning slice's `deps.py` — the only DI-aware module in a slice |
| The same type in two roles (reader vs admin engine/gateway) | a wrapper or phantom subtype as the second DI key (`ReaderEngine`, `ReaderSqlGateway` — ADR-0025 F12/F36), never a qualifier |
| A session-bound collaborator of a bus singleton | a factory-of-scoped-resource: the provider returns a small factory object whose `__call__` is an async context manager opening one session per command |
| A collaborator that must be resolved **per invocation**, not at wiring time | inject the `Resolver` port (registered once, at the composition root, by `resolver_provider`); every such seam needs its own ADR-0025 row — F43 (the calibration evaluator) is the only one |

### 2. Factory functions only — never `@injectable` on a class

Two reasons, one obvious and one not:

- Slices stay framework-pure. Handlers and adapters take plain constructor arguments; the only
  module in a slice that imports wireup is `deps.py`. The `Resolver` port and the structural fakes
  in `tests/support/fakes/` depend on this.
- The lint guarantee only holds for functions. `pyproject.toml` lists `wireup.injectable` under
  ruff's `runtime-evaluated-decorators`, which keeps a decorated **function's** own parameter and
  return annotations imported at runtime (wireup evaluates them at registration). That setting does
  **not** reach a decorated **class's** `__init__` annotations — so a decorated class would let a
  well-meaning `TYPE_CHECKING` import move break container resolution while staying lint-clean.
  The factories-only rule removes that failure mode wholesale instead of policing it.

### 3. Lifetime: singleton, explicitly, and nothing else today

Every factory declares `lifetime="singleton"` explicitly. Anything genuinely shorter-lived gets it
through an explicit factory (shape 4 above) or by opening its own session — never through a
container lifetime. Justify a future `scoped` registration by genuine per-request **state**, never
by wanting fresh values — freshness belongs to the wrapped resource's own refresh or pooling
policy, not to the DI lifetime. The gate refuses `scoped`/`transient` until this ADR is amended.

### 4. Lifecycle: whoever builds a container owns its end of life

All owners end their container through one path, `container.dispose_container` (each engine
disposed in isolation, tolerant of a never-resolved or already-broken engine, then `close()`):

- the **HTTP lifespan** disposes in its `finally` (a test-injected container stays the test's);
- the **CLI seam** (`_build_container`) registers disposal on the command context's close, which
  covers a clean run, a `typer.Exit`, a crash, and `engram mcp` returning from its stdio loop —
  including the case where eager startup resolution fails partway, where whatever already resolved
  would otherwise leak with no owner;
- **tests** that inject a container through `ctx.obj` or `create_app(container=…)` own it.

The engine providers' pgvector-codec registration is the composition's one deliberate
resolve-time connection; the gate documents it and overrides both engines in its sweep.

### 5. Test overrides

- **Unit, real graph:** build the real `create_container` from fully explicit settings and use the
  context-managed `container.override.injectable(target=…, new=…)` on the IO leaves. Override the
  **target itself**, never its leaf dependencies — a resolved singleton is cached, so leaf-dep
  overrides go order-dependent across tests.
- **e2e:** the real container with `container.override.set(…)` for the leaves that must stay inert
  (the embedder), everything else production — so a provider gaining a parameter cannot silently
  diverge from what the test runs.
- **Hand-rolled fake containers** (the `ctx.obj` seam) only where resolution *order* is itself the
  assertion; they must not mirror production wiring by hand where the real graph would do.

**Consequences.**

- (+) The container states only truths: a dead or unresolvable registration fails the sweep by
  construction, with the failure naming the registration.
- (+) The conventions survive contributors and agents: each rule is a failing test away from being
  noticed, not a review comment away from being missed.
- (+) Every container built by any entrypoint now actually ends: the resource-owning providers'
  cleanup contract is real on the CLI and MCP paths, not only under HTTP.
- (−) The gates add deliberate friction: a legitimate future `scoped` lifetime, qualifier, or
  second late-resolution seam costs an ADR amendment plus a gate change. That cost is the point.
- (−) CLI disposal runs on a fresh event loop after the command's own loops are gone, so the
  engine pools there are **released, not network-closed** (`close_connections=False`) — a pooled
  connection's transport belongs to its dead loop, and a graceful close from a fresh loop raises
  into that loop's callbacks (observed live as unraisable tracebacks after a clean command).
  Connections are reclaimed at process exit; the exit stack still unwinds; the HTTP lifespan,
  disposing on its own loop, keeps the full close.

**Related.** [ADR-0021](0021-explicit-eager-composition-root.md) (the composition-root shape these
conventions keep healthy), [ADR-0025](0025-intentional-deviations-register.md) (F12/F36 wrapper
types; F43 the sanctioned late-resolution seam),
[ADR-0012](0012-retrieval-strategy-and-code-mode-axes.md)
(why the retrieval toolset is built per run rather than registered). Gates:
`tests/unit/test_di_gates.py`. Lifecycle: `src/container.py::dispose_container`,
`src/cli_support.py::_build_container`, `src/features/serve/http.py` (lifespan).
