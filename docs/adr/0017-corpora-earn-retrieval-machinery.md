---
id: adr-0017-corpora-earn-retrieval-machinery
type: adr
title: Corpora earn their retrieval machinery at compile time
summary: 'A corpus declared `retrieval: auto` earns its machinery at compile time. Engram counts what the corpus is and flips three independent switches (embeddings, sql, graph) recorded on the compile manifest'
status: proposed
aliases:
- adr-0017-corpora-earn-retrieval-machinery
app: root
absorbs:
- '[[spec-2026-08-24-auto-retrieval-mode-design]]'
created: '2026-08-29'
---

## Context

Every corpus today carries the full retrieval machinery regardless of what it is. Embeddings run on every compile and every write, hybrid search answers every query, and the SQL and graph tools appear only when an owner mounts them by hand. A forty-note corpus pays the embedding tax for nothing, because PageIndex-style reasoning over the index beats vectors at that size. A large, densely linked corpus never gets the graph door unless someone thinks to flip it. The initiative folder 2026-08-24-auto-retrieval-mode (brainstorm `bs.auto-retrieval-mode`, design `spec-2026-08-24-auto-retrieval-mode-design`, plan `plan-2026-08-24-auto-retrieval-mode-plan`) answered an owner mandate of 2026-08-24 for a per-corpus retrieval ladder that engram computes at compile time. The design is executed and this ADR absorbs its durable decisions.

No accepted ADR in the repo's decision set already decides this. ADR-0014 decides that a per-corpus `CorpusSpec` drives a domain-general vault and ADR-0012 keeps retrieval strategy a per-run eval experiment axis, and neither decides which machinery a corpus carries. The closest standing rule is the embeddings-always-on doctrine (the never-pass `--no-embed` rule recorded in the agent-memory initiative, `bs.agent-memory-on-engram`), and it is a working-layer convention rather than an accepted ADR. This decision deliberately supersedes that doctrine, and only for corpora whose spec says `retrieval: auto`, only as decided by the compile, never by a lazy flag.

The constraints that ruled the alternatives out: consumers (the harness, the memory adapter, studio, the plugin) must not change what they call, so the mechanism has to live inside engram. Tiers must not flicker turn to turn, so movement happens at compile only, with hysteresis. Fixed-mode corpora must keep today's behavior exactly, so auto mode is scoped to a spec opt-in. Full-text search stays always on because it is free in Postgres and is the floor under everything.

## Decision

An auto-mode corpus earns its retrieval machinery instead of carrying all of it from the first note. `RetrievalSpec.mode` is `auto` rather than the default `fixed`, and only an auto-mode spec consults the switches at all. The mechanism is the switch set, not the ladder rung.

D1. Engram-private, automagic. The switch record lives on the collection's compile manifest and is engram's own business. No consumer reads it or changes its calls, and the one trace a consumer sees is the honesty line, every search result naming the leg that answered it (`lexical` or `hybrid`).

D2. Independent switches, not one rung. Three switches, each with its own trigger: embeddings (note count and token mass), sql (note count plus kind variety), graph (link density plus giant-component fraction). A five-thousand-note pile of isolated facts gets vectors without a useless graph door. The ladder (orientation-only, then lexical, then hybrid, then structured) remains the story people tell, and the switch set is the record.

D3. Both-way ratchet with wide bands, compile-time only. A switch turns on at its threshold and turns off only when the corpus falls below half of it, and switches move only when a real compile runs. Vectors already computed are kept on downshift, so they cost storage rather than spend and climbing back is cheap. The spec's optional `floor` and `ceiling` pins override the computation. A fixed-mode compile leaves any stored switches untouched, and an empty or missing switch record is never read as all off, because the gate is the spec's mode at the reader.

D4. Cheap counts now, measured quality later. The triggers are compile-time counters computed in plain SQL over the compiled tables, with openCypher on AGE where it is simpler. Watching real queries for lexical-recall degradation is a named future refinement, not this build, because it needs query traffic and a judge most corpora do not have.

D5. No owner flip for appearance, doors stay permission. When a corpus earns SQL or graph, the tools appear inside engram's own retrieval capability on the next turn with no owner action. The hosts' door switches remain on top with their existing meaning: auto decides what is available and the owner decides what is permitted, and a closed door stays closed no matter what the corpus earned.

D6. The low-rung index stays flat. Orientation at the small end is the existing listing grouped by kind, the surface the agent reasons over and descends by reading. A hierarchical map render waits until a live corpus overflows its injected budget.

D7. Thresholds are eval seeds, not truths. The seed numbers (embeddings on above 200 notes or 150k tokens, sql on above 1000 notes or 5 kinds, graph on at 1.5 edges per note with a 60 percent giant component above 100 notes, each with a looser off condition) seed the mechanism, and engram's eval suite tunes them, because retrieval quality per machinery per corpus size is exactly what the evals measure.

## Consequences

Easier. Small corpora stop paying the embedding tax on compile and write, and the memory corpus starts at the bottom of the ladder and climbs as the agent lives with no one flipping anything. Large linked corpora get the SQL and graph tools automatically, on the next turn after a compile. The honesty line tells the agent what machinery it is standing on without exposing why, and the memory adapter inherits routing for free because its search seam is already engram-side.

Harder. The embeddings-always-on doctrine is superseded for auto-mode corpora only, so every reader must keep gating on the spec's mode rather than on the column, and an empty switch record must keep meaning today's fixed behavior, never all off. Compile becomes the one writer of the record and the only thing that moves a switch, so an ingest-only auto collection gets no decision until a decision trigger exists, which is the named precondition for flipping the memory corpus's spec. The switch read fails closed: an error degrades to every switch off with one logged warning, never a failed tool call.

What the repo must keep doing. Consumers keep calling the same `search`, `fetch` and `ingest`, and the switch record stays ungranted except for its one reader door. Fixed-mode corpora keep byte-identical behavior, including the research corpus, which stays fixed-mode by convention because the harness adapter narrows its capability by concrete type. The known seams stand until paid: `filtered()` reflects what was built rather than what is currently earned, and the wrapped capability is reachable directly, the same doctrine the harness `Switched` door already follows. The eval suite owns threshold tuning under D7, and measured-quality triggers, the hierarchical map, and any dashboard surface for the switch set stay parked as the design's out-of-scope items, to be reopened deliberately rather than silently.

## Options considered

Keep fixed mode everywhere and mount doors by hand. Lost because it taxes every small corpus with embedding spend it cannot use, underserves large linked corpora, and depends on an owner remembering to act.

One tier per corpus as the mechanism, the ladder rung as the record. Lost because a corpus can earn one machinery and not another, so the rung collapses a five-thousand-note pile of isolated facts into the same tier as a densely linked one. The ladder survives as the mental model, the switch set as the mechanism.

An owner flag per corpus deciding its machinery. Lost because it recreates the hand-mounting problem it was meant to solve, and the embeddings decision was explicitly required to be made by the compile rather than by a lazy flag.

Measured-quality triggers, watching real queries for lexical-recall degradation. Deferred under D4, since it needs query traffic and a judge most corpora lack.

A hierarchical index render at the low rung. Deferred under D6, until a live corpus overflows its injected orientation budget.
