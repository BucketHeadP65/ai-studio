---
id: engram-adr-0040-engram-testing-toolkit
aliases: ["engram-adr-0040-engram-testing-toolkit"]
type: adr
app: engram
status: accepted
tags: []
title: "0040 — The library ships its test toolkit as `engram.testing`"
---

# 0040 — The library ships its test toolkit as `engram.testing`

**Status:** Accepted (2026-08-10)

**Context.** After the AI Factory Studio consolidation (root ADR-0009), engram's test-support code — the hand-written
`Fake*` classes implementing the library's Protocols, the polyfactory factories, the corpus builders, and the
containerized-DB fixtures — existed twice: once under `engram/tests/support/`, once copied into `studio/tests/support/`.
The copies had already diverged at birth (import-sort, one docstring, one path anchor), and nothing detected drift. Every
future consumer of the library would have made a third copy.

**Decision.** The toolkit is library surface: `engram.testing`, shipped in the wheel, behind a new `engram[testing]` extra
carrying the deps only its submodules import (polyfactory, pytest, testcontainers[postgres]). Plain `engram` stays
test-tooling-free — the subpackage's `__init__` imports nothing, so the cost lands only on the submodule a test actually
imports. The golden fixtures (mini registry pages, the LightRAG benchmark note, its mirror excerpt) become package data
anchored by exported constants (`GOLDEN`, `GOLDEN_MINI_REGISTRIES`, …), killing the cross-checkout path hops both suites
used to reach them. The containerized-DB machinery is a pytest plugin module (`engram.testing.corpus_db`) that consumers
register via `pytest_plugins`; it locates the alembic chain through the installed package's checkout (ADR-0039's
checkout-relative rule) and asserts the anchor at import.

Boundaries, both enforced by the package-boundary gate: the runtime library never imports `engram.testing`, and
`engram.testing` never imports the studio.

**Consequences.**
- (+) One definition of every fake; drift between consumers is structurally impossible rather than merely detected.
- (+) A fake's docstring is now a public contract beside the Protocol it implements.
- (−) `src/engram/testing/**` carries a per-file-ignores block relaxing the test-shape lint rules (fake-constructor arity,
  seeded randomness, import-time anchor asserts) — the price of test-support shapes living under `src`.
- (−) The extra's pytest dependency means a version floor on consumers' test runners is now partly the library's business.
