---
id: meta.ingestion-policy
aliases: [meta.ingestion-policy]
type: meta
title: Ingestion policy
kinds: []
max_age_years:
hop_depth:
per_source_cap:
per_pass_cap:
never: []
---

# Ingestion policy

Frontmatter: `kinds` names the source types this knowledge base admits (e.g. paper, article, video). `max_age_years`
caps how old an admitted source may be, or is left unset for no cap. `hop_depth` bounds how many reference
hops a digest may request beyond its own source. `per_source_cap` and `per_pass_cap` bound how many
references one source or one digest pass may request. `never` names source types this knowledge base refuses outright
(e.g. profile, homepage, social-post).

Body: one short paragraph stating what a source must do to count as admissible for this knowledge base (what a
digest must build on, compare against, or evaluate on), and naming the one or two cases that never count on
their own (a bare index, a code repository standing in for the paper it implements).

## Example (the research knowledge base)

```yaml
kinds:
  - paper
  - article
  - video
max_age_years: 2
hop_depth: 1
per_source_cap: 5
per_pass_cap: null
never:
  - profile
  - encyclopedia
  - homepage
  - social-post
  - repository
```

Take what advances retrieval and memory for coding agents. A paper counts when a digest builds on it,
compares against it, or evaluates on its dataset. Surveys older than the field's last turn do not count. A
code repository is never a source on its own, its paper is.
