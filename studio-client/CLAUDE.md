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
`harness/docs/reference/inspection.md`. A consumer of
`client-core`, talking to the Studio's agent backend (the `harness` app, :8770) for chat, the workflow inspector, the projects
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

**Frontend** (`studio-client/`, `client-core/`): `npm test` is `vitest run`. Pass filters through after `--`:

```bash
npm test -- search-form                  # only files matching "search-form"
npm test -- -t "renders empty state"     # only tests whose full name matches the pattern
npm run test:e2e -- e2e/search.spec.ts   # one Playwright spec (the two SPAs only)
```

## Frontend API boundary (cross-stack)

The SPA agrees a typed contract with each backend it talks to (`studio-client` to `studio serve-http`, and to the harness backend for the studio station). The discipline: every backend response is `.parse()`d against a `zod` schema before reaching components (the typed `createApiClient` from `client-core` plus an app-local base-URL injection in `src/api/`), and the OpenAPI-derived types are regenerated with `npm run gen:types` against the running server. Never `as`-cast at the boundary. Backend drift (renamed field, new required, type change) should surface as a `ZodError` at the boundary instead of as `undefined.something` deep in a component. Datetime fields use `z.string().datetime({offset: true})` so a malformed timestamp surfaces as a `ZodError` rather than `NaN` downstream.

## Vendored components

`src/components/ui/` and `src/components/ai-elements/` are vendored copied source (shadcn + Vercel AI Elements, see
studio-client ADR-0003), not the app's kit: app code takes its primitives (Button, …) from `client-core`, and only the chat's
`work-tool.tsx` seam consumes the vendored tree. Keep vendored files as shipped apart from repo formatting. Theme them via the
token aliases in `globals.css`.

## Consuming `client-core`

**Refresh workflow (binding, npm packs lazily).** After editing `client-core/`, run **`npm install ../client-core` in each consumer** (from `studio-client/`). A plain `npm install` / `npm update` will **not** repack an unchanged spec, *even after a version bump*, so the consumers keep the stale copy. When the change matters, **bump `client-core`'s patch version** (`package.json`) so the intent is recorded, then re-install the copy in both consumers.

**Tailwind (v4) must scan client-core.** Tailwind v4 auto-detects sources from the project tree but **deliberately skips `node_modules`**, and `client-core` is copy-installed *into* `node_modules/client-core`. Without an explicit `@source`, every utility class used only by a `client-core` primitive (e.g. the `Button`'s `bg-slate-900 text-white`) gets **purged** and the component renders unstyled (looks like plain text). Each consumer's `globals.css` must register it: `@source "../../node_modules/client-core/src/**/*.{ts,tsx}";` (path relative to the CSS file). Applies to `studio-client/`.
