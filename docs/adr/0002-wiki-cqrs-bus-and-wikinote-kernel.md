---
id: adr-0002-wiki-cqrs-bus
aliases: ["adr-0002-wiki-cqrs-bus"]
type: adr
app: root
status: accepted
tags: []
title: "0002 — Wiki CQRS message bus and the `wikinote` shared kernel"
---

# 0002 — Wiki CQRS message bus and the `wikinote` shared kernel

- **Status:** accepted
- **Superseded in part:** on 2026-08-26 by [core ADR-0001](../../core/docs/adr/0001-coordination-layer.md). Events are
  no longer deferred, the bus hands them to a Postgres outbox and a worker delivers them.
- **Date:** 2026-06-11
- **Deciders:** repository maintainers (wiki-rag feature)
- **Supersedes / Amends:** none
- **Related:** [[0001-wiki-app-placement-and-core]] (the wiki app's placement and the `core` shared package — this ADR records its *internal* architecture), and the vault-side counterpart **D-22** in `_meta/design/decisions.md` (the authoring-contract change that follows from the shared kernel)

## Context

[[0001-wiki-app-placement-and-core]] settled *where* the wiki serving layer lives — its own top-level app `engram/`, sharing infrastructure through the concrete `core` package. This ADR settles the wiki app's **internal architecture**: how its write side is structured, and where the note grammar lives. Two questions, decided together because they share one motivation — *the ingestion pipeline will grow, and we want its complexity to land additively rather than as a restructure*:

1. **How is the write side organised?** The compiler is not a simple function call. It is a five-stage pipeline (parse → resolve → validate → load → embed) that today runs synchronously and end-to-end, but that the plan (§Context item 3) expects to grow event-driven: re-embedding on demand, partial recompiles, an outbox feeding a queue. We have to choose a write-side shape *now* that absorbs that growth without a rewrite later.
2. **Where does the note grammar live?** The binding authoring rules from the vault's `_meta/schema.md` (page types, frontmatter fields, canonical H2 sections, the block-anchored claims grammar, registry vocabulary) are consumed in **two** places: the compiler must *parse* existing `.md` files into structured data, and the new authoring CLI must *construct and validate* new `.md` files from extractor payloads. If those two consumers each encode the grammar independently they will drift, and the whole point of programmatic authoring ("validated files, not trusted prose") collapses.

Constraints in play:

- **CQRS is a new pattern here.** Introducing it is a **deliberate choice**, so it needs its rationale recorded, and it needs to stay framework-pure and small enough to justify its weight.
- **A shared kernel is a familiar idea**: one explicit module that several consumers import from. A note-grammar kernel is that idea at larger scale, one explicit module both the compiler and the authoring CLI depend on.
- **Events are deferred, not designed.** The plan is explicit: *"CQRS + repository + alembic now; events later."* No outbox, queue, scheduler, or worker is built in v1. The bus is the seam that makes "later" cheap — so its commit/dispatch ordering has to be right from day one even though nothing yet subscribes to the events it raises.
- **The kernel must touch nothing heavy.** A grammar parser/renderer imported by an authoring CLI cannot drag in SQLAlchemy, asyncpg, or wireup. It is stdlib + pydantic + Markdown libraries only, exactly so the authoring path stays light and the compiler can reuse it without circular dependency.

Tasks 2.5 and 2.6 have already landed the bus kernel against decision (a): `engram/src/cqrs/{messages,bus,uow}.py` exist (commit `eb78b6e`, with its red-phase contract specs in `f8104e1`), and the migrations that the eventual handlers will write through landed in `de085c4`. This ADR records the *why* behind that kernel as built, and the design intent of the `wikinote` shared kernel — which does **not** exist yet (slice C builds it), so part (b) below is written in target-state terms per the plan skeleton.

## Decision

We make **two decisions** for the wiki app's internal architecture.

### (a) CQRS with an in-process message bus and a UnitOfWork port

The write side is structured as **Command Query Responsibility Segregation** in the Cosmic Python idiom:

- **Frozen Command and Event models.** `cqrs/messages.py` defines a `Message` pydantic base (`model_config = ConfigDict(frozen=True)`) and two markers, `Command` (an intent handled by exactly one handler) and `Event` (a fact emitted during handling, observed by zero or more subscribers afterwards). Immutability means a message can be passed to a handler, recorded, and replayed without any leg mutating it underneath another.
- **One handler class per command.** A command is routed to **exactly one** handler. The compiler's `CompileVault` command maps to a single `CompileVaultHandler` that runs the five pipeline stages (slice D builds it). The bus raises `KeyError` if a command arrives with no registered handler — there is no fan-out on the command side.
- **Events collected during handling, dispatched *after* the handler returns.** This is the load-bearing ordering decision. The handler accumulates events as it works, but the bus does not fan them out until the handler has returned — so subscribers observe the state the handler **already committed**, never a half-finished one. Future event-driven ingestion (an outbox row written in the same transaction, drained to a queue) becomes an **additive** change to the dispatch mechanism, not a restructure of handlers or commands.
- **A `UnitOfWork` port** (`cqrs/uow.py`) defines the transactional boundary as a structural `typing.Protocol` (`runtime_checkable`): `async __aenter__/__aexit__/commit`, with the classic contract *roll back on exit unless the caller committed first*. Being a Protocol, the production SQLAlchemy adapter (`features/compile/uow.py`, slice D) and the test fakes both satisfy it without importing or subclassing anything. The compile slice's `WikiUnitOfWork` extends it with per-aggregate repositories exposed as attributes (the Cosmic Python shape).

**As-built conventions, documented from `engram/src/cqrs/bus.py` (commit `eb78b6e`)** — the kernel exists, so these are the code as it is, not a sketch:

- **Handler-owned `.events` collector, drained post-handler.** Each command handler exposes an `events` collector — a plain `list[Event]` it appends to during handling. After `await handler(command)` returns, the bus *drains* that collector (reads it, then clears it) and only then dispatches. This is why subscribers see committed state: the drain happens strictly after the handler — and therefore after its `UnitOfWork.commit()` — has returned. The handler result (e.g. a `CompileReport`) is returned to the caller **after** the drain completes.
- **The `runtime_checkable` `_EventCollector` probe.** The "exposes an `events` collector" surface is a `runtime_checkable` `Protocol` (`_EventCollector`, a read-only `events: list[Event]` property). `CommandHandler` composes it (a handler *always* has events — that is where the initial batch comes from). Subscribers do **not** have to: a subscriber that only observes is a bare async callable, and the bus drains a subscriber's cascade only when an `isinstance(subscriber, _EventCollector)` check passes. This keeps the simple subscriber case a one-line `async def` while still supporting cascades.
- **FIFO cascade drain.** `_drain` works a `collections.deque` FIFO: it pops an event, dispatches it to each subscriber registered for that event's concrete type, and — for any subscriber that *is* an `_EventCollector` — extends the queue with whatever further events that subscriber raised. A cascade therefore fully resolves within one `handle` call, in emission order.
- **Subscriber isolation.** `_dispatch` wraps each subscriber call in `try/except Exception`, logging via `logger.exception` and continuing. **One failing subscriber neither aborts the command result nor blocks its siblings** — the command has already committed by the time events dispatch, so a listener failure is an observation problem, not a transactional one. (The command handler's own exceptions are *not* swallowed — they propagate out of `handle`, and the UoW rolls back.)
- **Framework-pure.** `cqrs/` imports only the stdlib (`logging`, `collections.deque`, `typing`) and the local `cqrs.messages` base — no SQLAlchemy, typer, wireup, or pydantic-ai. The bus is wired by `container.py`'s composition root (handlers and subscribers injected as `Mapping`s); the kernel itself knows nothing about how messages are produced or persisted. That purity is what makes the in-process drain swappable for an outbox/queue later by editing only this one module.

The rationale is recorded plainly: **the ingestion pipeline will grow complex, and the pattern is new here**, so the pattern is introduced deliberately, kept minimal (~80 LOC across three files), and justified by the additive-events seam rather than by present need.

### (b) The note grammar is a shared kernel, `src/wikinote/`

The note grammar lives in **one** package, `engram/src/wikinote/`, consumed by **two** sides — *one schema, two consumers*:

- **Compile-time parsing.** The compiler's parse stage runs `wikinote.parser` over every `Wiki/**/*.md` file, turning frontmatter + H2 sections + claim blocks into a frozen `WikiNote` (typed per-page-type `*Frontmatter`, `Section`, `ClaimBlock`). The same package's `wikinote.registry` compiles `_meta/registry/*.md` into a `RegistryIndex`, and `wikinote.verify` does the whitespace/Unicode-normalized quote-in-mirror check.
- **Authoring-time construction and validation.** The new authoring CLI consumes the *same* models from the *other* direction. An ingestion extractor emits an `AuthoringPayload` (the JSON contract in `wikinote.payload`); `engram note build` renders it through `wikinote.renderer` into a **canonical, validated** `.md` file — frontmatter validated through the per-type model, claim anchors and Findings bullets generated, registry vocabulary checked, quotes verified against the source mirror. This is the mechanism behind the user's requirement to produce **"validated files, not trusted prose"**: extractors stop hand-writing Markdown and instead emit a payload that the kernel renders and validates.

A single kernel is the only way both sides stay in lockstep: the parser and the renderer are inverses over the same domain models, and the round-trip invariant (`parse(render(payload))` is semantically equal to the payload's `WikiNote`, with the live `lightrag-2025.md` golden fixture as the regression net) is only meaningful because there is one grammar, not two.

This follows the **shared-kernel idea**: a small module that several consumers import from deliberately, rather than copy-pasting. The wiki's kernel is larger because the grammar is larger, but the principle is identical — *one explicit definition, many consumers, no drift.* Consistent with [[0001-wiki-app-placement-and-core]]'s ownership rule, the kernel is **app-domain code and stays in the app** (`engram/src/wikinote/`), not in `core`; it is wiki-specific, not general-purpose. It depends only on the stdlib, pydantic, Markdown libraries, and `core.markdown` (the frontmatter ⇄ dict split helper) — no SQLAlchemy or wireup — so the authoring CLI stays light and the compiler reuses it without a circular dependency.

The **authoring-contract change this enables is recorded vault-side** as **D-22** in `_meta/design/decisions.md` (landed by task 2.22): the vault's ingestion extractors switch from writing raw `.md` to emitting `AuthoringPayload` JSON and running `engram note build`. That ADR governs the vault's authoring workflow; *this* one governs the code that makes it possible.

## Consequences

**Positive**

- The write side has a **single, well-defined extension seam.** When event-driven ingestion arrives, handlers and commands do not change — only the bus's post-handler drain is replaced by an outbox write + queue consumer. The deferred-events decision is honoured without painting the v1 code into a corner.
- **Subscribers always observe committed state**, by construction: the post-handler drain runs after the handler's `UnitOfWork.commit()` has returned. No subscriber can read a half-built transaction, so the eventual event-driven features start from a correct ordering invariant rather than retrofitting one.
- **The bus is framework-pure and tiny** (~80 LOC), unit-tested in isolation (`unit/cqrs/test_bus.py` covers command dispatch, post-handler drain ordering, and subscriber isolation). Its cost is proportionate to the deliberate-pattern decision.
- **The note grammar cannot drift between compile and authoring** — they share one kernel, and the `parse(render(·))` round-trip plus the `lightrag-2025.md` golden fixture pin them together. Programmatic authoring delivers validated files because the validator the compiler trusts is the same one `engram note build` runs.
- **The authoring CLI stays light.** Because the kernel pulls in no database or DI machinery, `engram note build|validate|verify|schema` runs without a Postgres connection or a full container — appropriate for a tool an ingestion extractor invokes per file.

**Negative / costs**

- **CQRS is a new pattern in this monorepo.** A reader used to a plain request/response service will not recognise the command/handler/bus/UoW shape; it raises the onboarding bar for the wiki app and must be learned, not pattern-matched. This is the accepted price of choosing a structure for the pipeline's *future* complexity rather than its present size — and the reason this ADR documents the as-built conventions in detail.
- **Subscriber-failure isolation hides observation errors.** A subscriber that raises is logged and skipped, not surfaced to the caller. That is correct for post-commit reactions (the command already succeeded), but a silently-failing future subscriber (e.g. one that should write an outbox row) will need its own monitoring — the bus deliberately will not fail the command for it.
- **The shared kernel is a coupling point.** Both the compile slice and the authoring CLI depend on `wikinote`; a grammar change ripples to both, and any change there must keep the round-trip invariant and the golden-fixture regression green. This is the intended trade — one place to change, but one place that *must* be changed carefully.
- **The kernel encodes the vault's `_meta/schema.md` in code**, so the two can diverge: a schema change in the vault that the kernel does not track will surface as parse/lint failures against real notes. The `lightrag-2025.md` golden fixture is the early-warning net, but keeping kernel and schema in sync is an ongoing obligation, not a one-time port.

**Follow-ups**

- **Event-driven ingestion** (outbox + queue/worker) is the designated next step the bus is built to absorb; it remains out of scope here ("no outbox, queues, schedulers, or workers; the bus is the seam").
- The `wikinote` kernel itself is **built by slice C** (`domain.py`, `parser.py`, `renderer.py`, `payload.py`, `registry.py`, `verify.py` + the authoring CLI subcommands); this ADR describes it in target-state terms ahead of that work.
- The **vault-side authoring-contract change is recorded as D-22** in `_meta/design/decisions.md` (task 2.22), and the extractor brief (`.claude/skills/ingesting-papers/extractor-brief.md`) is rewritten to the payload-JSON path in slice F — both downstream of this decision.
