---
id: core-adr-0003-the-record-and-the-wiring-helpers-live-in-core
aliases: ["core-adr-0003-the-record-and-the-wiring-helpers-live-in-core"]
type: adr
app: core
status: accepted
tags: []
title: "0003: The conversation record and the wiring helpers live in core"
---

# 0003: The conversation record and the wiring helpers live in core

## Status

Accepted, 2026-09-06.

## Context

Two applications in this repository keep conversations, and a third kind of code assembles both of them. The
conversation history they keep was a file per run holding the whole message list again at every step, so a
conversation cost steps times history. A record that stores every message once, keyed by what nobody rewrites, with
one small row per step saying what the model was given, was built and tested against 988 real snapshots:
1066 MB of cumulative snapshots became 10.5 MB, and every snapshot reconstructed byte for byte. That record talks to
Postgres through three small protocols and to the agent through the vendored `StepStore` port, and it names nothing
of any application's domain.

Assembly has the same shape. Each application needs its settings root turned into one container registration per
nested model, so a consumer declares the slice it reads and never a whole settings tree, and each needs one late
resolution seam over its container for the callers that cannot take a dependency as an argument. The `Resolver` port
already lived in `core.resolver` with its only adapter in one application.

## Decision

`core.ai.history` is the record. It keeps pydantic-ai's messages as the record itself, once, with a form per shape a
message was ever given and a stamp version per set of fields pydantic-ai rewrites, and every step is an ordered list
of three-integer entries. `PostgresStepStore` implements the vendored `StepStore` over asyncpg and adds the four
things that port has no method for: the request parameters of each model request, every message of a conversation in
its first form, the history a next run continues from, and deleting a conversation, which is two statements and the
schema's own cascades. Every write reconstructs the snapshot it was given and
refuses to commit when the two differ, so the store is exact by construction rather than by trust. The record ships
its own DDL and `create_record_schema` applies it: whoever owns the database owns when that runs.

The record talks to Postgres through three protocols of its own: `Row` (a result row by column name), `Connection`
(execute, fetch, fetchrow, transaction) and `Database` (a connection per block). An `asyncpg.Connection` satisfies
`Connection` as it is. A pool does not satisfy `Database` as it is, because asyncpg installs the connection's methods
on its pooled proxy at class creation, so `PoolDatabase` is the four-line adapter that closes that gap.

`core.wiring` is the assembly. `sections_of(settings)` walks a settings root and answers one registration per
model-annotated field, recursively, each under the field's own type. A scalar is not a section, a `list[Model]` and a
`dict[str, Model]` are not sections, an optional model and a union of two models refuse at build time by naming the
field. `ContainerResolver` is the adapter over a wireup container for the `Resolver` port that stays in
`core.resolver`, and `resolver_of(app)` reads it back off an application's state.

`wireup` and the vendored `pydantic-ai-harness` become dependencies of core, the second by the same path source
every other project in this repository uses.

## Consequences

- Both applications keep their conversations in one shape, and neither owns a message type of its own.
- The record's vocabulary (key, form, stamps, entry, snapshot, digest) exists in exactly one place, and an
  application sees conversations, runs, messages and steps.
- Every consumer of core now resolves the vendored harness. A consumer that never imports `core.ai.history` still
  installs it, which is the price of the record being core's.
- The record carries no idempotency key on its events or its steps, because the vendored fork's types carry none.
  When the fork gains them, the columns and the two conflict clauses come with them.

## Alternatives considered

Keeping the record inside one application and importing it from there was rejected: two applications here need
it, and neither may depend on the other. Making it an optional extra of core was rejected for the same reason the
workflow engine is not one for its own consumers: the record is small, and an extra that every consumer must remember
to ask for is a trap. Keeping the wiring helpers in the application that had them was rejected because both
applications derive sections from a settings root and both resolve late from a container.
