<!-- GENERATED from architecture.md by scripts/docs/split_doc.py --dir docs/architecture — do not edit here; edit the master and re-run the script. -->

## 5. Frontends
> **TL;DR:** One Next.js 16/React 19 SPA, studio-client (:3031), local and no-auth, branded "AI Factory Studio", with its app-agnostic code in `src/shared/`: a typed zod fetch factory, UI primitives, and a Vitest/MSW test harness.
<!-- load-when: frontend, studio-client, src/shared, AI Factory Studio, the Studio, dashboard, react, next.js, ui, SPA -->

- **studio-client/**, user-visibly **AI Factory Studio**:
  `/search`, `/chat`, `/graph` (sigma.js + graphology), `/studio` (the workflow
  studio, React Flow over a dagre layout, talking to the agent half on :8770),
  `/ops` (job triggers), `/evals`, `/settings`. Types generated from the running
knowledge base API's `/openapi.json` (`npm run gen:types`). Every response zod-parsed
in `src/api`. The brand lives in copy only, routes, ports, and
  `NEXT_PUBLIC_*` names are unchanged
  ([ADR 0009](../../adr/0009-ai-factory-studio-consolidation.md), amended by [ADR 0010](../../adr/0010-harness-extraction.md)).
- **studio-client/src/shared/**, the app-agnostic code, which imports nothing
  from the rest of the app (`src/shared/boundary.test.ts` fails on a crossing).
  Seams: `api/client` (`createApiClient({baseUrl, getToken?, onUnauthorized?})`),
  the `ui/*` primitives, `lib/cn`, `lib/format-relative`, and the test support
  (`test-support/setup`, `test-support/render`, `test-support/msw/server`). Deep
  imports per module, no barrel
  ([ADR 0021](../../adr/0021-frontend-shared-code-lives-in-the-spa.md)).
