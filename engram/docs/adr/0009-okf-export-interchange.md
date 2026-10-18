---
id: engram-adr-0009-okf-export-interchange
aliases: ["engram-adr-0009-okf-export-interchange"]
type: adr
app: engram
status: accepted
tags: []
title: "ADR-0009 — OKF export is an interchange view, not a schema change"
---

# ADR-0009 — OKF export is an interchange view, not a schema change

**Status:** Accepted (2026-06-18). Implements vault decision **D-24** (OKF as an
interchange view) on the engram side. Scope: a new read-only `export` slice that
projects a compiled collection into an [Open Knowledge Format](https://github.com/GoogleCloudPlatform/knowledge-catalog/blob/main/okf/SPEC.md)
(OKF) v0.1 bundle. The authoring schema (`_meta/schema.md`) and the compile/serve
pipeline are **unchanged**; an OKF *importer* is out of scope (deferred behind a real
need, per D-24).

**Context.** Google's OKF (v0.1; Apache-2.0) formalizes the same LLM-wiki substrate
this vault uses — a directory of Markdown files with YAML frontmatter, linked into a
graph — at a strict *subset* of our rigor: one required field (`type`, a free string),
untyped links (the relation's meaning lives in prose), and a single optional
`timestamp` as the whole provenance model. The vault's design guide
(`_meta/design/okf/okf.md`, §8–§10) lays the two side by side: concept ↔ page,
`type` ↔ `type`/`entity_type`, links ↔ typed edges, `index.md`/`log.md` ↔ ours,
broken-links-tolerated ↔ dangling-`builds_on`-is-the-queue. We already satisfy OKF;
OKF does not satisfy us. The opportunity (okf.md §9) is a near-zero-cost **export**: we
are already Markdown + frontmatter, so projecting a conformant bundle is a thin,
hash-free stage that gives D-19 an at-rest, vendor-neutral wire format to complement
query-time federation, and lets non-Postgres consumers (Obsidian, Gemini/ADK agents,
Knowledge Catalog, a collaborator's repo) use our knowledge.

The one real design choice is **edges**: OKF wants plain Markdown links, but discarding
our relation *types* and claim provenance is the one thing we must not silently do.

**Options considered.**

- **Adopt OKF as the authoring schema.** Rejected: discards typed registry edges,
  `loc`+verbatim-`quote` claim provenance, comparative qualifiers, supersession, and
  contradictions — the entire value-add. OKF is a floor to export *to*, never a ceiling
  to retreat to.
- **Ignore OKF.** Rejected: forgoes a cheap interop + credibility win on a format aimed
  at exactly our shape.
- **Wait for v1.0.** Rejected: the export is a thin, reversible projection; if the
  format shifts, the stage shifts with it — the cost of being early is trivial.
- **Lossy-but-faithful export with extension keys (chosen).** Project each page to a
  conformant OKF concept; emit typed edges as both plain Markdown links (in a
  `## Relations` section and inline) **and** a non-standard `okf_x_relations` frontmatter
  key that preserves the relation type, plus an `okf_x_engram` key that preserves id /
  status / collection / facets / provenance. OKF permits arbitrary keys and mandates
  consumers ignore unknowns, so the bundle stays conformant while we stay lossless.

**Decision.**

- A new **`export` vertical slice** (`src/features/export/`) — a CQRS command
  `ExportOkf{collection, out_dir}` → handler running **parse → resolve → project →
  write**. It reuses the compile slice's `VaultReader` port and `resolve(...)` (no
  duplicated vault-reading, the same Obsidian link resolution) and adds nothing to the
  Postgres/embedding path: **no DB, no embeddings, no vault mutation.** The bundle is a
  rebuildable derivative (vault D-13 holds).
- The projection (the pure `projector.py`, unit-tested hardest):
  - **Skip `meta` pages** (contract/registry/log/dashboard/about are not knowledge).
  - **Path**: group by page kind into a subdir (`paper`→`papers/`, `entity`→`entities/`,
    `concept`→`concepts/`, `comparison`→`comparisons/`, `playbook`→`playbooks/`,
    `question`→`questions/`, `moc`→`maps/`); filename is the page slug (`id` minus the
    type prefix). Concept id = `<dir>/<slug>`.
  - **Frontmatter**: `type` (entity → `entity_type`; `moc` → `map`; else the page `type`
    verbatim — always non-empty), `title`, `description` (← `summary`), `resource` (← a
    paper's `artifacts.code`), `tags` (← the free-form `tags` list), `timestamp` (←
    `updated`) — each optional field omitted when absent.
  - **Extension keys**: `okf_x_relations` (page-level typed edges → the exported concept
    ids they resolve to; edges to dangling or skipped targets are dropped) and
    `okf_x_engram` (id, status, collection, non-empty facets, aliases, `superseded_by`,
    and — for papers — `paper_type`/`year`/`zotero_key`/`source_hash`).
  - **Body**: the canonical sections in authored order, with Obsidian wikilinks rewritten
    **fence-aware** (resolved → relative `.md` link; dangling/non-exported → plain text;
    `![[…]]` embeds treated like links; links inside fenced code left verbatim), plus a
    `## Relations` section listing the exported edges as human/LLM-readable links.
  - **`index.md`**: the bundle root, declaring `okf_version: "0.1"` and a
    progressive-disclosure directory listing grouped by subdir.
- The filesystem write is the slice's only IO, behind a `BundleWriter` Protocol port
  (`FilesystemBundleWriter` serialises frontmatter through `core.markdown.render_frontmatter`
  — the same `yaml.safe_dump` round-trip, and the exact inverse of `split_frontmatter` a
  consumer reads it back with).
- CLI: `engram export okf --out <dir> [--collection research]`, dispatched through the
  shared bus exactly like `compile`/`sync`. The `ExportOkf` route is registered on the
  bus by the composition root (`container.export_handler_provider`), the same lazy-route
  arrangement as sync; `features.export.deps.resolve_export_bus` is the single dispatch
  funnel.

**Consequences.**

- (+) Near-zero-cost, vendor-neutral export: a plain OKF consumer reads valid concepts;
  an OKF-aware-of-us consumer (or a future re-import) recovers the typed edges +
  provenance from the `okf_x_*` keys and the prose. The bundle is the at-rest complement
  to D-19's query-time federation.
- (+) Banks the credibility of a hyperscaler endorsing the Markdown-canonical substrate
  (D-13).
- (−) The `okf_x_*` extension-key convention is ours alone until an importer round-trips
  it; the export is lossy-by-omission for any consumer that ignores the extension keys
  (by design — that is what keeps it plain-OKF-conformant).
- (−) The export reads the vault from settings/env (like `compile`/`sync`); there is no
  `--vault` override on this command (a deliberate match to the bus-dispatch slices, not
  the note subcommands' direct-path resolution).
- **Reversible?** Yes, trivially. The slice is purely additive (a new package + two
  additive composition-root providers + an additive CLI sub-app); deleting it reverts
  the change with no impact on compile/serve/sync.

**Related.**
Vault decision **D-24** and its design guide `_meta/design/okf/okf.md` (the data model,
the projection, the worked example); vault **D-19** (collections & federation — the
at-rest/query-time pair) and **D-13** (Markdown-canonical, DB is a derivative);
[ADR 0002](0002-disposable-mirror-zotero-source-of-truth.md) (the disposable-derivative
stance the bundle inherits). Reference doc: [export](../reference/export.md). Patterns:
[vertical-slice DDD](../../../docs/explanation/design-patterns.md#vertical-slice-ddd),
[CQRS message bus](../../../docs/explanation/design-patterns.md#cqrs-message-bus),
[DI seams / fakes over mocks](../../../docs/explanation/design-patterns.md#di-seams-and-fakes-over-mocks).
