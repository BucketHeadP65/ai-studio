---
id: engram-adr-0001-azure-document-intelligence
aliases: ["engram-adr-0001-azure-document-intelligence"]
type: adr
app: engram
status: accepted
tags: []
title: "ADR-0001 — PDF→Markdown via Azure Document Intelligence (Managed Identity)"
---

# ADR-0001 — PDF→Markdown via Azure Document Intelligence (Managed Identity)

> Recorded for the Zotero sync tool before it became an engram slice (root ADR-0003).

**Status:** Accepted, refined by [ADR-0003](0003-hybrid-html-first-conversion.md) — the FORMULAS add-on is now enabled, and Azure DI is the *fallback* in a hybrid that prefers arXiv HTML→pandoc.

**Context.** Layer-1 sources must be lossless Markdown. The corpus is born-digital papers whose hard parts are **tables, math, and figures**. Three converter families exist: (a) deterministic, no-OCR text extraction (PyMuPDF4LLM) — lossless for prose/reading-order but weak on tables/math; (b) cloud ML OCR (Azure Document Intelligence `prebuilt-layout`) — tables→HTML with merged cells, math→LaTeX, figures→`<figure>`; (c) local ML (Marker/Docling/MinerU) — good structure, heavier, non-deterministic. "Deterministic + no-OCR + faithful tables/math" cannot all be had at once.

**Decision.** Use Azure Document Intelligence `prebuilt-layout` with `output_content_format=MARKDOWN`, authenticated via **Managed Identity** (`DefaultAzureCredential`) — no API key in env or repo. Locally, `az login` credentials stand in for the managed identity; in Azure, the resource's MI is used. RBAC: the identity needs **Cognitive Services User** on the Document Intelligence resource.

**Consequences.**
- (+) Faithful tables (HTML), math (LaTeX, renders in Obsidian), section hierarchy; one API call per PDF; ~$0.01/page (~$5 to backfill 33 papers, pennies/paper after).
- (+) No secret management (MI).
- (−) PDFs are uploaded to Azure (acceptable: public arXiv papers) and the path is **cloud ML OCR**, the opposite of deterministic/no-OCR — accepted trade-off for fidelity.
- (−) Adds an Azure dependency + RBAC setup.
- **Reversible?** The converter sits behind the `DocumentConverter` port, so swapping engines is a one-adapter change — but re-converting the whole corpus is the real cost. Hence an ADR.
