---
id: engram-adr-0041-k-source-policy-seam
aliases: ["engram-adr-0041-k-source-policy-seam"]
type: adr
app: engram
status: accepted
tags: []
title: "0041 — Search `k` becomes host policy behind a live `k_source`"
---

# 0041 — Search `k` becomes host policy behind a live `k_source`

**Status:** Accepted (2026-08-11)

**Context.** The `search` tool `RetrievalCorpus` builds exposes `k` (hit cap, default 12) as an agent-tunable parameter, and every door —
the in-process capability, the MCP server, the evals harness — serves that signature. The AI Factory Studio's Explore station needs the
opposite ownership: retrieval hyperparameters are policy the UI holds (a live knob a person turns mid-session), not a choice the agent
makes per call. But existing hosts rely on agent-tunable `k` — evals baselines were measured against it, and the MCP contract publishes
it — so removing the parameter outright would repoint every consumer at once.

**Decision.** Hide, don't remove, and let the host decide. `RetrievalCorpus` gains `k_source: Callable[[], int] | None = None`. With the
default `None`, nothing changes: agents see `k`, default 12. With a source set, the search tool is built *without* `k` in its schema — the
agent cannot see or send it — and every call resolves the cap from the source at call time, so a host holding a mutable knob (the Explore
session) changes the very next search without rebuilding the capability. The same parameter threads through every door that builds a
search tool: `embedded_retrieval`/`embedded_library` forward it into their `RetrievalCorpus`, and the MCP door — whose `search` is
registered by `register_mcp_tools` over `build_retrieval_toolset`, not a `RetrievalCorpus` — accepts the identical `k_source` in those
factories. The application's `RetrievalSettings` gains `agent_tunes_k: bool = True` and `k_default: int = 10` so config-driven hosts (the
MCP server) derive a constant source from settings. Tool descriptions follow the mode: the hidden form never mentions `k`.

**Consequences.**
- (+) One seam, every door: the station's live knob, a config-pinned MCP server, and tomorrow's policy engines all inject through the same
  callable, and the resolved cap is recordable per call (the Explore journal stores `k_in_effect`).
- (+) Default-safe: no eval baseline or existing contract moves until a host opts in.
- (−) `search` now has two schema modes; capability tests and drive-contract pins must cover both, and description text must match the
  mode it ships with.
- (−) A `Callable[[], int]` is deliberately minimal; if policy grows past one integer (per-corpus caps, budgets), this seam widens into a
  policy object rather than sprouting siblings.
