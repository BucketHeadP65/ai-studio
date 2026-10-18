---
id: bs.memory-engineering-lifecycle
aliases: ["bs.memory-engineering-lifecycle"]
type: brainstorming-doc
title: Memory engineering for the development process
summary: Dialogue notes layering project knowledge, factoring findings into memory units with provenance, and untangling progressive schema disclosure over one compiled substrate.
status: active
emits:
  - "[[mu.lifecycle-as-consolidation]]"
  - "[[mu.enforcement-tiers]]"
  - "[[mu.disclosure-envelope]]"
---

# Memory engineering for the development process — draft

**Status:** Brainstorming (2026-08-08). Dialogue notes; no decisions made. **Prompt:** Every new session the coding agent knows nothing;
CLAUDE.md is one kind of memory. Project knowledge has layers — the software-engineering layer (stack, patterns, testing discipline) lives
in docs, but the working layer is the hard one: when exploring a new idea we need a spec for the brainstorming artifact *and* a structured
memory of findings gathered along the way (a tiny knowledge base per brainstorm). New documents are produced from brainstorming and during
development. What is the standard format of these docs, what memory is stored under each, and what about memories common to multiple docs?
It's all memory management — put order to the documentation processing, and put engram into play. **Trigger reading:** [Context vs. Memory
Engineering in Agentic AI Systems](https://machinelearningmastery.com/?p=24568) (via @beamnxw), plus the llm-wiki discipline (Karpathy's
"the LLM should maintain a wiki for you").

---

## 1. The problem, separated into layers

The cold-start pain is not one problem — it is four kinds of knowledge with different lifetimes, and today each has a different (or missing)
home:

| Layer | What it is | Lifetime | Where it lives today |
|---|---|---|---|
| **L0 Procedural** | How we work: conventions, patterns, testing discipline, gates | quasi-permanent | CLAUDE.md hierarchy, skills |
| **L1 Semantic (stable)** | What the system is and why: architecture, ADRs, reference | long, versioned | governed docs + CONTEXT-MAP.md |
| **L2 Working knowledge** | Active ideas: brainstorms in flight, research findings, decisions-so-far | weeks; some of it should survive forever | lifecycle dirs (brainstorming → specs → plans) — findings trapped *inside* prose docs |
| **L3 Session state** | The current conversation, intermediate results | one session | context window + docs/implementation |

The pain concentrates in **L2**. L0/L1 are solved-ish (CLAUDE.md loads always; governed docs are findable via CONTEXT-MAP). L3 is the
harness's job. But L2 — the tiny knowledge base that accretes around each idea — has no format, no shared layer, and no retrieval path. Each
brainstorm doc is an island holding everything inline.

## 2. The memory-engineering frame, applied

The article's split: **context engineering** decides what enters a single inference call (ephemeral, placement-sensitive); **memory
engineering** decides what persists across sessions and how it is written, stored, retrieved, trusted. The boundary between them is
**retrieval**: "memory determines what is available, context determines what becomes actionable."

Mapped onto a coding-agent project, the classic four memory types land precisely:

| Memory type | Stores | Repo artifact |
|---|---|---|
| **Working** | active task state | session context, `docs/implementation/` |
| **Episodic** | past interactions, runs, findings | brainstorm docs, research notes, session summaries |
| **Semantic** | durable facts, decisions, domain knowledge | approved specs, ADRs, architecture, reference |
| **Procedural** | learned workflows | CLAUDE.md, skills |

Two consequences fall out immediately:

**(a) The lifecycle is a consolidation pipeline.** brainstorming → specs → plans → implementation → promotion into governed docs is
*episodic-to-semantic memory consolidation* — the hippocampus-to-cortex move. A brainstorm is episodic (what we found, in order, with dead
ends); an approved spec is semantic (what is true and decided, dead ends dropped); an ADR is maximally consolidated (one decision, its
context, its consequences). The lifecycle already encodes the *stages*; what it does not yet encode is the **write policy** — what exactly
is extracted at each promotion, what is duplicated versus referenced, what decays or gets superseded.

