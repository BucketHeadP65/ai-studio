---
id: engram-adr-0037-okf-v02-projection
aliases: ["engram-adr-0037-okf-v02-projection"]
type: adr
app: engram
status: accepted
tags: []
title: "ADR-0037 — The OKF export projects to v0.2 (trust + lifecycle families)"
---

# ADR-0037 — The OKF export projects to v0.2 (trust + lifecycle families)

**Status:** Accepted (2026-07-29). Extends [ADR 0009](0009-okf-export-interchange.md)
(OKF export as an interchange view, vault decision D-24). Scope: the `export` slice's
projection target moves from OKF v0.1 to **v0.2**; the authoring schema and the
parse → resolve → project → write pipeline are unchanged.

**Context.** OKF v0.2 shipped the structured trust model v0.1 lacked — exactly the
convergence `okf.md` §11 predicted ("will v0.x grow structured provenance? If so, it
converges toward us"). The v0.2 deltas that matter to the projection: `timestamp` is
**replaced** by `generated: {by, at}` under an actor convention
(`<producer>/<version>` for tools, `human:<id>` for people); new optional families
`verified` (confirmation events), `status` (`draft | stable | deprecated`),
`sources` (per-source provenance with credibility signals), `stale_after`; and the
bundle-root `index.md`'s reserved structure is pinned to an `okf_version`-only
frontmatter block. Our vault already records richer versions of most of these
(the QA ladder, supersession, `updated`, papers' Zotero provenance), so v0.1's
export threw information away that v0.2 can carry natively.

**Decision.** The projector emits v0.2, mapping only what the vault honestly records:

- **`generated`** = `{by: engram/<installed version>, at: <updated>}`. The exporter
  is the producing actor — the same stance as the spec's reference enrichment agent,
  whose bundle-writing pipeline is the actor. `at` is omitted when a page never
  recorded `updated`; `by` (required within the family) is always present. The
  handler resolves the real version; the pure projector takes `generated_by` as a
  parameter (default `engram/dev`).
- **`status`** folds the QA ladder: `stub`/`draft` → `draft`; `synthesized`/
  `verified` → `stable`; a page with `superseded_by` → `deprecated` regardless of
  rung. The raw engram status stays lossless in `okf_x_engram`.
- **`verified`** = one event `[{by: <tool actor>, at: <updated>}]`, emitted **only**
  for QA-`verified` pages with an `updated` date. The tool actor classifies as
  v0.2's *machine-confirmed* trust tier — deliberately under-claiming, because the
  vault records *that* verification happened, not which human performed it. No
  `human:` actor is ever fabricated.
- **`sources`** = a paper's bibliographic record:
  `[{id: <zotero_key>, resource: zotero://select/library/items/<key>, title: <title>}]`.
  `id` serves the spec's claim-attribution purpose (the same stable key claims trace
  to via the mirror). Non-paper kinds derive from papers through the **claim layer**,
  which is not projected (ADR 0009's fidelity boundary), so they carry no `sources`.
- **`timestamp`** is dropped (v0.2 replaced it); **`stale_after`** is not emitted
  (the vault has no staleness-threshold signal — freshness is engram's own sweep).
- **`index.md`** frontmatter shrinks to `okf_version: "0.2"` alone (the pinned
  reserved-file structure; reserved files are exempt from the type rule).

**Options considered.** *Emit both `timestamp` and `generated`* (rejected: `timestamp`
is an unknown key in v0.2 — dead weight a consumer must ignore); *fabricate
`human:` verification actors from the QA ladder* (rejected loudly: over-claims the
trust tier; machine-confirmed is the honest floor); *wait for v1.0* (rejected for the
same reason as in ADR 0009 — the projection is thin and reversible).

**Consequences.** (+) The bundle now carries lifecycle + trust natively readable by
plain OKF consumers (status filtering, trust-tier classification) instead of only in
`okf_x_engram`. (+) The `okf_x_*` overlay is unchanged, so an OKF-aware-of-us
consumer is unaffected. (−) A strict v0.1 consumer sees unknown keys (`generated`,
`verified`, `sources`) — conformant either way, since consumers MUST tolerate
unknown keys. **Reversible?** Yes — the mapping is confined to the pure projector's
frontmatter/index builders; reverting restores v0.1 byte-shape.

**Related.** [ADR 0009](0009-okf-export-interchange.md) (the export slice and the
extension-key convention, both unchanged); `docs/explanation/okf.md` §11 (the
"watch the spec" trigger this ADR answers); reference doc:
[export](../reference/export.md).
