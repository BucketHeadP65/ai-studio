# client-core

`client-core` is the shared frontend library of this repository, the frontend counterpart of `core/`. It holds the
app-neutral pieces that [`studio-client`](../studio-client/README.md), AI Factory Studio, is built from: the typed fetch
client with its zod boundary, UI primitives, two helpers and the Vitest and MSW test setup. The decision behind it is
[root ADR 0004](../docs/adr/0004-client-core-shared-frontend-package.md).

## How an app uses it

The package ships raw TypeScript with no build step. An app:

1. depends on it with `"client-core": "file:../client-core"` and sets `install-links=true` in its `.npmrc`, so npm
   installs a copy inside the app's own `node_modules` rather than a symlink.
2. transpiles it with `transpilePackages: ["client-core"]` in `next.config.ts`.
3. extends `./node_modules/client-core/tsconfig.base.json` in its `tsconfig.json`.
4. registers it with Tailwind v4 in its `globals.css`: `@source "../../node_modules/client-core/src/**/*.{ts,tsx}";`.
   Tailwind skips `node_modules`, so without this line the primitives render unstyled.
5. lists `react`, `react-dom`, `next`, `zod` and `@tanstack/react-query` itself. They are peer dependencies here, so
   the app holds one copy of each.

npm does not repack an unchanged file dependency, so after editing `client-core`, run `npm install ../client-core` in
the app, and bump this package's patch version when the change matters.

## Imports

Each module is imported by its own path. There is no barrel file.

```ts
import { createApiClient, ApiError, VoidSchema } from "client-core/api/client";
import { cn } from "client-core/lib/cn";
import { formatRelative } from "client-core/lib/format-relative";
import { Button } from "client-core/ui/button";
import { ConfirmDialog } from "client-core/ui/confirm-dialog";
import { installTestLifecycle } from "client-core/test-support/setup";
import { renderWithProviders } from "client-core/test-support/render";
```

The UI primitives are `badge`, `button`, `card`, `confirm-dialog`, `drawer`, `empty-state`, `input`, `modal`,
`section-header`, `select`, `status-dot` and `textarea`. `ui/modal` is the one centered dialog and `ui/drawer` the one
side panel, both on the Radix dialog.

`createApiClient({ baseUrl, getToken?, onUnauthorized? })` returns a typed `request(path, schema, init)`. Each app
injects its own base URL, token and `401` handler, and builds its endpoint functions and zod schemas on top.
`installTestLifecycle(server)` takes the app's own MSW server, so the request handlers stay in the app.

## Commands

```bash
npm install
npm run typecheck   # tsc --noEmit
npm run lint        # biome check src
npm run check       # biome check --write src
npm test            # vitest run
```

A change here must keep `studio-client` green, so run its typecheck and tests as well.