**(b) Session cold-start is a retrieval problem, not a documentation problem.** Today the retrieval strategy is: CLAUDE.md loads
unconditionally (fixed context spend), and the agent greps/reads docs ad hoc. The article's failure modes apply directly: retrieval without
a context budget (dump every related doc into the window) and poor placement. The target is a **retrieval-to-context pipeline**: "we are
working on X" → retrieve X's memory pack → admit it under a token budget, positioned deliberately. That requires memory to be *addressable
by aspect* — which prose documents are not.

## 3. The everything-in-the-document problem, and the factoring move

Storing all findings inline in the brainstorm doc is the natural default and it has a failure mode the user already senses: **memories
common to multiple docs.** The judges brainstorm (sibling doc, 2026-08-08) already exhibits it — "verdicts are typed, signed evidence"
belongs to the trust doctrine, not to that doc; the harness repo's capability model will be cited by any future agent-infrastructure
brainstorm; the Logfire emission contract will be needed by ops docs. Inline storage means duplication; duplication means drift; drift means
contradictions with no owner.

The knowledge-compilation discipline's answer is factoring: **documents keep narrative and decisions; shared knowledge moves to entity/concept notes;
documents `[[link]]` to notes.**

- A **brainstorm's tiny KB** = the set of notes it created or touched — some scoped to the idea, some shared with the whole corpus. The doc
  becomes: prompt, dialogue arc, decisions (dated), open forks, and links. Findings live as notes with provenance.
- **Shared memories** are simply notes with more than one inbound link. No copy problem; one owner; contradictions become detectable (two
  notes claiming conflicting things about one entity) instead of silent.
- The three-layer compilation model maps cleanly: raw sources (conversations, fetched articles, explored repos — cite them, never edit them), the
  note layer (LLM-maintained, cross-linked), the schema (the lifecycle charter + doc/note frontmatter contracts).

And this is the engram play: **engram already is this machine** — memory-unit grammar, markdown-corpus compiler, compile-to-Postgres, retrieval
over CLI/MCP/HTTP, calibration loop. It is currently pointed at Zotero/papers. Pointed at the repo's own working knowledge, it turns L2 from
prose islands into a compiled, queryable memory: session start on topic X becomes an engram query, not an archaeology session.

Principles from the compilation discipline that transfer as-is: compile findings once at ingest (the brainstorm session *is* the ingest loop);
span-level provenance per claim; contradiction as first-class status, never blended; supersession for freshness (a revised decision points
forward, never silently overwrites); a periodic lint pass (orphan notes, dangling links, stale claims).

## 4. What "standard format" means here

The user's question "what is the spec of these docs" decomposes into two schemas, not one:

1. **Doc schema (per lifecycle kind)** — frontmatter contract: `kind` (brainstorm|spec|plan|implementation-state), `status`, dates,
   decisions (dated, with what they superseded), linked notes, provenance of the trigger. The body stays narrative — docs are for
   humans-and-agents *reading a story*.
2. **Note schema (the memory unit)** — engram's memory-unit grammar territory: entity vs concept vs claim, `[[links]]`, provenance chain
   (source, span, when, by what), status (`current | superseded | contradicted`), scope (idea-local vs shared).

The split matters because the two are retrieved differently: docs are read start-to-end when resuming a thread; notes are retrieved
selectively by aspect, under budget, into any session that touches them.

## 5. The target loop (sketch)

```
during a session      findings → notes (provenance, links)   [write policy: what qualifies?]
at doc promotion      brainstorm → spec: decisions consolidated, dead ends stay episodic
                      landing → governed docs: ADR/architecture updated, notes re-linked
session cold-start    "working on X" → engram query → memory pack (doc + linked notes
                      + shared doctrine) → context assembly under budget
maintenance           lint: orphans, contradictions, stale notes; supersession over decay
```

