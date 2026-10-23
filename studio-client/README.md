# studio-client, AI Factory Studio

AI Factory Studio is the web app of this repository: a Next.js 16 single-page app in React 19 and TypeScript over two
backends, the agent and workflow runtime ([`harness`](../harness/README.md), port 8770) and the knowledge base API
([`studio`](../studio/README.md), port 8100).

![The workflow library in AI Factory Studio, a grid of workflow cards with trust levels, step counts, gates and run costs](../docs/images/studio-library.png)

## Run it

```bash
npm install
npm run dev
```

The app runs at <http://localhost:3031>. <http://127.0.0.1:3031> works too, because the knowledge base API accepts
browser requests from both origins. It expects the agent backend at `http://127.0.0.1:8770` and the knowledge base API at
`http://127.0.0.1:8100`. Set `NEXT_PUBLIC_STUDIO_API_URL` and `NEXT_PUBLIC_KNOWLEDGE_BASE_API_URL` in `.env.local` to
point elsewhere, as [`.env.example`](.env.example) shows.

## Pages

| Page | Path | Backend | What it shows |
| --- | --- | --- | --- |
| Home | `/` | harness | Start an automation from a saved workflow, what is running, what waits for your decision, and recent results. |
| Chat | `/chat` | harness | The conversation with the agent: sessions on the left, the work on the right, a turn's detail behind every reply. |
| Fleet | `/workflows` | harness | Runs waiting on you, running, paused, scheduled and recent, and the run archive. |
| Library | `/workflows/library` | harness | Every saved workflow with its trust level, steps, gates and cost. A workflow's own page shows its map and every step. |
| Run | `/workflows/{id}?run=...` | harness | One run: outcome, artifacts, spend, timeline, values, gate decisions, and controls to pause, stop or run again. |
| Schedules | `/workflows/schedules` | harness | The workflows that run on a schedule. |
| Projects | `/projects` | harness | A project's files, notes and skills, read and edited in place. |
| Knowledge bases | `/knowledge-bases` | studio | Every knowledge base: create one from a template, register, refresh and schedule it, its decisions, and its link graph. |
| Harness | `/harness` | harness | The model tiers, the plugins, the profile, and the custom items the agent wrote, to review and activate. |
| Inspection | `/inspection` | harness | The findings from inspected runs, each finding's evidence, and each run's overview. |
| Settings | `/settings` | none | Appearance, font and chat preferences for this browser. |

![The page of one completed run of the workflow Link news digests, with outcome, cost, evidence, values and timeline](../docs/images/studio-run.png)

![The link graph of a news knowledge base, one digest page selected and its neighbours listed beside the graph](../docs/images/studio-graph.png)

## Scripts

| Script | What it does |
| --- | --- |
| `npm run dev` | The Next dev server on port 3031. |
| `npm run build`, `npm start` | A production build, and serving it on port 3031. |
| `npm run typecheck` | `tsc --noEmit`. |
| `npm run lint`, `npm run check` | Biome lint, and Biome check with fixes written. |
| `npm test` | The Vitest suite. `npm test -- <file filter>` or `npm test -- -t "<name>"` runs part of it. |
| `npm run test:e2e` | The Playwright suite in [`e2e/`](e2e/), on port 3031 unless `PLAYWRIGHT_PORT` names another. |

## How it is built

- Next.js 16 App Router, React 19 and TypeScript in strict mode.
- Tailwind CSS v4, TanStack Query and Biome.
- [`src/shared/`](src/shared/) holds the typed fetch client, the UI primitives and the Vitest and MSW test setup. It
  imports nothing from the rest of the app.
- Every backend response is parsed with a zod schema before a component sees it, so a changed backend fails loudly at
  the boundary.
- The chat runs on the Vercel AI SDK. The workflow maps use `@xyflow/react` laid out by `elkjs`, and the knowledge base
  graphs use sigma and graphology.
- `src/components/ui/` and `src/components/ai-elements/` are vendored shadcn and AI Elements components.
- [`design-system/ai-factory-studio/MASTER.md`](design-system/ai-factory-studio/MASTER.md) holds the visual rules.

## Layout

```text
src/
  app/            the routes, one folder per page
  api/            the zod-checked clients for both backends
  components/     the components of each page, plus the vendored ui/ and ai-elements/
  lib/            the app's helpers
  shared/         the app-agnostic code, which imports nothing from the app: the fetch client, UI primitives,
                  utilities, the test lifecycle and the one MSW server (shared/test-support/msw/server.ts)
  test-support/   the Vitest setup file and the test factories
e2e/              the Playwright specs
```

## Docs

- [`CONTEXT.md`](CONTEXT.md), the glossary of the web app.
- [`docs/reference/harness-station.md`](docs/reference/harness-station.md), the Harness page.
- [`docs/adr/`](docs/adr/), this app's decisions.
