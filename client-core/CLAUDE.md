# client-core: the shared frontend library

`client-core/` is the frontend analogue of `core/`: app-agnostic building blocks (typed `fetch` + zod boundary, UI primitives, utilities, the Vitest/MSW test-support harness) consumed by `studio-client/`. It ships **raw TypeScript with no build step**. Consumers transpile it like first-party source via Next's `transpilePackages: ["client-core"]` (ADR-0004). `react`, `react-dom`, `next`, `zod`, and `@tanstack/react-query` are **peerDependencies** (the consumer owns the single copy of each).

Its surface today: `api/client` (the typed fetch + zod boundary), the `ui/*` primitives (`ui/modal` is the one centered dialog, `ui/drawer` the side panel, both on the radix Dialog. `ui/confirm-dialog` builds on `ui/modal`. Never hand-roll an overlay), `lib/cn`, `lib/format-relative`, and `test-support/{setup,render}`.

**Consumed as a packed COPY, never a symlink.** Each consumer declares `"client-core": "file:../client-core"` and pins `install-links=true` in its `.npmrc`, so npm installs a *copy* inside the app's own `node_modules` rather than a symlink. The copy keeps the package's real path **inside** the consuming app, which preserves Turbopack's default project root and avoids a second `node_modules` shadowing react/react-query during SSR.

> **NEVER set `turbopack.root`** in any consumer (`studio-client/`). A symlinked `client-core` once forced `turbopack.root` up to the monorepo, pulling every sibling `.venv`, `.git`, and `node_modules` into the dev watcher plus persistent-cache task graph. `next-server` leaked to ~60 GB and OOM'd repeatedly on 2026-06-11. The copy-install is the fix, widening the root re-opens the wound.

**Refresh workflow (binding, npm packs lazily).** After editing `client-core/`, run **`npm install ../client-core` in each consumer** (from `studio-client/`). A plain `npm install` / `npm update` will **not** repack an unchanged spec, *even after a version bump*, so the consumers keep the stale copy. When the change matters, **bump `client-core`'s patch version** (`package.json`) so the intent is recorded, then re-install the copy in both consumers.

**TypeScript config.** Consumers extend the shared base **through the copy-installed package**: `"extends": "./node_modules/client-core/tsconfig.base.json"` (an **in-root path**, because Turbopack's build refuses an `extends` that resolves outside the project root, and widening `turbopack.root` is forbidden, see above). `tsconfig.base.json` ships in the package's `files`. After changing the base, refresh with `npm install ../client-core` in each consumer.

**Tailwind (v4) must scan client-core.** Tailwind v4 auto-detects sources from the project tree but **deliberately skips `node_modules`**, and `client-core` is copy-installed *into* `node_modules/client-core`. Without an explicit `@source`, every utility class used only by a `client-core` primitive (e.g. the `Button`'s `bg-slate-900 text-white`) gets **purged** and the component renders unstyled (looks like plain text). Each consumer's `globals.css` must register it: `@source "../../node_modules/client-core/src/**/*.{ts,tsx}";` (path relative to the CSS file). Applies to `studio-client/`.

**Ownership rule: a change in `client-core/` must keep its consumers green.** Run typecheck plus the full suites in `studio-client/` before claiming it done.