CLAUDE.md remains the L0 fixed segment; CONTEXT-MAP.md remains the human index; engram's compiled store becomes the machine index. The
agent-side auto-memory (MEMORY.md) stays for user/feedback facts — *project* knowledge belongs in the repo, versioned with it.

## 6. Open forks

1. **Dogfood vs product** — *decided 2026-08-08: dogfood first, then product.* Point engram at this repo's own working knowledge as the lab;
   the strategic goal is to make engram itself much more capable, and the general "memory engineering for any coding project" capability is
   extracted afterward from what survives real use. (More research papers will feed this thread later — via the Zotero pipeline, which means
   the note layer should stay compatible with paper-derived knowledge: one corpus grammar for both.)
2. **Unit of memory** — *decided 2026-08-08: note layer, piloted on the two live brainstorm threads* (judges/operators, memory engineering).
   Factor their findings into memory units and compile notes + docs; the live threads are the test corpus for the whole thesis. (Alternatives
   considered: compile existing docs as-is — quick retrieval but no shared-memory factoring; shared-doctrine notes only — deferred, subsumed
   by the pilot.)
3. **Where notes live** — a corpus dir in this repo? per-idea subdirs vs one flat corpus with scope tags? What is gitignored vs committed?
4. **Write policy** — when does a finding become a note: live during the session, or batched at doc promotion? How autonomous is the write
   (llm-wiki says: quarantine + gate unattended writes)?
5. **Retrieval interface** — engram MCP at session start ("load memory pack for X") vs a SessionStart hook injecting an index vs an explicit
   skill the user invokes.
6. **Contradiction & supersession policy** — for decisions specifically: a reversed decision must point at what reversed it (ADR discipline
   extended down to notes).
7. **Relation to the judges thread** — the sibling brainstorm's record-plane/evidence doctrine and this thread's provenance-carrying notes
   look like the same shape (typed, signed, provenance-first artifacts); probably one doctrine, two surfaces.
8. **Vocabulary** — *decided 2026-08-08: the atomic unit is a **memory unit** (`MemoryUnit`)* — the literature-converged term (HiGram's
   `MemoryUnit`, `MemoryEntry` in the MLM article, "memories" across Mem0/LangMem/Letta). "wiki\*" leaves the project vocabulary entirely
   (`src/wikinote/` → `src/memory/` in I1; "wikilinks" are just links); external names (DeepWiki, the llm-wiki skill) remain as citations.
   Naming standardizes on literature convergence, not project branding. *Extended 2026-08-08:* "vault" (Obsidian leakage) leaves too —
   the term is **corpus** (IR-converged; already house vocabulary via `core.ai.retrieval.CorpusSchema`), the spec file is
   `corpusspec.json`, and the Postgres schema `wiki` becomes **`memory`** (new tables immediately; existing tables renamed in one
   dedicated post-slice migration — plan Task 14, "the de-Obsidianification").
9. **Disclosure-logic ownership** — who declares vs enforces the threshold modes, and who owns search when a collection degrades to
   `search_only`; see §9(k)(2). Open; design after slice-1 field experience.

## 7. Pilot sketch — proposed defaults for forks 3–6 (NOT yet approved)

A reviewable proposal so the remaining forks have a concrete shape to react to:

- **Corpus location (fork 3):** `docs/notes/` at repo root, flat, committed to git — memory versioned with the code it describes; review =
  ordinary git diff. Scope (idea-local vs shared) via frontmatter, not subdirectories. Engram's compiler is domain-general, so pointing it
  at this corpus should be configuration, not new code — to be verified as the pilot's first technical step.
