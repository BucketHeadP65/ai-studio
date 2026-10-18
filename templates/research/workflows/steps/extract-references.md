---
title: Extract and judge references
summary: The references one mirror builds on, each passed or dropped by the base's source-queue policy with its reason, and the passing ones requested.
version: 5
step:
  name: extract_references
  instructions: |
    Call source_queue_policy with the collection as `knowledge_base`. Its answer carries the never list, the source types, max_age_years
    when the base sets one, the guidance, and the purpose. Use those values, never a list named here. You already
    read the whole mirror on your first turn, work from what this conversation holds and never call read_source
    again. Name the references this source builds on, the methods it extends or compares against, the datasets
    and benchmarks it evaluates on, the one or two works it positions itself against. Never a link that merely
    appears in passing.

    Apply the rules yourself, in this order, before any judgment. First the never list from source_queue_policy's answer,
    a link whose class is on it is dropped with that class as the reason. Then the source types from source_queue_policy's
    answer, a reference whose source type is not among them is dropped with the source type as the reason. Then max_age_years,
    when source_queue_policy's answer sets one, a reference whose year is older than that many years before today is
    dropped with its year as the reason, and an unknown year passes. The authoring tools cannot check max_age_years itself,
    so this rule is your own judgment.

    Judge what is left against the guidance and the purpose. Drop what does not serve the base, with one sentence
    saying why, and give it the decision dropped_by_judgment. Never send that drop to request_source. Request each
    reference that passes and has a URL with request_source, found_via set to the source_id and reason set to one
    sentence on how this source builds on it.

    Take the decision and the reason from request_source's answer. Its first word gives the decision, requested is
    requested, seen is seen, dropped is dropped_by_rule, no_url is no_url, and already takes the value of the
    status word right after it, requested or resolved is requested, seen is seen, skipped or failed is
    dropped_by_rule. The rest of the answer is the reason. A reference with no URL is no_url, with a search hint as
    its reason, never sent to request_source.

    Return every reference you named, each with its url when it has one, why the source builds on it, its decision,
    and its reason.
  inputs:
    source: {like: find_undigested, element: true}
    collection: {json_schema: {type: string}}
  output:
    json_schema:
      type: array
      items:
        type: object
        title: Reference
        properties:
          url: {type: string}
          why: {type: string}
          decision: {type: string, enum: [requested, seen, dropped_by_rule, dropped_by_judgment, no_url]}
          reason: {type: string}
        required: [why, decision, reason]
  tools: [source_queue_policy, request_source]
---

Use as the last turn of the source worker, after digest_source and relate. The rules are the base's own, read
fresh each run from its policy page, so the owner changes what gets through by editing that page, never this
step. The authoring tools enforce the cap and the hop depth, this step's job is to name and judge. Do not use on a base
with no source layer.
