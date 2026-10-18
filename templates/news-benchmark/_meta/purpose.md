---
summary: The MultiHop-RAG benchmark's news articles stored filter-first, so an agent answers questions that span several articles with SQL, search and the graph.
---

# Purpose

## Mission
Hold the MultiHop-RAG benchmark's 609 news articles exactly as published, and digest every one of them filter-first:
its outlet, author, publication time and category as fields SQL can filter on, its claims with the date each is about,
and links to one entity page per thing it names. An agent then answers a question that spans several articles with
SQL, search and the graph, and the benchmark's questions score the answers.

## In scope
Every article of the dataset, mirrored one per source, one digest page per article, and one entity page per person,
organization, product, event, place or work an article names. Every action, statement or measurement an article
reports bears on this purpose, whatever its subject, so no article is skipped for its subject.

## Out of scope
Anything outside the dataset: no other article, no page a link inside an article points to, and no edit to a mirrored
article.

## Consumers
The evals agent, which answers the benchmark's questions from this knowledge base alone.