- **Note schema:** reuse engram's existing memory-unit grammar, extended per the adopted HiGram findings (§8): four-state status (`active |
  superseded | outdated | pending`), a `scope` field for conditional validity, dependency-typed links distinct from references, and hub
  notes as the abstraction layer. Dogfooding means no second grammar.
- **Write policy (fork 4):** batched, not live — notes are drafted at session end or doc checkpoints; the agent proposes, the human reviews
  the diff before commit. No unattended writes in the pilot (llm-wiki quarantine discipline, implemented for free by git). Qualifies as a
  note: a dated decision, a doctrine statement, or a finding referenced (or predictably referenceable) from more than one place.
- **Retrieval (fork 5):** engram MCP query at session start — "memory pack for thread X" = the doc + its linked notes + shared doctrine
  notes, admitted under a token budget. Until the compile target exists, the fallback is manual: the doc's `[[links]]` make the pack
  explicit enough to load by hand.
- **First concrete exercise:** factor the two live brainstorms. Candidate notes: `evidence-kinds-doctrine`, `three-planes-model`,
  `verdict-binding-algebra`, `harness-capability-model`, `emission-contract`, `lifecycle-as-consolidation`, `memory-types-repo-mapping`.
  Each with provenance back to its doc + session date. Success test: start a fresh session on either thread and reconstruct working state
  from the pack alone, without re-reading the full dialogue history.

## 8. Reading notes

### HiGram — hierarchical graph memory with path-level localization and rewrite
(arXiv:2608.05095, Yue et al., 2026-08-05 — read 2026-08-08)

An agent-memory framework built on exactly our problem: memory that must be *updated* over time as facts and feedback arrive, where flat
stores accumulate irrelevant context and unit-by-unit updates miss related changes.

**Their memory unit** is a subject–relation–object fact plus object category, context, timestamp, confidence, and a **status flag: `active |
superseded | outdated | pending`** — only `active` units participate in retrieval; `superseded`/`pending` preserve history without polluting
the evidence view. **Hierarchy:** upper-level nodes (subject, object-category, context) sit above units as an abstraction layer; retrieval
localizes coarse-to-fine through "MicroGraphs" (subject × category regions) → support subgraph → one scored **evidence path**, which also
bounds the rewrite region for updates. **Coordinated rewriting:** an update revises the affected units *and* their dependency edges — "a
dependent MemoryUnit is preserved when its supporting evidence remains consistent; otherwise it is marked outdated and excluded" — so
conclusions derived from invalidated premises don't linger. **Conflict taxonomy:** dynamic (temporal validity), static (resistance to
incorrect overwrites — supersede, never delete), conditional (value holds only under a scope). **Results:** best answer quality on LoCoMo at
~7% of full-context tokens; largest wins on temporal/adversarial questions and static/ conditional conflicts (e.g. 68.75% vs 43.75%
static-conflict accuracy against MemOS).

**What it changes for this thread** (*all four adopted 2026-08-08; paper added to Zotero*):

1. **Validates our status vocabulary — and adds `pending`.** Their lifecycle is nearly our sketch (`current/superseded/contradicted`) plus a
   quarantine state; our "agent proposes, human reviews the diff" pilot policy is literally their `pending` status. Adopt the four-state
   vocabulary in the note schema.
2. **The corpus wants an abstraction layer, not just flat notes + links.** Their hierarchy exists to make retrieval-under-budget cheap:
   coarse nodes route, fine units carry facts. For us: entity/concept hub notes over claim-level notes — which engram's compile schema can
   expose as a level to filter on. Evidence that "memory pack" assembly should descend a hierarchy, not grep a flat corpus.
3. **New mechanism we lacked: dependency-aware invalidation.** Our supersession policy pointed forward but had no answer to "what downstream
   conclusions does this reversal invalidate?" HiGram makes that computable via typed dependency edges between units + coordinated rewrite
   over a *localized* region. For the doc lifecycle: when a brainstorm decision is reversed, notes/spec sections that depended on it should
   be findable and markable `outdated` mechanically. → new open fork: do note links carry a *dependency* type distinct from mere reference?
4. **Conflict is three kinds, not one.** Our binary `contradicted` should split: temporal (was true, superseded), adversarial (incorrect
   overwrite attempt — resist), conditional (true under a scope — the `scope` field does real work). Decision notes are very often
   conditional ("for the pilot", "until X lands").

### PageIndex — vectorless, reasoning-based RAG over a tree index
(github.com/VectifyAI/PageIndex, ~35k stars — read 2026-08-08)

Builds a "table-of-contents" **tree index** per document — node = `{title, node_id, page span, summary, children}`, natural sections, no
chunking — and retrieves by having the LLM **reason over the tree** (agentic tree search), reading summaries and deciding where to descend,
like a human expert navigating a manual. Their argument: vector similarity is not relevance; *relevance requires reasoning*. Retrieval
becomes *traceable and explainable* — an explicit path of section references instead of "vibe retrieval". A "PageIndex File System" layer
extends the tree above documents (corpus → file → section) for million-document scale. 98.7% on FinanceBench, well ahead of vector RAG on
professional documents. Served via MCP/API; agentic demo on a mainstream agents SDK.

**What it changes for this thread** (*adopted 2026-08-08*):

1. **The escalation ladder gets a middle rung.** Navigation isn't only for "you know where you are": with summaries on every spine node, an
   LLM can *reason* its way down the tree even when it doesn't know where the answer lives — reading fan-out summaries, choosing a branch,
   backtracking. So: **deterministic walk → reasoning- guided tree search → associative search (vector/BM25/graph)**. Vectors drop to the
   *last* rung — for cross-corpus semantic soup, entity aliasing, similarity-shaped queries — not the default. §9's table amended
   accordingly.
2. **We're better-positioned than PageIndex on our own docs.** Their hard problem is *inferring* the tree from unstructured PDFs. Our
   working docs are born structured — the schema *defines* the tree — so the disclosure ladder is exact by construction; inference is only
   needed for external corpora (papers), where engram's Zotero pipeline already owns the ingest.
3. **Retrieval trajectories are judgeable evidence.** A reasoning-driven descent leaves an explicit path (which summaries were read, which
   branch chosen, why) — the same "evidence path" shape as HiGram's, and exactly the trajectory the sibling judges thread wants to score
   ("retrieval regression: same query, different path"). The two brainstorm threads touch here: reasoning-based retrieval makes retrieval
   *itself* observable and judgeable.
4. **Vectorless-first, not vector-never.** PageIndex is dogmatically vectorless; we keep the hybrid stance (llm-wiki: the compiled layer
   complements retrieval) — but the *default* flips: structure and reasoning first, embeddings as escalation.

## 9. Progressive schema disclosure — untangling the access-mode chaos

The felt chaos (2026-08-08): blending "the RAG knowledge base over corpora" with "progressive disclosure schemas over docs" with coding
docs, where a brainstorm's memory can be larger than the brainstorm itself, and it's unclear how it all fuses into engram with a
pydantic-facing surface. The untangling: **two access modes, one substrate, schemas as contracts.** The systems being blended are not
different systems — they are different *corpora × schemas* over the same machine, accessed in two ways.

**(a) Two access modes — refined to three rungs after the PageIndex read (§8).**

| Rung | Mode | When | Mechanics | Cost |
|---|---|---|---|---|
| 1 | **Deterministic walk** | you know *where* you are (resuming a thread, reading a known doc) | descend the spine; each level serves **summaries of the next level down** | O(depth), deterministic, cacheable |
| 2 | **Reasoning-guided tree search** | you don't know where, but the corpus is structured | LLM reads fan-out summaries, chooses a branch, backtracks — leaves a traceable path | tokens per hop, bounded by fan-out; explainable |
| 3 | **Associative search** | structure doesn't help (semantic soup, entity aliasing, similarity-shaped queries, cross-corpus multi-hop) | SQL / vector / BM25 / graph over everything | expensive, probabilistic |

Neither replaces the other — DeepWiki's production MCP is literally this split (`read_wiki_structure` / `read_wiki_contents` = navigation,
`ask_question` = search), and the llm-wiki discipline says it outright: *the retrieval ladder is a protocol agents choose, not a topology.*
"Crazy SQL/vector/graph is often overkill" is the navigation mode's reason to exist: it is the **cheap-deterministic-first rung**, with
search as the expensive escalation — the same ladder shape as the judges thread's operator → monitor → judge.

**(b) One substrate.** Every corpus — papers corpus, governed docs, working docs, notes — compiles into one store with: a **structural
spine** (corpus → doc → section → subsection → claim/note, where *every node carries a summary of its children* — HiGram's upper-level
nodes, generalized); **typed links** (reference vs dependency, adopted in §8); and **indexes** (vector/BM25/graph) as disposable
derivatives. The "map of context" experience — doc summary → pick a doc → section summaries → subsection summaries — is just walking the
spine.

**(c) The schema is a three-phase contract.** Per doc kind (brainstorming, spec, ADR, paper, memory-unit), one specification declares:

1. **Structure** — required sections/subsections → *authoring-time validation* (a brainstorm MUST have prompt, decisions, open forks, …);
2. **Disclosure** — what each level exposes as the agent descends → *compile-time*: the summary ladder is produced/checked from the schema,
   so "the layers automatically given as summaries" is a compiled artifact, not a convention;
3. **Emission** — what memory the kind emits (which notes, links, provenance) → the *write policy per kind* (§2's missing piece, now
   schema-resident).

"The specification defines the structure and also defines the hierarchical progressive disclosureness" becomes literal: the schema is data
driving validation, compilation, and serving.

**(d) The doc is a projection of its memory.** A brainstorm's memory (notes, findings, provenance, links) legitimately dwarfs the doc — no
paradox: the doc is the *narrative spine*; factored notes hang off it via links; the "tiny KB per brainstorm" is simply the subgraph
reachable from the doc node; the disclosure ladder surfaces notes at the appropriate depth.

**(e) The serving surface — three verbs, not thirty tools.**

```
map(node)              → children + their summaries        (walk the spine)
open(node)             → full content at that level        (commit tokens deliberately)
search(query, filters) → cross-corpus associative hits     (the escalation rung)
```

DeepWiki's MCP triad, generalized. Ships as engram MCP tools and/or a pydantic-ai toolset/capability that also injects the session-start
memory pack; respects the 30-tool cliff by construction. Note the pattern is fractal: deferred tool loading / ToolSearch is progressive
disclosure applied to *tool schemas* — same principle, one level up.

**(f) The rungs compose within one query journey** (refinement, 2026-08-08). Search and disclosure are not alternatives — they interleave:
search lands the agent at a node ("found the paper"), disclosure governs the reading from there (descend its map); navigation scopes search
("search only under this subtree"). A query journey is an alternating sequence of rungs, each hop leaving a trace.

**(g) The catalog is itself a disclosable corpus** (refinement, 2026-08-08). At scale the KB's self-description — entity vocabulary, edge
types, clusters/groups, per-corpus stats, exemplar queries — outgrows any context window (thousands of entities and edges). It gets the same
spine treatment: top-level map of the database (corpora, entity groups, edge kinds, counts) → clusters → types → instances, with retrieval
over the catalog as its own escalation rung. This is what an agent reads to learn *how to query* the KB: progressive disclosure of the
**query surface**, not just the content. Content, catalog, and tool schemas all disclose the same way — the pattern is fully fractal.

**(h) Enforcement locus — three honesty tiers** (refinement, 2026-08-08). A disclosure *convention* is worthless if the agent can ignore it;
the protocol is only as real as the runtime carrying it:

| Tier | Runtime | Guarantee |
|---|---|---|
| **Guaranteed** | engram's own pydantic-ai agents | memory is reachable *only* through the three-verb toolset; schemas are `Disclosable` models — each level a typed view, summary fields at level-0 — so tool **return types are the ladder**; a capability holds the descent cursor, enforces per-rung token budgets, and emits the descent path as telemetry (the judgeable retrieval trajectory) |
| **Offered** | MCP (Claude Code, external agents) | same three verbs; the **server** enforces shape — summaries by default, capped payloads, node-by-node descent — client behavior stays uncontrollable |
| **Conventional** | direct filesystem access | unenforceable; schema conventions + CLAUDE.md discipline are all there is — admitted honestly |

The "automagically works for agents, always" requirement lands in the guaranteed tier: embed the disclosure contract once in the schema
(`Disclosable`), make the toolset generic over it, and every new card/doc kind gets map/open/search behavior with zero new code. This is the
engram-owns-the-runtime argument for building the agent surface on pydantic-ai rather than relying on any external agent's goodwill.

**(i) Disclosure modes — per-node policy with graceful degradation** (2026-08-08). What does `map(node)` return when the children number 10
vs 1,000 vs 100,000? Answer: a node's **presentation form** degrades gracefully with cardinality, and a **mode policy** governs the
movement:

| Presentation form | Shows | Requires (compiled artifact) |
|---|---|---|
| `inline` | full contents — disclosure would be overhead | nothing |
| `disclosed` | item summaries, descend on demand | per-item summaries |
| `clustered` | summaries *of clusters* — 1,000 item summaries is still too much | hook-built clustering + cluster summaries |
| `search_only` | a generic description of the collection + the search verb only | index + collection description |

Modes as policy over the forms: **`strict`** pins summary-first always; **`auto(N)`** starts inline and moves a collection down the ladder
as same-kind cardinality crosses tunable thresholds; **`search_only`** pins the last form. Crucially, `search_only` does not exit the
system: **retrieved hits come back as disclosed nodes** — you search into the collection, then resume the ladder on what you found.
Presentation modes decide *what a node shows*; access rungs (§9a) decide *how you move*; the system never exits disclosure, it re-enters at
finer scope.

**Hooks — the maintenance machinery the degradation needs.** You cannot switch to cluster-summaries unless someone built the clusters.
Event/condition hooks (tunable) watch collections over time: a deterministic *operator* tracks cardinality/size; a threshold-crossing
*monitor* event triggers governed maintenance agents (summarizing agents, clustering agents) that compile the next-coarser representation
under the fork-10 write governance. This is the sibling judges thread's operator → monitor → governed-action pattern applied to the memory
system itself — the KB has its own telemetry-and-maintenance loop.

**Localized schema disclosure.** The policy attaches per-*subtree*, including inside a single schema instance. Example: pydantic-ai's
`AgentSpec` — dozens of JSON fields; show the agent's summary first, and nested sub-objects collapse to a one-line *description* until
explicitly opened. So `Disclosable` is not only for corpus nodes: any pydantic model subtree can carry a disclosure annotation the
renderer/toolset honors — one contract from corpus root down to a leaf field. (Example of the whole composition: 1,000 skills — `disclosed`
is still 1,000 summaries → degrade to `clustered` (skill-group summaries) or `search_only` with a generic description; search returns five
skills *as disclosed nodes*; each skill's spec then disclosed field-by-field.)

**(j) The three roles** (2026-08-08; *supersedes the earlier "two consumption points" version of this section — that draft had the
consumer building a first-party toolset and parking payloads, which was the wrong split*). **Provider / spec / consumer:**

- **Provider** — anyone with a DB and a retrieval tool (engram first). Owns the data, the compiled summaries, and any maintenance hooks.
  Keeps its existing tools; conformance means its *results* follow the spec.
- **The spec** — defines only the format of a conformant tool result: a **thin envelope** (the disclosed node + children as summaries +
  opaque refs + mode) and one behavioral requirement, the **descent convention** (the provider's tool accepts a ref and returns that
  node's envelope). No tools are defined by the spec; no first-party toolset exists anywhere in the design.
- **Consumer runtime** — a generic `ProgressiveDisclosure` pydantic-ai capability: a thin protocol handler that recognizes conformant
  envelopes, governs context injection, offers descent, tracks the cursor, enforces budgets. It ships no content — no summaries, no
  hooks, no storage.

Thin envelopes give **server-enforced leveling**: even uncontrolled MCP clients get progressive disclosure with no capability installed;
the capability adds the owned-runtime discipline on top (cursor, budgets, traces).

**(k) Parked for post-slice revisit** (2026-08-08, user-raised; deliberately NOT in slice 1):

1. **Type-level disclosure binding via pydantic's native `ConfigDict`.** The idea: pydantic's own config machinery carries the
   declaration — `model_config = ConfigDict(disclosable=True)` — and a disclosable model automatically gains a **private** `_disclosure`
   attribute (`PrivateAttr`, populated at construction; mandatory or defaulted per business rules). The strong argument is collision
   avoidance: the protocol never reserves a *public* field name, so a user model with a genuine `disclosure` field cannot clash with the
   spec. This is native-`ConfigDict` territory — i.e., plausibly an **upstream pydantic / pydantic-ai contribution**, not a house
   subclass; today an unknown config key isn't honored (type checkers flag it, runtime ignores it), so any near-term realization is a
   thin mixin reading the key — a stopgap, not the idea. Natural extension: SQL schema definitions (the `CorpusSchema`/search builders)
   read the same declaration to generate leveled views — schema-as-contract driven down to the type level. Risks to weigh at revisit:
   private attrs are invisible to serialization (right for construction-time metadata, wrong if the wire needs it);
   magic-at-a-distance vs the explicit envelope; upstream acceptance timeline.
2. **Who owns the disclosure logic?** The threshold behavior — ≤10 inline, 10–100 disclose, >100 expose a *search tool* instead (exactly
   pydantic-ai's deferred-tools/tool-search pattern, applied to data). Candidate owners: the **schema** (declares the policy as data,
   e.g. `auto(10, 100)`); the **provider** (enforces for served corpora — it owns the index, so it owns search); the **runtime
   capability** (enforces for in-process collections; may own a trivial local search); the **framework** (pydantic-ai upstream, if this
   ever generalizes). Leading intuition: policy is *declared* once in the schema, *enforced* by whichever party materializes the
   collection, and search is owned by whoever owns the items. Design after slice-1 field experience.

**Fork 10 — summary provenance:** *decided 2026-08-08: compiled, governed.* Disclosure summaries are derived data, regenerated when the
source changes — staleness is mechanical (source-hash → stale → recompile), never silent rot. They are autonomous LLM writes, so they fall
under the write-governance discipline: quarantined/eval-gated, logged, revertible. (Alternatives considered: authored-at-promotion —
trustworthy voice but rots; hybrid authored-abstract + compiled ladder — deferred, can be revisited if compiled doc-level summaries prove
tonally lossy.)

## Sources

- [Context vs. Memory Engineering in Agentic AI Systems — MachineLearningMastery](https://machinelearningmastery.com/?p=24568) (shared via
  [@beamnxw](https://x.com/beamnxw/status/2084647643314102600))
- [HiGram: Hierarchical Graph Memory for LLM Agents with Path-level Localization and Rewrite —
  arXiv:2608.05095](https://arxiv.org/abs/2608.05095) (Yue, Wang, Zhang, Zheng, Zhang; 2026-08-05) — see §8 reading note
- [PageIndex — Vectorless, Reasoning-based RAG](https://github.com/VectifyAI/PageIndex) (VectifyAI) — tree index + reasoning-based tree
  search; see §8 reading note
- llm-wiki skill (Karpathy seed gist; DeepWiki as production anchor; synthadoc/LLM-Wiki-v3 conventions)
- Sibling brainstorm: [[2026-08-08-judges-operators-over-telemetry-draft]]
