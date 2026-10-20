---
id: engram-adr-0051-a-sync-admits-one-mirror-per-source
aliases: ["engram-adr-0051-a-sync-admits-one-mirror-per-source"]
type: adr
app: engram
status: accepted
tags: []
title: "0051: A sync admits one mirror per source"
---

# 0051: A sync admits one mirror per source

**Status:** Accepted (2026-10-20). Extends [ADR-0002](0002-disposable-mirror-zotero-source-of-truth.md): the
connector stays the source of truth for its own items, and a source two connectors hold is mirrored once.

**Context.** Each connector keys its items its own way: the Zotero connector by the library's item key, the source
queue connector by `arxiv-<id>`, `x-<status id>` or a URL hash. The sync decided per item by its own key alone, so a
paper the Zotero library held and the source queue also requested got two mirrors, two sets of chunks and two digests.
The research knowledge base held six papers twice this way (for example `HEMH5NU7` and `arxiv-2201.11903`). A
question about one of them scored the other copy as a miss, and search showed both. `request_source` already refused a
URL an indexed mirror holds, but a watch-list scan, a queue row requested before the other mirror existed, and a
library item added after the source queue brought the same paper all reached the sync unchecked.

**Decision.**

1. A source's identity is the set of identifiers that name it whatever connector brought it: its URL, its arXiv id
   (read from an arxiv.org abs, pdf or html URL, or from an arXiv DOI `10.48550/arXiv.<id>`) and its DOI (in lower
   case, from the DOI field or a doi.org URL).
   `source_identities` in `engram.sources.domain.source_queue` answers it from a URL and a DOI. It sits beside the
   identifier readers `arxiv_id_of` and `doi_of` that classification and the index already use, and reads no
   knowledge base field name.
2. `MirrorFile` carries the mirror's `url` and `doi`, read back from the frontmatter every mirror already writes.
3. Before an item with no live mirror of its own is converted, the sync looks for an active mirror under another key
   that holds any of the item's identifiers. When one does, the item is not admitted and lands in
   `SyncReport.already_mirrored`, its key mapped to that mirror's key. The first mirror in wins. A second item of one
   source within one connector's run is refused the same way, and that connector's dry run reports the same refusals.
   Across connectors an applying run refuses too, since each connector's handler reads the mirrors the one before it
   wrote. A dry run writes nothing, so a later connector's plan does not see an earlier connector's planned mirror.
4. An item that has a live mirror of its own keeps converging, and a removed mirror holds nothing, so a source comes
   back under the other connector once its mirror is archived.
5. `studio sync` prints the refused items with the mirror that holds each, and a refresh's detail counts them.

**Consequences.**

- (+) A paper reaches the knowledge base once, whichever connector brings it first, so its digests, chunks and
  search hits stay with one source.
- (+) The rule names no host and no connector. A connector that fills a URL or a DOI is covered with no change.
- (−) A refused item stays in its connector's library and is reported on every sync until the owner removes it there,
  or until the mirror that holds its source is archived.
- (−) Admission runs before the orphan pass. An item whose key changes while its URL stays (a library item deleted and
  added again) is refused while its old mirror is active, then the same run archives the old mirror, so the source
  has no active mirror until the next sync admits it.
- (−) Two copies of one paper that share no identifier (a proceedings page and the arXiv record) are not recognized.
  Title matching is not used, since short or generic titles name different sources.
- (−) The sources already mirrored twice are merged once, outside this change, by a script that keeps one copy and
  points every digest and citation at it.
