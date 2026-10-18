import type { NextConfig } from "next";

// Security headers + CSP for the AI Factory Studio dashboard.
//
// `headers()` ships a minimal-but-load-bearing CSP + the usual transport /
// framing / referrer hardening. The CSP is intentionally strict; if a future
// feature needs a new directive, add the smallest host allowlist that makes it
// work — never relax to `*`. `connect-src` allowlists exactly the local knowledge base
// API origin (Decision U); the dashboard talks to nothing else.
//
// `'unsafe-eval'` is added to `script-src` ONLY in development because React's
// dev build calls `eval()` to reconstruct error callstacks (HMR + the redbox
// overlay). Production React never evaluates strings, so the directive must stay
// off in prod — a CSP that ships `'unsafe-eval'` to users effectively disables
// one of the few mitigations against XSS-as-arbitrary-JS.
//
// No `images.remotePatterns`: the dashboard renders no remote images today. Add
// an entry (never `**`) when a real source lands.
const isDev = process.env.NODE_ENV !== "production";
const scriptSrc = isDev
  ? "script-src 'self' 'unsafe-inline' 'unsafe-eval'"
  : "script-src 'self' 'unsafe-inline'";

const config: NextConfig = {
  // The dev server registers as `localhost`; a browser on `http://127.0.0.1:3031`
  // is cross-origin to Next's dev resources (HMR socket, client chunks) and gets
  // blocked by default — the page then renders server HTML but never hydrates
  // (no handlers, dead inputs). Both spellings of loopback are this machine.
  allowedDevOrigins: ["127.0.0.1"],
  // client-core ships raw TypeScript (no build step, ADR-0004); Next must
  // transpile it like first-party source.
  //
  // The package is a file: dep installed as a COPY, not a symlink
  // (`install-links=true` in this app's .npmrc). Copying keeps its real path
  // INSIDE this app, which preserves Turbopack's default project root: the
  // symlink-era workaround (`turbopack.root` widened to the monorepo) put
  // the Python projects' .venvs, .git and every node_modules into the dev watcher +
  // persistent-cache task graph and leaked next-server unboundedly. The copy
  // also carries no nested node_modules, so react / react-dom / next /
  // react-query resolve to THIS app's single copies — required during SSR,
  // where a second react-query instance hides the app's QueryClientProvider
  // from client-core's hooks. NEVER set `turbopack.root` here. Consequence:
  // after editing client-core/, refresh the copy with `npm install ../client-core`
  // here — a plain `npm install`/`npm update` will NOT repack an unchanged spec,
  // even if the package's version was bumped.
  transpilePackages: ["client-core"],
  async headers() {
    return [
      {
        source: "/:path*",
        headers: [
          {
            key: "Content-Security-Policy",
            value: [
              "default-src 'self'",
              "img-src 'self' data:",
              scriptSrc,
              "style-src 'self' 'unsafe-inline'",
              // Two local backends: the knowledge base API (8100) and the Workflow
              // studio's agent backend, `studio.agent` (8770).
              "connect-src 'self' http://127.0.0.1:8100 http://127.0.0.1:8770",
              "frame-ancestors 'none'",
              "base-uri 'self'",
              "form-action 'self'",
            ].join("; "),
          },
          {
            key: "Strict-Transport-Security",
            value: "max-age=63072000; includeSubDomains; preload",
          },
          { key: "X-Frame-Options", value: "DENY" },
          { key: "X-Content-Type-Options", value: "nosniff" },
          { key: "Referrer-Policy", value: "strict-origin-when-cross-origin" },
          { key: "Permissions-Policy", value: "camera=(), microphone=(), geolocation=()" },
        ],
      },
    ];
  },
};

export default config;
