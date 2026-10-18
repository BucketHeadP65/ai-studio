// The Workflow studio's API base URL — a DIFFERENT backend than the knowledge base API:
// the `studio.agent` FastAPI app, which serves the workflow library, the
// authoring agent, and the run machinery on 127.0.0.1:8770 by default.
//
// Read from `NEXT_PUBLIC_STUDIO_API_URL` (set in `.env.local`), falling back to
// the fixed local port for the same reason KNOWLEDGE_BASE_API_URL does: the dashboard is
// local-only, and a missing var should not fail the build.
export const STUDIO_API_URL = process.env.NEXT_PUBLIC_STUDIO_API_URL ?? "http://127.0.0.1:8770";
