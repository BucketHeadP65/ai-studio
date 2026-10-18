---
id: adr-0012-youtube-provider-and-inbox-corpus
type: adr
title: YouTube intake through a SourceProvider adapter and an inbox corpus
summary: 'Absorbs the knowledge-intake design: YouTube capture lands as a second SourceProvider adapter backed by an intake ledger, transcripts mirror in chunker-shaped markdown, and unsorted knowledge arrives '
status: proposed
aliases:
- adr-0012-youtube-provider-and-inbox-corpus
app: root
absorbs:
- '[[spec-2026-08-20-knowledge-intake-design]]'
created: '2026-08-29'
---

## Context

The studio brainstorm on knowledge intake (bs.studio-knowledge-intake) asked for paste a YouTube URL and end up with the transcript as a searchable source, plus an inbox corpus to receive knowledge whose final home is not yet known. The design for this lives in the 2026-08-20 knowledge-intake initiative (spec, plan, brainstorm, design: spec-2026-08-20-knowledge-intake-design), which this ADR absorbs.

The sources pipeline today serves Zotero only. The identity slice has already generalized sources behind the SourceProvider port (list items, resolve one by provider key), with Zotero as the first adapter, and the sync slice is an idempotent reconcile that drives the provider, converter and mirror-store ports. Mirror disposability is already decided: engram ADR-0002 (disposable mirror, upstream as source of truth, state in frontmatter) holds, and this ADR does not reopen it. What is not yet decided anywhere is how a source with no external library (YouTube has nothing like a Zotero library to list) plugs into that reconcile loop, and where knowledge lands before anyone knows where it belongs.

Constraints that shaped the decision: no new pipeline concepts may be introduced (the URL must become a source mirror exactly as a Zotero paper's PDF does), compile, chunking, embedding, retrieval and quote verification must be untouched, and the chunker only assigns section paths to numbered H1 to H4 headings.

## Decision

D1. YouTube is a SourceProvider adapter, not a new pipeline. YouTubeProvider is the second adapter after Zotero, with provider "youtube", provider_key the 11-character video id, and source_id the video id. It lives in engram/src/engram/sources/youtube/ as pure domain plus one adapter, mirroring sources/zotero/'s layout. It won over a bespoke ingestion path because the identity slice's port already covers everything a second source needs, and the sync handler is not modified at all.

D2. Capture is a ledger, sync stays the reconcile loop. Because YouTube has no library to list, the corpus gets one: _meta/intake.yaml, a list of requested URLs with status requested, resolved or failed with a reason. Pasting a URL appends a row (CLI now, studio UI later per the corpus-management spec). The provider's list reads every non-failed row, requested and previously resolved alike, re-fetching each transcript fresh so SKIP and RECONVERT have something to compare against and no resolved row drops out of the present-key set that guards against orphan archival. resolved is stamped at list time, before any mirror write, so a dry run still fetches over the network and updates the ledger and only skips the write. A fetch failure on an already-mirrored video marks the row failed, which drops it from the present-key set and archives its mirror like a removed item. Recovery is manual by choice: the owner resets the row to requested in the ledger and the mirror returns through the ordinary CREATE path, with no special-case unarchive.

D3. Transcript fetch is httpx against YouTube's public endpoints under the house HTTP-boundary rules: validated frozen response models, a 30 second timeout on every call site, and failures raised as the conversion error that marks the ledger row failed, never a silent skip. A video with no caption track is a failed: no-transcript row. A speech-to-text fallback is a fork, not this slice.

D4. Transcript markdown is written for the chunker. The mirror body is segmented as numbered headings of the form ## n [mm:ss] title, one per topical segment, so every retrieval hit's section_path maps straight back to a video offset. What ships is windowed only (roughly 3 minute windows headed by their own leading words). The segmenter's chapter-boundary path is real and tested but dormant, since nothing populates the chapter data today.

D5. Mirror frontmatter is the provider-neutral shape from the identity slice (source_id, provider, provider_key, source_hash as the transcript body hash, converter youtube-transcript, plus title, url, channel, published). The zotero_key and zotero_version fields stay stamped on every provider alike because they remain the mirror plane's join key across the store, compiler, export, quote verification and the database surface. Retiring them is explicitly deferred, and any future re-key must migrate already-authored talk pages that cite through the literal field name.

D6. The inbox is an ordinary corpus, shipped as a scaffold recipe. studio init gains an --inbox preset: slug inbox, page types note and talk (a talk page is the authored digest of one video), no facets, no claims, sources enabled with provider youtube, retrieval collection inbox. It compiles, searches, cites and later promotes page by page like any corpus, and is never a special case in code.

D7. One provider per corpus, for now. SourcesSpec.provider stays a single value. The inbox wants YouTube, research wants Zotero, and nothing today wants both. A tuple of providers with per-provider mirror partitioning is a parked fork.

Options considered. A dedicated ingestion pipeline (its own command, store and retrieval tier) was rejected because it duplicates the mirror, compile and reconcile machinery the identity slice just generalized, and would need its own orphan handling. Parking capture in the studio UI first was rejected because the CLI plus ledger is the smallest end-to-end win and the UI layer calls the same machinery later. A Whisper speech-to-text fallback for caption-less videos was rejected for this slice on cost and latency, and stays opt-in behind the same provider when a real corpus accumulates enough failed: no-transcript rows to matter.

## Consequences

What becomes easier. A second source kind now lands as an adapter plus a ledger row format, not a pipeline change. Any future provider (feeds, arXiv listings, article URLs) reuses the ledger pattern, and the scheduled-ingestion work already generalizes it. The inbox gives unsorted knowledge a real home immediately: it is compiled, searchable and citable from day one, and promotion can move pages out one by one later.

What becomes harder. A provider-backed sync is not network-free, even with --dry-run, and an unlimited sync re-fetches every mirrored transcript each run, so cost grows with corpus size. A failed refetch silently archives an existing mirror until someone resets the ledger row by hand, and no retry command exists yet. Chaptered videos segment on fixed windows rather than their real chapter boundaries until the dormant chapter path is fed.

What the repo must keep doing to honor the decision. Keep the sync handler generic: if a provider need ever forces a change to SyncMirrorsHandler, stop and surface it as a concern rather than adapting the handler quietly. Keep mirrors disposable per engram ADR-0002, so delete the mirror, re-sync, it regenerates. Keep the segmenter's headings matching the chunker's numbered-heading rules by importing the real regex, not copying it. And when the zotero_key re-key fork is eventually taken, migrate the talk pages that cite through it along with engram's own code.
