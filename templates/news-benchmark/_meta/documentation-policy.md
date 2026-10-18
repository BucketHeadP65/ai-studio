---
id: meta.documentation-policy
aliases: [meta.documentation-policy]
type: meta
title: Documentation policy
---
# Documentation policy

## digest
One digest per article, written by the digest workflow from the article's mirror. The outlet, author, published_at,
published_on, category and url fields come filled from the source metadata. Keep each value exactly as given.

### TL;DR
Who did what and when, in two sentences, as the article reports it.

### Findings
One claim per action, statement or measurement the article reports, each with a quote copied verbatim from the
article. Fill `when` with the date the claim is about, as `YYYY-MM-DD` or `YYYY-MM`, whenever the article states one.
Fill `figure` with the number and its unit for a measurement. The link workflow, run after the digest run,
links each claim's subject and each statement's speaker to an entity page.

## entity
One page per named person, organization, product, event, place or work. Only resolve_entities writes these pages,
so never write one by hand.

### TL;DR
What the entity is, in one sentence.
