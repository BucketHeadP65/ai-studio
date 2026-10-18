---
id: adr-0008-progressive-disclosure-protocol
aliases: ["adr-0008-progressive-disclosure-protocol"]
type: adr
app: root
status: accepted
absorbs: ["[[spec-2026-08-08-progressive-disclosure-memory-design]]"]
tags: []
title: "ADR-0008 — Progressive-disclosure protocol: envelope contract, descent convention, enforcement tiers"
---

# ADR-0008 — Progressive-disclosure protocol: envelope contract, descent convention, enforcement tiers

**Status:** Accepted (2026-08-09). Executed as the first slice of
the progressive-disclosure design.

**Context.** Agents over compiled corpora either read whole files (context blowout) or search blind (no orientation). We wanted
context machinery — not a memory feature — that lets any provider serve information level by level, with the consumer deciding how
deep to go. Three candidate shapes were on the table: a first-party retrieval toolset (rejected: providers already have tools), a
provider-side summarization service (rejected: couples the contract to one corpus), and a result-format contract (chosen).

**Decision.** The spec defines only a **result-envelope format and a descent convention**; providers keep their own tools and conform
by what those tools return.

- **Contract** (`core.ai.disclosure`): a frozen `DisclosureEnvelope` — `ref`/`kind`/`title` identity, `summary` + `content` disclosed
  body, `children` as `ChildSummary` stubs, `mode` (`strict | auto | search_only`). The descent convention is the single behavioral
  requirement: a provider's tool accepts a `ref` and returns that node's envelope. Envelopes are thin — deeper levels exist only as
  refs.
- **No envelope-level `status` field (v1).** `DisclosureStatus` (`active | superseded | outdated | pending`) is shared vocabulary for
  provider-side lifecycles, deliberately not carried on the envelope: consumers must never assume a disclosed or listed unit is
  `active`, and engram's provider intentionally serves non-active units unfiltered. Surfacing status envelope-level (so a caller can
  filter, or a UI can dim a stale child) is deferred until a consumer needs it.
- **`kind` is an open set.** A page envelope's `kind` is the provider's own unit type (e.g. `memory-unit`); only `collection` and
  `section` envelopes carry fixed kinds. Consumers branch on ref prefixes, never on `kind` literals.
- **Three enforcement tiers.** *Guaranteed:* the `ProgressiveDisclosure` pydantic-ai capability — recognizes conformant results by
  shape (never by tool name), meters them via `approx_tokens` against a per-run budget (default 20k), replaces the result that
  crosses the budget with a `search_only` stub, and traces every exchange as a span. It ships no content. *Offered:* server-shaped
  leveling — the ordinary `fetch` tool becomes the leveled read (engram's `disclose` tool was retired in favor of fetch — agents
  get leveling without new vocabulary), and `/api/envelope` returns only the disclosed level + refs, so uncontrolled clients get
  progressive disclosure with no capability installed. *Conventional:* files stay directly readable; the discipline is documented,
  not fought.
- **engram is the first conforming provider.** `EnvelopeHandler` renders envelopes behind the `api.envelope_*` `SECURITY DEFINER`
  surface (migration 0012) so the read-only `wiki_reader` role serves the real path; child summaries come from compiled, governed
  `memory.unit_summaries` rows (`pending` until approved via `engram summaries approve`); truncation is signaled by a sentinel child
  (`ref = "truncated:<ref>"`).

**Consequences.**
- (+) Provider-agnostic: any tool that returns a conformant envelope gets budget governance and tracing for free; the dashboard can
  be a second consumer of the same envelopes.
- (+) Uncontrolled clients (Claude Code over MCP) still get leveling, because thinness is server-enforced.
- (−) `auto(N)` is not yet enforced; per-kind listing (`disclosed`) and per-collection mode (`disclosure_mode`) are declared in the
  corpus spec and enforced by the provider (migration 0013).
- (−) Envelope-level status filtering is not possible in v1 (see above) — accepted until a consumer needs it.
- (−) Measured (cold-start eval, 2026-08-09): agents rarely *choose* to disclose when search is coached harder — median recall@5
  0.900 through the production path, but adoption of descent depends on instruction competition; that is the next slice's lever, not
  a contract change.
