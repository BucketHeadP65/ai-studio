---
id: studio-client-adr-0002-chat-first-parallel-sessions
aliases: ["studio-client-adr-0002-chat-first-parallel-sessions"]
type: adr
app: studio-client
status: accepted
tags: []
title: "0002 — Chat first, sessions parallel: the station targets a contract the backend does not have yet"
---

# 0002 — Chat first, sessions parallel: the station targets a contract the backend does not have yet

**Status:** Accepted (2026-08-23).

## Context

S2 recomposes the SPA around conversation: `/studio` becomes `/chat` and the front door, past sessions sit on the left grouped by
project, the workflow canvas moves into a full-page inspector at `/workflows/{id}`. The owner's model for the new station is
parallel sessions: each session is its own agent with its own context and world, many answering at once, and clicking a session
only changes what you view.

The sessions backend being built in parallel (S1) contradicts that model at
its core: one `Runtime`, one live slot, `POST /api/sessions/{id}/continue` re-points it, 409 while a turn is in flight, and the
journal derives which turns belong to which session from boundary events in a single timeline — a derivation that interleaved
parallel turns would break.

## Options

1. **Ship on S1 as-is.** One session answering at a time; sending in another session switches the agent there. Rejected: it bakes
   send-is-switch into the new station's core interactions, and the parallel model the owner wants would force a second
   conversation-flow rework shortly after the first.
2. **Pause S2 and amend S1 first.** Cleanest end state, but blocks both the planning and the in-flight backend build on a redesign
   conversation. Rejected.
3. **Design the UI for the parallel model and name the gap as a prerequisite.** Chosen.

## Decision

- The chat station's interaction model is parallel-native: click views, typing engages the viewed session, Stop interrupts an
  answering one, every workflow run lives in exactly one session (a manual run from the inspector gets a fresh session named after
  the workflow).
- The backend contract the model needs is written down as a spec for the S1 builder rather than authored by S2: session-scoped
  chat-stream with implicit hydration (no continue endpoint in the end state), per-session interrupt/decide/run/pause/resume, an
  aggregate `/api/runs`, session creation, and session identity carried explicitly on turn events.
- Delivery is phased. Slice A ships the full layout against today's single-slot backend behind one shim: send wraps `/continue`,
  one session answers at a time, Stop stays hidden. Slice B deletes the shim when the backend lands.
- The chat engine adopts the Vercel AI SDK now, per [ADR 0001](0001-vercel-ai-protocol-and-sdk.md): composer takeover for gates is
  the B2 moment that ADR named, and the parallel rework is the conversation-flow rework it budgeted.

## Consequences

- One conversation-flow rework instead of two; the SPA's most-used surface is built once, against the model it will keep.
- The S1 builder gets a precise negotiation surface (the spec) instead of drive-by scope creep on a mid-flight plan.
- Slice A knowingly under-delivers the model it renders: the UI promises parallel sessions while only one can answer. The shim and
  its notice ("the agent is answering in another session") are temporary by design and must be deleted, not maintained.
- If the backend gap lands materially different from the spec (the builder owns the journal grammar), the client's session API
  layer is the single place that absorbs the drift — components consume its zod-parsed types only.
