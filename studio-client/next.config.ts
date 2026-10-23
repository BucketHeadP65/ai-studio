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
  // Without this, `next dev` started by a coding agent writes a Next.js block into AGENTS.md, which is a
  // symlink to the committed CLAUDE.md, so every dev server start would dirty the checkout.
  agentRules: false,
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
