// The knowledge base API's base URL — the `studio serve-http` process that serves the
// indexed knowledge base.
//
// Read from `NEXT_PUBLIC_KNOWLEDGE_BASE_API_URL` (set in `.env.local`; see `.env.example`),
// defaulting to the local serve process on 127.0.0.1:8100. The dashboard is
// local-only with a fixed default port, so a missing var falls back to that
// default rather than failing the build.
export const KNOWLEDGE_BASE_API_URL =
  process.env.NEXT_PUBLIC_KNOWLEDGE_BASE_API_URL ?? "http://127.0.0.1:8100";
