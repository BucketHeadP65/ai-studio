# studio-client: AI Factory Studio SPA

The AI Factory Studio SPA (Next.js): five stations. Home (the root, `/`, and the product's landing screen: Start automation
with the primary Create workflow action over the guided route at `/workflows/draft` and the saved workflows, each opening its
setup dialog, then Active automations, Needs your decision, and Recent results, over the bounded live, library and recent-run
reads), chat (the conversation center, sessions on the left, work on the right), projects (a project's files, skills, and notes, read and edited in place),
knowledge bases (every knowledge base the Studio knows, creation from the Studio, a terminal one click away inside any of
them, and each knowledge base's own link graph on its detail page's Graph tab, `studio/docs/reference/knowledge-base-management.md`),
and harness (the owner's control room: plugins, profile selection, and the review-and-activate surface over every custom
capability/step/agent/profile, `harness/docs/reference/self-authoring.md` section 6). The workflows station lives at
`/workflows`: the fleet board (waiting on you, running, paused, scheduled, recent lanes over the live pool, the schedules
table, and the run archive), each run's inspector at `/workflows/{id}` and `/workflows/draft` with its history rail, run chat
drawer, control strip (pause, stop, automode), gate decisions with feedback, and per-node outputs
(`harness/docs/reference/workflow-monitoring.md`), and each workflow's own page at `/workflows/library/{id}` (what it does: the
description and applicability, the parameters, every step in execution order with its briefing or program, tools, inputs, and
output schema as a field table, and what a run hands back). The inspection station lives at `/inspection`: the
findings list (a count line, four selects, one row per finding with its blame surfaces as chips), each finding's page at
`/inspection/{id}` (the judgment table, the evidence as a story with a detail pane, a drawer that draws one step of the
run as the chat does, and the inspector's trace folded into one bar of calls with the composer at its end), and each
run's overview at `/inspection/runs/{run}` (the timeline, its leads, findings, questions and jobs), see
`harness/docs/reference/inspection.md`. It talks to the Studio's agent backend (the `harness` app, :8770) for chat, the workflow inspector, the projects
station, the knowledge-bases station's terminal, and the harness station, and `studio serve-http` (the knowledge base API,
:8100) for the knowledge-bases station's registry list and each knowledge base's graph.

## Common Commands

### `studio-client/` *(run from `studio-client/`, dev port 3031)*

```bash
npm run dev          # dev server (:3031)
npm run gen:types    # regenerate the typed API client from the RUNNING HTTP server's
                     # OpenAPI schema — start studio serve-http first
                     # (studio-client → 127.0.0.1:8100)
npm test             # vitest unit run
npm run typecheck    # tsc --noEmit
npm run lint         # biome check
npm run test:e2e     # playwright
```

### Running a single test

**Frontend** (`studio-client/`): `npm test` is `vitest run`. Pass filters through after `--`:

```bash
npm test -- search-form                  # only files matching "search-form"
npm test -- -t "renders empty state"     # only tests whose full name matches the pattern
npm run test:e2e -- e2e/search.spec.ts   # one Playwright spec
```

## Frontend API boundary (cross-stack)

The SPA agrees a typed contract with each backend it talks to (`studio-client` to `studio serve-http`, and to the harness backend for the studio station). The discipline: every backend response is `.parse()`d against a `zod` schema before reaching components (the typed `createApiClient` from `src/shared/api/client` plus an app-local base-URL injection in `src/api/`), and the OpenAPI-derived types are regenerated with `npm run gen:types` against the running server. Never `as`-cast at the boundary. Backend drift (renamed field, new required, type change) should surface as a `ZodError` at the boundary instead of as `undefined.something` deep in a component. Datetime fields use `z.string().datetime({offset: true})` so a malformed timestamp surfaces as a `ZodError` rather than `NaN` downstream.

## Vendored components

`src/components/ui/` and `src/components/ai-elements/` are vendored copied source (shadcn + Vercel AI Elements, see
studio-client ADR-0003), not the app's kit: app code takes its primitives (Button, …) from `src/shared/ui/`, and only the chat's
`work-tool.tsx` seam consumes the vendored tree. Keep vendored files as shipped apart from repo formatting. Theme them via the
token aliases in `globals.css`.

## Shared code (`src/shared/`)

`src/shared/` holds the app-agnostic building blocks: `api/client` (the typed fetch and zod boundary), the `ui/*`
primitives, `lib/cn` and `lib/format-relative`, and `test-support/` (the test lifecycle, the one MSW server and
`render`). `ui/modal` is the one centered dialog and `ui/drawer` the side panel, both on the radix Dialog.
`ui/confirm-dialog` builds on `ui/modal`. Never hand-roll an overlay.

**Code under `src/shared/` imports nothing from the rest of the app**, only packages and other `src/shared/` files.
`src/shared/boundary.test.ts` fails on an `@/` import outside `@/shared/` or a relative path that climbs out of the
folder.

**One test setup chain.** `vitest.config.ts` runs `src/test-support/setup.ts`, which imports
`src/shared/test-support/setup.ts` (jest-dom, the scrollIntoView stub, the MSW server's listen, reset and close with
`onUnhandledFrame: "error"`, and a handler exception failing its test), then adds the jsdom shims React Flow needs.
Tests take `server` from `@/shared/test-support/msw/server` and `render` from `@/shared/test-support/render`.

## Node, TypeScript and the graph renderer

**Node 22.22.2 or later.** jsdom 30.1 declares `^22.22.2 || ^24.15.0 || >=26.0.0` in its `engines` field, and its
undici 8.12 needs 22.19 or later.

**TypeScript 7.** `npm run typecheck` and the type check in `next build` both run the TypeScript 7 `tsc` (Next.js
calls the command line, its `experimental.useTypeScriptCli` default). The TypeScript 7 package has no programmatic
API, so no code here imports `typescript`. Vitest, Biome and Playwright do not need it. `src/shared/boundary.test.ts`
reads imports with the oxc parser that `vite` exports as `parseSync`. In VS Code, the "TypeScript 7" extension
(`TypeScriptTeam.native-preview`, a workspace recommendation) runs the TypeScript 7 language server.

**`npm run gen:types` brings its own TypeScript 5.9.** `openapi-typescript` 7.13, the latest release, builds its output
through the TypeScript 5 programmatic API and declares the peer `typescript ^5.x`. npm installs a peer next to the
package that asks for it, so a project on TypeScript 7 cannot give it a copy of its own. The script runs it through
`npm exec` with `openapi-typescript@7.13.0` and `typescript@5.9.3` pinned, and it is not a devDependency. Make it one
again once a release supports TypeScript 7 (openapi-typescript issue 2841).

**The graph draws its own edge labels.** sigma 4 lays an edge label from source to target, so the label of an edge
drawn right to left comes out upside down (sigma.js issue 1544, open on 4.0.3). `renderEdgeLabels` stays off and
`src/components/graph/edge-labels.ts` draws the labels upright on a 2D canvas layer under the WebGL stage. Turn sigma's
labels back on and delete the layer once a sigma release keeps them upright. `e2e/graph.spec.ts` checks that nodes,
edges and edge labels are drawn.
