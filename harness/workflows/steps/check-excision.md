---
title: Check a source's excision
summary: Reads every page an excision pass touched whole and answers approve, reject with page-naming feedback, or cannot judge, for the run.
version: 1
step:
  name: check_excision
  instructions: |
    You are handed the excision results for pages that used to reference a source being removed, and the
    collection they live in. Each result already carries the source's own id. When results is empty, the plan
    had no rewrite items to verify. Answer approve straight away, feedback "the plan had no rewrite items,
    nothing to verify", without reading anything. Otherwise read every rewritten page whole with read_notes,
    `knowledge_base` set to `collection` and `path` set to the page's path, before you decide anything.

    For each result whose status is rewritten: verify that no verbatim text from the removed source remains on
    the page, that no claim on it still cites that source, that the page still reads as one whole piece rather
    than a gap where something was cut, and that every link on it resolves. When you need to check exactly what
    the source's own words were, rather than trusting memory, call read_source with `knowledge_base` set to `collection`
    and `source_id` set to the result's own `source_id` (every result in the batch carries the same one).
    read_source is the one that answers content, not just paths and ids. Reject the whole batch when any
    rewritten page still fails one of these, and your feedback names the page by its page_id and says exactly
    what is still wrong with it.

    For a result whose status is `healed` or `deferred`, you only need to confirm its page still resolves (read_notes
    succeeds and the page is not empty) before counting it clean. It carries no rewrite to verify.

    Approve when every result checks out. Answer cannot_judge when a page you need is missing, cut, or
    unreadable, and say plainly what you saw instead.
  inputs:
    results: {like: excise_source, many: true}
    collection: {json_schema: {type: string}}
  output:
    json_schema:
      type: object
      title: Verdict
      properties:
        outcome: {enum: [approve, reject, cannot_judge]}
        feedback: {type: string}
      required: [outcome, feedback]
  tools: [read_notes, fetch, search, read_source]
  tier: orchestrator
  budget: {requests: 8, tool_calls: 12}
---

Use as the judge after a map of excise_source, with collection wired from the workflow's own parameter. The
source's own id rides on every result already, nothing else to wire. Do not use to edit a page, it only
answers.
