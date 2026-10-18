---
id: studio-adr-0001-explore-two-brain-driver-port
aliases: ["studio-adr-0001-explore-two-brain-driver-port"]
type: adr
app: studio
status: superseded
tags: []
title: "0001 — The Explore station's two-brain driver port"
---

# 0001 — The Explore station's two-brain driver port

**Status:** Superseded (2026-08-21) — the Explore station was removed from the product; no successor decision replaces it.

**Context.** The Explore station embeds an agent the person can invoke while navigating the knowledge base, and the agent must be *self-aware* —
it should know this project, not answer as a random assistant. The truest self-aware brain available is the interactive Claude Code
session driving the repo (the `core.ai.bridge` transport already turns one into a pydantic-ai `Model`), but a page whose only brain goes
silent when a terminal closes cannot stand alone. Meanwhile the product under construction *is* a coding agent — and how closely it
behaves like the bridge brain on identical questions is the working measure of the "better harness" goal.

**Decision.** Agent messages go through a driver port chosen **per message**: `bridge` (the attached Claude Code session, via the file
queue) or `studio-agent` (the coding agent in its **real composition** — retrieval doors, store verbs, sandbox, shell — plus the session's
journal, workbench state, and knobs injected as instructions; deliberately not a scoped-down variant). A bridge message with no session
attached journals as `pending` and waits until the person cancels it — there is **no timeout and no auto-fallback**, because the driver
tag must always mean "who you asked": an answer labelled `bridge` is genuinely the attached session, or it does not exist.

**Consequences.**
- (+) An honest A/B harness: the same prompt, flipped between brains mid-session, with the journal recording who answered what.
- (+) The page is always usable — the studio-agent driver needs no terminal attached.
- (−) `pending` is a first-class entry state carried by the journal, the feed UI, and the telemetry, with an explicit cancel path.
- (−) The real agent can act (shell, pack) from an exploration chat — the same exposure the Studio tab's chat already accepts; scoping it
  down was rejected as quietly falsifying the comparison.

**References.** studio ADR-0002 (the journal those drivers write into).
