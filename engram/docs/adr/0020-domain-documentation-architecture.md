---
id: engram-adr-0020-domain-documentation-architecture
aliases: ["engram-adr-0020-domain-documentation-architecture"]
type: adr
app: engram
status: accepted
tags: []
title: "ADR-0020 — engram domain-documentation architecture (the layered three-view bounded-context pattern)"
---

# ADR-0020 — engram domain-documentation architecture (the layered three-view bounded-context pattern)

**Status:** Accepted (2026-06-29). Planned in the `engram-domain-docs` initiative (the whole-engram
documentation pass). Builds on the docs-as-code standard
([root ADR-0005](../../../docs/adr/0005-docs-as-code-standard.md) — Diátaxis + arc42 + C4 + MADR) and
generalizes the per-feature three-view package first shipped for incremental ingestion
([incremental-ingestion/](../explanation/incremental-ingestion/index.md), [ADR-0019](0019-true-incremental-ingestion.md))
to the whole engram domain. Scope: a new app-local arc42/C4 architecture master
(`engram/docs/architecture/`, graduated from the root master's §04 per ADR-0005's graduation rule); a
`engram/docs/explanation/domain/` three-view package (Business / Research / Engineering + a concept
concordance) anchored on five bounded contexts; the per-slice domain-model class diagrams, per-slice
C4 L3 component diagrams, and single system ERD that live in the master; the four gap reference
contracts (`conversion`, `mirror`, `zotero`, `wikinote`); a CONTEXT.md glossary audit; and an
onboarding how-to. No code, schema, `api` surface, or `wiki_reader` changes — documentation only.

**Context.** engram has grown to nineteen ADRs, a rich vision/solution-design doc, and eight slice
reference docs, but there is no single coherent entry: no plain-language account of the whole domain
for a non-technical reader, no per-slice domain models / ERD / component diagrams, four slices with no
reference contract, and engram's architecture is still a ~430-token section inside the cross-app
master. The incremental-ingestion package proved that one feature told three ways — Business,
Research, Engineering, over one shared glossary, tied by a concept concordance — lands for three
different audiences without the three accounts drifting apart. The open question, confirmed with the
user, was whether to scale that pattern to the whole domain and, if so, *where* the deep technical
content (system ERD, per-slice domain models, C4-component diagrams) should physically live so that it
neither bloats a single narrative essay nor duplicates the arc42 master and the ADRs. The driving
constraint is the codebase's standing rule — each fact has exactly one home; existing docs are
authoritative — held simultaneously with presenting the domain in three audience languages.

**Options considered.**

- **One big domain package (the incremental pattern, scaled up).** A single `explanation/domain/`
  package whose Engineering view is one self-contained IEEE-1016 SDD carrying the system ERD, every
  per-slice domain model, and all component diagrams. Rejected as the end state: at whole-domain scale
  the engineering essay becomes unmaintainable and restates the arc42 master and the ADRs, reopening
  the duplication the project forbids.
- **Per-slice federated three-view.** Every slice gets its own mini Business/Research/Engineering trio
  plus diagrams, federated by an index. Rejected: roughly twenty-one view documents fragment the
  whole-domain story and impose heavy concept-concordance upkeep for little gain over a single
  domain-level concordance.
- **Layered front-door over canonical homes (chosen).**

**Decision.**

Four load-bearing choices carry the documentation architecture.

**1. A thin three-view front door narrates; canonical homes hold the facts.** `engram/docs/explanation/domain/`
carries an `index.md` (a concept concordance + reader's guide), a Business view, a Research view, and
an Engineering view — each short, each linking *down* to where the fact actually lives. A view never
embeds a diagram that duplicates the master; it tells the story and points at the structure.

**2. The deep technical content lives in a graduated, app-local arc42/C4 master.** engram graduates
out of the root master's §04 (per ADR-0005's graduation rule) into
`engram/docs/architecture/architecture.md`, split-doc'd like the root master. It carries C4 L2
(engram-internal containers and data flow), per-slice C4 L3 component diagrams, the per-slice
domain-model class diagrams, and the single system ERD. The root master keeps the system-wide C4 L1/L2
view and its §04 shrinks to a summary plus a link down.

**3. Building blocks follow five bounded contexts.** The master's building-block sections are
**Ingestion** (zotero, conversion, mirror, sync), **Knowledge** (the wikinote kernel + compile),
**Retrieval** (serve), **Evaluation** (evals), and **Lifecycle & interchange** (export, init, plugin),
over a **cross-cutting** section (CQRS bus, DB foundation, DI / composition root, settings,
observability, CLI). The same five contexts anchor the concept-concordance rows, so the three views
and the architecture agree by construction rather than by manual upkeep.

**4. One home per fact, enforced by an information-architecture split.** The three views own narrative
+ concordance; the architecture master owns *structure* (component diagrams, domain models, the ERD);
each `reference/<slice>.md` owns the *contract* (entry points, public API, testing seams) plus a
*behavioral* runtime flow that links up to the master's component diagram; `vision.md` /
`product-decisions.md` / the ADRs own the *why* and stay untouched. Every diagram and contract cites
the source files it derives from and is verified against them (the initiative's hard-accuracy gate).

**Consequences.**

- (+) **A single coherent entry for three audiences** that provably agree — the concordance maps each
  concept across all three views to its canonical home, so a stakeholder, a researcher, and an
  engineer read the same domain, not three approximations.
- (+) **No duplication.** A slice's structure lives once (master), its contract once (reference), its
  story once (a view); the canonical vision and decision docs are linked, never restated.
- (+) **A reusable standard.** Future engram domain docs follow this layered pattern and the same five
  bounded contexts; this ADR is the citation that explains *why three parallel views*.
- (+) **engram's architecture becomes first-class** — a graduated arc42/C4 master with per-slice depth,
  instead of a ~430-token section in the cross-app master.
- (−) **The architecture master is a single split-doc file deepened across several delivery slices, so
  per-slice master edits serialize** (the reference docs and the three views parallelize). Backbone-first
  sequencing absorbs this.
- (−) **More cross-document link discipline.** The "links down / links up" structure only stays honest
  if `validate_docs.py` keeps every cross-link resolving — which the hard-accuracy gate enforces.
- **Reversible?** Yes, cheaply and additively. It adds documents and moves one section (graduation is
  itself reversible under ADR-0005); nothing in the code, schema, `api` surface, or `wiki_reader` role
  is touched.

**Related.** [root ADR-0005](../../../docs/adr/0005-docs-as-code-standard.md) (the docs-as-code
standard and the graduation rule this applies), [incremental-ingestion/](../explanation/incremental-ingestion/index.md)
(the per-feature three-view package this generalizes), [ADR-0013](0013-design-and-vision-docs-in-repo.md)
(the in-repo vision/decision docs the views link to rather than restate),
[ADR-0019](0019-true-incremental-ingestion.md) (whose package is this pattern's proof of concept).
Entry point: [CONTEXT-MAP.md](../../../CONTEXT-MAP.md). Glossary: [engram/CONTEXT.md](../../CONTEXT.md).
