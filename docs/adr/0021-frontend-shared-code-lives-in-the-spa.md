---
id: adr-0021-frontend-shared-code-lives-in-the-spa
type: adr
title: The shared frontend code lives inside studio-client, in src/shared/
summary: 'client-core merges into its only consumer: its code moves to studio-client/src/shared/, which imports nothing from the rest of the app, and the packed copy, its config and its separate test setup go away'
status: accepted
aliases:
- adr-0021-frontend-shared-code-lives-in-the-spa
app: root
created: '2026-10-23'
---

## Context

Root ADR 0004 made `client-core` a separate package for two Next.js clients. One client remains. `studio-client`
installed `client-core` as a packed copy from `file:../client-core`, because a symlink once forced `turbopack.root`
up to the monorepo and the dev server ran out of memory.

The copy cost something on every change. An edit reached the app only after `npm install ../client-core`, and npm
did not repack an unchanged spec even after a version bump. Tailwind needed an `@source` line into `node_modules`,
Next needed `transpilePackages`, Vitest needed an `inline` entry, and the app's tsconfig extended a base file through
`node_modules`. The two packages carried the same dependencies and overrides, two Dependabot groups and two MSW
test setups.

## Decision

D1. **`client-core` merges into `studio-client`.** Its `api`, `lib`, `ui` and `test-support` folders move to
`studio-client/src/shared/` and are imported through the app's `@/shared/` alias. The `client-core` folder, its
`file:` dependency, `.npmrc`'s `install-links`, `transpilePackages`, the Vitest `inline` entry, the tsconfig
`extends` and the Tailwind `@source` line are deleted.

D2. **`src/shared/` imports nothing from the rest of the app.** It imports packages and its own files only.
`src/shared/boundary.test.ts` fails on an `@/` import outside `@/shared/` or a relative path that climbs out of the
folder.

D3. **One test setup chain with one MSW server.** `src/test-support/setup.ts` imports
`src/shared/test-support/setup.ts`, which runs the lifecycle against the one server in
`src/shared/test-support/msw/server.ts`. The duplicate `render`, server and setup files are merged into it.

## Consequences

- An edit to shared code is live on save, like any other file in the app.
- Tailwind scans `src/shared/` with the rest of the app, so no `@source` line is needed.
- The shared code typechecks, lints and tests with the app's own versions and tools.
- A second frontend would have to extract `src/shared/` into a package again. The boundary test keeps that a move
  rather than a rewrite.
- Root ADR 0004 is superseded by this record.

## Options considered

- **Keep the package and run an npm workspace.** Refused. It adds root Node tooling to a polyglot repository for
  one consumer.
- **Keep the package as a symlinked `file:` dependency.** Refused. The symlink is what forced `turbopack.root`
  wide.
- **Merge without a boundary.** Refused. Shared code that imports app code can no longer be lifted out.
