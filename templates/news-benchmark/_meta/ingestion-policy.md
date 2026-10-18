---
id: meta.ingestion-policy
type: meta
title: Ingestion policy
source_types:
- article
max_age_years: null
hop_depth: 0
per_source_cap: 1
per_pass_cap: null
never:
- profile
- encyclopedia
- homepage
- social-post
- repository
---

# Ingestion policy

A closed set. Only the dataset's own articles belong here, and the `multihop_rag` connector brings every one of them.
A link inside an article is never fetched: every reference sits one hop from the article that names it, past a hop
depth of 0, so it is recorded as seen at most and never queued for a sync.
