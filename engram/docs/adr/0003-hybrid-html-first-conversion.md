---
id: engram-adr-0003-hybrid-html-first
aliases: ["engram-adr-0003-hybrid-html-first"]
type: adr
app: engram
status: accepted
tags: []
title: "ADR-0003 — Hybrid conversion: arXiv HTML first, Azure DI fallback"
---

# ADR-0003 — Hybrid conversion: arXiv HTML first, Azure DI fallback

> Recorded for the Zotero sync tool before it became an engram slice (root ADR-0003).

**Status:** Accepted (2026-06-09). Refines ADR-0001 (Azure DI is now the *fallback*, with the FORMULAS add-on enabled).

**Context.** Azure Document Intelligence `prebuilt-layout` *without* the FORMULAS add-on drops **all** mathematics; *with* FORMULAS it recovers math but as OCR-artifacted LaTeX (e.g. `\frac { 0 } { 5 }`, `\mathrm { S i m }`) and costs the add-on per page. A fair comparison on a real paper showed that for arXiv papers the published **HTML** version converted with **pandoc** yields the author's *exact* LaTeX and text (`\frac{1}{|\mathcal{F}_e|}`), for free. Most of the corpus is arXiv.

**Decision.** Convert **per item, HTML-first**:

1. Derive `https://arxiv.org/html/<id>` from the item's arXiv URL/DOI; HTTP GET (bounded timeout). On `200`, take the **HTML route**: pandoc (`pypandoc-binary`, bundled — no system install) to Markdown, then strip `<span>`/`<div>`/`<a>` wrappers and leading ar5iv boilerplate (TOC/widgets); a math-sentinel step preserves `<math alttext>` LaTeX that pandoc would otherwise drop.
2. Otherwise fall back to **Azure DI** `prebuilt-layout` + FORMULAS over the PDF.

The `converter` frontmatter key records which route ran (`arxiv-html/pandoc` | `azure-di/prebuilt-layout+formulas`); `source_hash` stays the **PDF** sha256 regardless of route. A REFRESH (version bump, same hash) preserves the existing `converter` label without re-converting.

**Consequences.**
- (+) Pristine math + text for arXiv papers (the majority), at zero Azure cost.
- (+) Provenance is explicit and survives reconcile; non-arXiv items still convert via DI.
- (−) Two conversion code paths to maintain; the HTML cleanup is heuristic (ar5iv chrome evolves).
- (−) Adds a pandoc binary dependency (bundled via `pypandoc-binary`).
- **Reversible?** The output contract (Markdown body + `converter` label) is stable and a reconvert re-runs the chosen route, so dropping the HTML path later is mechanical — but the dual-path contract and the provenance label are load-bearing, hence an ADR. A future unified local converter (e.g. docling) is flagged for evaluation.
