---
id: studio-glossary
aliases: ["studio-glossary"]
type: glossary
app: studio
status: current
tags: []
title: "studio: knowledge-base-management glossary"
---

# studio: knowledge-base-management glossary

The vocabulary for this package is [`docs/initiatives/2026-09-02-vocabulary/design.md`](../docs/initiatives/2026-09-02-vocabulary/design.md),
sections 4.1 (Knowledge base), 4.2 (Sources) and 4.3 (Registry of knowledge bases).

Every code name in this package now says the approved word. The refresh is `kb.refresh`,
`RefreshKnowledgeBaseJob`, `POST /api/knowledge-bases/{name}/refresh` and
`GET /api/knowledge-bases/{name}/refresh-runs`, whose rows are `RefreshRun`.

The `/api` surface is one controller per route group under
`src/studio/knowledge_base/http/controllers/`, and the pairs it serves are pinned in
`tests/unit/knowledge_base/http/test_routes.py`.
