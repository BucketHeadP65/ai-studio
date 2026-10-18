---
id: adr-0011-documentation-estate-one-corpus
type: adr
title: One engram corpus for the whole documentation estate
summary: The entire documentation estate, from governed docs through the lifecycle dirs to the code plane, becomes one repo-docs corpus whose corpusspec is the single policy artifact, validated at authoring an
status: proposed
aliases:
- adr-0011-documentation-estate-one-corpus
app: root
absorbs:
- '[[spec-2026-08-18-documentation-policy-and-ingestion-design]]'
created: '2026-08-29'
---

## Context

The repo already carries a docs-as-code standard (ADR-0005: Diátaxis kinds, arc42-lite masters, federated app topology, split-doc sectioning) that governs how documents are shaped. Nothing governs how they are known. Retrieval over the documentation existed only over the working layer (the old workdocs corpus), while the governed estate of ADRs, architecture masters, reference and explanation pages, app-local docs, CONTEXT maps, lifecycle dirs, memory units and code stayed outside the corpus, so a fresh session answered a repo question by archaeology rather than by query.

Three constraints forced the design:

1. The repo's context knowledge lives in two planes that evaporate: the derivation behind decisions (brainstorms, units with provenance) and the rationale behind packages (which today has no addressable home at all).
2. Validation rules were duplicated. `validate_docs.py` hardcoded kinds, statuses and frontmatter requirements while engram compile enforced its own grammar, so the two could drift.
3. The estate must remain a working Obsidian vault. Every `[[id]]` link must resolve by filename or `aliases:` frontmatter, which constrains how ids and filenames may differ.

No accepted ADR already decides corpus ingestion of the estate. ADR-0005 decides document structure, ADR-0008 decides single-source configuration, and ADR-0011 decides a single retrieval toolset. This decision covers the documentation policy and ingestion itself, which was new.

## Decision

One documentation policy for the whole estate, enforced by one artifact, the corpusspec, and served as one engram collection.

- D1. Scope is the whole documentation estate, everything CONTEXT-MAP governs: root `docs/`, every `<app>/docs/` tree, every CONTEXT file, the lifecycle dirs (`docs/brainstorming/`, `docs/specs/`, `docs/plans/`, `docs/memory/Units/`), and the code plane (`docs/memory/Modules/`). `docs/implementation/` and generated split-doc artifacts are never ingested.
- D2. Topology is one corpus: one corpusspec, one collection named `repo-docs`, one retrieval surface. Relations may cross kinds.
- D3. Only working docs emit memory units (`brainstorming-doc` as today, `spec-doc` optionally). Governed kinds are the consolidated layer and compile as themselves. Promotion re-links the history: an absorbed unit gains an `absorbed_by` edge and moves to `superseded`, while `depends_on` remains the invalidation edge.
- D4. The corpusspec is the policy. `validate_docs.py` and engram compile both read `docs/corpusspec.json`, which grows to twelve page types, each declaring structure, disclosure and emission. Neither checker keeps a private copy of the rules.
- D5. The corpus identity renames in one dedicated slice: spec file to `docs/corpusspec.json`, slug `workdocs` to `repo-docs`, with the eval dataset, drift guards and compiled collection updated together.
- D6. The ingestion trigger stays deliberately open until the estate compiles and recompile cost is real. Until then compiling is a deliberate manual act, and staleness surfaces mechanically as a source-hash versus compiled-hash drift report.
- D7. The code plane lives in the corpus as a `module` page type, one page per Python package, carrying the code path, `governed_by` ADR ids, `applies` units, and limitations in frontmatter. The mapping is one-way: docs never point at code. Compile lints both ends (declared paths exist, declared ids resolve). Lookup is deterministic: file path, owning package, module page, the knowledge behind it.
- D8. Maps are app-grouped with `kind` as a filterable facet, never a second tree, and every listing obeys the disclosure ladder (inline, disclosed, clustered, search only) with spec-declared thresholds. The spine is app-first because a seeker knows where it is working before it knows which kind of document it needs.

Supporting rules the decisions depend on: the corpus stays a working Obsidian vault, so any page whose filename differs from its id declares the id in `aliases`, and the validator enforces reachability. Created and last-edited timestamps and authors are never authored frontmatter, they are enriched from git history at compile through a host-supplied provenance seam. Unit emission is batched and reviewed through git, never live. Rationale stranded in code comments migrates to the corpus, with code keeping contracts only.

## Consequences

Easier: a fresh session answers "why does X work this way?" by retrieval instead of archaeology, with a measurable gate (a second cold-start eval authored against the governed estate, plus code-plane cases). Package rationale gains an address (`mod.` pages), so the standing docstring rule finally has a destination. Validation has one contract checked twice, and drift guards generalize to per-kind id sets over the estate, including a guard proving `validate_docs.py` really reads the spec. Promotion of initiative knowledge into ADRs gains a mechanical re-linking path (`absorbed_by`).

Harder: every doc kind now carries a frontmatter contract, so the back catalog needed a retrofit and the validator's scope grew from three kinds to twelve. The code plane's incremental compile dirties pages by markdown content hash, so a filesystem-only change (a package rename) does not re-lint an untouched module page, full rebuilds remain the code plane's verification point until the trigger fork settles. Module pages and the estate map add content that must be maintained, and the plan's follow-up findings (the `meta` kind leaking onto maps, an app group already over the disclosed cap, the empty `docs/_meta/purpose.md`) show the ladder thresholds and app grouping need tuning from real cardinality, not the design's estimate.

What the repo must keep doing: compile as a deliberate manual act until the trigger is decided, honor the one-way rule when writing records (no prose path mentions going forward, back catalog cleaned opportunistically), keep `validate_docs.py` as the pre-commit ritual now exercising the compile contract, and keep the Obsidian reachability rule (id equals filename stem or sits in `aliases`). Open and deliberately not decided here: the ingestion trigger, the session-start retrieval interface, ladder thresholds and the clustered form, module-page coverage pace, and a per-kind declaration of which statuses count as not-current for the search predicate. Nothing hidden fires on commit, and no knowledge auto-injects on file-read, that is roadmap Phase 6, which this corpus merely gives an address to query.

## Options considered

Schema truth. Prose as the single policy source was rejected because prose cannot drive a checker, and keeping `validate_docs.py` hardcoded alongside a declared spec (approach B) was rejected because two rule copies always drift. A single JSON spec read by both checkers won because JSON stays stdlib-only for the pre-commit ritual and lets the spec absorb the old hardcoded vocabularies as data.

Code-plane mechanisms. Manifest files scattered through `src/`, frontmatter anchors on record docs, and prose path mentions in documentation were all rejected: they put the mapping at both ends of the link, which is what rots. One central module page type with a one-way mapping was chosen for a single lint surface and deterministic lookup.

Emission from governed docs (a shadow unit layer over ADRs) was rejected. ADRs are already the consolidated layer, re-emitting them would duplicate knowledge and split retrieval.

Automatic ingestion now. Git-hook or CI recompilation was rejected for this slice, not on the merits: nobody knows the recompile cost yet, so the trigger fork stays open with mechanical staleness surfacing as the interim guarantee.

Map topology. A kind-first tree was rejected for the map's first level (an agent knows its app before it knows its document kind), and unbounded fan-out was rejected outright, which is what the ladder encodes.
