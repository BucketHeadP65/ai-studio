---
id: adr-0004-client-core-package
aliases: ["adr-0004-client-core-package"]
type: adr
app: root
status: superseded
superseded_by: "[[adr-0021-frontend-shared-code-lives-in-the-spa]]"
tags: []
title: "0004 — `client-core` shared frontend package via `file:` path dependencies"
---

# 0004 — `client-core` shared frontend package via `file:` path dependencies

**Superseded by [ADR-0021](0021-frontend-shared-code-lives-in-the-spa.md) (2026-10-23).**

- **Status:** superseded by [ADR-0021](0021-frontend-shared-code-lives-in-the-spa.md)
- **Date:** 2026-06-11
- **Deciders:** repository maintainers (ui-skeleton feature)
- **Supersedes / Amends:** none
- **Related:** `docs/adr/0001` (the uv path-source precedent this mirrors), `docs/adr/0003` (the wiki consolidation the dashboard serves)

## Context

Two Next.js clients (`engram-client/`, the wiki dashboard, and a sibling client) need the same app-agnostic frontend code: the typed zod-validating fetch wrapper, the `ui/` primitives, `cn`/format utilities, the Vitest/MSW test-support harness, and the base tsconfig/biome configs. The idiomatic JS answer is npm workspaces — but ADR-0001 deliberately rejected the analogous uv workspace so each app keeps an independent lockfile and an unmoved virtualenv. The same forces apply on the Node side: a root `package.json` in a polyglot repo, changed hoisting under a consumer, and modified build commands are real costs for a two-consumer package.

## Decision

Create top-level `client-core/` (package `client-core`) shipping raw TypeScript source, consumed by both clients as a **`file:../client-core` dependency** — npm symlinks local directory deps, so edits reflect immediately — with `transpilePackages: ["client-core"]` in each Next config. React, `react-dom`, and `next` are **peerDependencies** of `client-core` (each app resolves its own single copy); each app keeps its own `package-lock.json`. Each consumer re-points its imports onto the package, gated on its full unit + e2e suite staying green. Ownership rule mirrors `core/`: a change in `client-core/` must keep **both** clients' suites green.

## Options considered

- **npm workspaces** — rejected: introduces root-level Node tooling to a polyglot monorepo, changes hoisting/resolution under a consumer, and requires build edits; violates the per-app-independence principle ADR-0001 established.
- **Publish to a registry (private npm/GitHub)** — rejected: versioning + publish ceremony for two same-repo consumers.
- **Copy the shared files into each client** — rejected: duplication is the problem this package exists to solve.

## Consequences

**Good:** one source of truth for shared frontend code; the extraction playbook and ownership rule are the proven `core` ones; each client's deployment is untouched.

**Bad:** `file:` deps have install-time gotchas — when `client-core`'s *own* `dependencies` change, consumers must re-run `npm install`; tooling that resolves through symlinks (Vitest, Next) needs the transpile/`preserveSymlinks` settings kept consistent.

**Mitigation:** `client-core` keeps its runtime dependency list minimal (utilities pin `clsx`/`tailwind-merge`; everything framework-shaped is a peer), and both clients' CI scripts run `npm install` before build, which re-syncs the link.
