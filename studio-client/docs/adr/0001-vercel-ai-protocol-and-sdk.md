---
id: studio-client-adr-0001-vercel-ai-protocol-and-sdk
aliases: ["studio-client-adr-0001-vercel-ai-protocol-and-sdk"]
type: adr
app: studio-client
status: accepted
tags: []
title: "0001 — The chat wire speaks Vercel AI now; the SDK arrives with the approval slice"
---

# 0001 — The chat wire speaks Vercel AI now; the SDK arrives with the approval slice

**Status:** Accepted (2026-08-22). The server half of the decision is recorded in
[harness ADR 0002](../../../harness/docs/adr/0002-upstream-first.md); this ADR is the client's half.

## Context

The chat stream migrated off hand-rolled SSE frames onto the Vercel AI **data-stream protocol** — the de-facto standard chat
wire in the JS ecosystem (the `ai` package is the dominant AI toolkit there; its protocol is what other frameworks, pydantic-ai
included, ship server adapters for). "Vercel AI" names a wire format only: the server encoder is `pydantic_ai.ui.vercel_ai`
running locally, nothing calls any Vercel service, and the model path (the Pydantic AI Gateway in `creds.json`) is untouched.
The open question was whether the client should also adopt the SDK itself (`ai` + `@ai-sdk/react`, `useChat`) or keep its own
parser.

## Decision

- **Emit the protocol now, parse it by hand.** The client half stays the ~100-line zod-parsed parser in
  `src/api/chat-stream.ts` (tested), consumed by `useConversation`. No new dependency.
- **Adopt the npm SDK at the approval slice (B2, gate approval as composer takeover), not before.** That is the moment the SDK
  does real work: pydantic-ai's adapter at `sdk_version=6` streams tool-approval chunks for human-in-the-loop flows, which is
  exactly the composer-takeover UX, and `useChat` then earns its place managing approval state and message parts.

## Why deferred

1. **The conversation model is server-authoritative on purpose** — the server holds and resumes history; the client sends one
   `{message}`. `useChat` manages messages client-side and posts the whole array, so adoption means reworking the conversation
   flow and the Console, chat rail, and drawer with it.
2. **It is a fat dependency for what it would replace today** — one stream parse.
3. **The wire just stabilized with tests**; touching it again buys nothing owner-visible until B2.

## Consequences

- Any AI-SDK-compatible frontend can already consume the agent — the wire is the standard one.
- The B2 slice budgets the SDK adoption and the conversation-flow rework as one change, targeting `sdk_version=6`.
- Until then, protocol drift is caught by the parser's wire tests and the harness's stream pins (settle parity, tool chunks,
  error chunk).
