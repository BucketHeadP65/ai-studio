---
id: engram-adr-0031-staleness-aware-dataset-maintenance
aliases: ["engram-adr-0031-staleness-aware-dataset-maintenance"]
type: adr
app: engram
status: accepted
tags: []
title: "ADR-0031 — Staleness-aware dataset maintenance (Vibe RAG Phase 6)"
---

# ADR-0031 — Staleness-aware dataset maintenance (Vibe RAG Phase 6)

**Status:** Accepted (2026-07-04). Designed in the vibe-rag-phase6-freshness planning session; to
be built on `feat/vibe-rag-phase6-freshness` (Vibe RAG **Phase 6**). Consumes the freshness
verdicts of [ADR-0030](0030-freshness-via-mirror-hash-invalidation.md) and refines the
[ADR-0028](0028-immutable-dataset-versions-and-case-identity.md) top-up: its retirement reasons,
its gap semantics, and its case-naming rule. Scope: `features/evals/generation/topup.py`
(`plan_top_up`), `datasets.py` (`RetirementRecord`, a stamp-chain walker), `pipeline.py` (ordinal
allocation), and the top-up handler. No new DB tables; datasets stay file artifacts.

**Context.** Detection alone leaves the loop broken: ADR-0028's top-up is staleness-blind, so a
case whose drafting basis moved survives the salted-hash ranking indefinitely — only `gold_gone`
and quota displacement retire cases. Two composition defects compound it. First, **retirement never
reopens drafting**: `_gap_cells` reads a stratum's gap as (fresh quota − the parent stamp's
*planned* quota), so with an unchanged allocation the gap is zero no matter how many cases retire —
the dataset shrinks monotonically under drift, and today's `gold_gone` already shrinks it this way
(Phase 5 consciously recorded planned-not-actual coverage; the attrition was visible only in the
coverage audit). Second, **redrafts would reuse retired names**: the top-up re-draws with the
parent's salt, so a stale seed re-enters at the same rank and its fresh draft would again be named
`{seed_key}#0`; `_merge_top_up.seen_names` tracks only survivors and the parent stamp's ledger only
records the last transition, so nothing remembers that `#0` ever existed — different QA content
would silently reoccupy a name that `wiki.eval_case_results` history and `verify-advisory` joins
treat as one case (the exact breakage ADR-0028 exists to prevent).

**Decision.** Top-up becomes staleness-aware by default, and its three seams are repaired together:

- **Stale cases retire with `reason="stale"`.** The top-up handler runs the ADR-0030 check and
  passes the era-authoritative retiring set into `plan_top_up`, which removes those cases from the
  survivor competition exactly like `gold_gone`; `RetirementRecord.reason` widens to
  `displaced | gold_gone | stale`. Precedence is `gold_gone` > `stale` > `displaced`. Default-on
  (an opt-in `--retire-stale` flag and mark-only reporting were rejected: both leave known-stale
  ground truth scoring calibrations indefinitely). Redrafts are new cases — the retired name's
  score history honestly ends.
- **Deficit cells reopen exactly the drafting retirement closed.** `plan_top_up` keeps the
  stamp-based stratum gap and adds a deficit term: per `(archetype × difficulty)` band, fresh quota
  minus survivors-after-retirement, distributed over the band's feasible cells with pool headroom.
  This also heals the pre-existing `gold_gone` shrinkage. `_merge_top_up`'s per-band caps already
  prevent over-fill, so over-drafting is bounded. Switching gaps wholly to live-count deficits
  (loses the stamp's anchor-kind granularity everywhere) and keeping planned-quota gaps (the
  redraft would never happen) were rejected.
- **Case names are historically unique via a ledger chain-walk.** The handler walks the version
  chain through `parent_version` links, unions every stamp's retired names with the parent's live
  names, and the pipeline's naming step allocates the next free ordinal per seed (retired `#0` →
  redraft becomes `#1`). A name, once used, is never reoccupied by different QA content. Recording
  cumulative used-ordinals in each stamp (duplicates what the chain encodes, desyncs under hand
  edits) and accepting reuse (corrupts run-history joins) were rejected.

**Consequences.** (+) The dataset self-heals under drift: what retires is redrafted from the
current corpus in the same version transaction, and the consistent-hashing minimal-churn property
is preserved — only stale, gone, or displaced cases move. (+) Run-history and `verify-advisory`
joins stay sound across redrafts; retired-stale, gold-gone, and displaced are distinguishable in
the ledger. (+) The `gold_gone` shrinkage defect is fixed as a by-product. (−) The chain-walk reads
one stamp file per version link — local JSON, but unbounded chains grow the read (pruning stays a
user-owned filesystem operation per ADR-0028). (−) A band whose corpus pool has genuinely thinned
cannot refill its deficit; the coverage report's thin-pool warning is the visibility. (−)
`plan_top_up` gains an ordering constraint (retirement before deficit computation) and a wider
signature — the pure-function contract absorbs it, but the inputs are now five.
