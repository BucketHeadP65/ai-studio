---
title: Remove a source
description: One source leaves a knowledge base, everything depending only on it removed mechanically, every page mixing it with other sources rewritten without it, the source blocked from re-ingestion, once the owner approves the traced plan.
uses: [st.trace_removal.v1, st.excise_source.v2, st.check_excision.v1, st.removal_report.v2]
workflow:
  name: source-remove
  version: 2
  description: Trace the source's reach, hold for the owner's approval, remove the only-source workflows and block re-ingestion, excise the source from every mixed page under a judge, report.
  parameters:
    collection:
      description: The knowledge base to work in, by collection name.
      json_schema: {type: string, minLength: 1}
    source:
      description: The source to remove, by its source_id, exact title, or URL.
      json_schema: {type: string, minLength: 1}
  inline:
    - name: plan_items
      source: |
        rewrites = [{"kind": "rewrite", "page_id": r["page_id"], "path": r["path"], "why": r["why"], "excise": r["excise"], "drop_link_to": r.get("drop_link_to"), "source_id": plan["source_id"], "source_title": plan.get("source_title") or "", "digest_page_id": plan.get("digest_page_id") or ""} for r in plan["rewrites"]]
        heals = [{"kind": "heal", "page_id": h["page_id"], "path": h["path"], "why": None, "excise": None, "drop_link_to": h["drop_link_to"], "source_id": plan["source_id"], "source_title": plan.get("source_title") or "", "digest_page_id": plan.get("digest_page_id") or ""} for h in plan["heals"]]
        rewrites + heals
      inputs:
        plan: {like: trace_removal}
      output:
        json_schema:
          type: array
          items:
            type: object
            title: RemovalItem
            properties:
              kind: {type: string, enum: [rewrite, heal]}
              page_id: {type: string}
              path: {type: string}
              why: {type: string}
              excise: {type: string}
              drop_link_to: {type: string}
              source_id: {type: string}
              source_title: {type: string}
              digest_page_id: {type: string}
            required: [kind, page_id, path, source_id]
    - name: remove_artifacts
      source: |
        if plan.get("nothing_found"):
            reply = "nothing to remove, no source-owned files were traced"
        else:
            reply = await remove_source_artifacts(knowledge_base=collection, source_id=plan["source_id"], expected_paths=plan["expected_paths"], reason="source removed: " + (plan.get("only_x_summary") or ""))
        reply
      inputs:
        plan: {like: trace_removal}
        judged: {like: excise_source, many: true}
        collection: {json_schema: {type: string}}
      output:
        json_schema: {type: string}
      tools: [remove_source_artifacts]
  nodes:
    - kind: run
      id: trace
      work: trace_removal
      inputs: {collection: {parameter: collection}, source: {parameter: source}}
      tier: orchestrator
    - kind: gate
      id: owner_approves
      reviews: {node: trace}
      rejected_to: trace
    - kind: run
      id: plan_items
      work: plan_items
      inputs: {plan: {node: owner_approves}}
    - kind: map
      id: excisions
      turns: [excise_source]
      over: {node: plan_items}
      each: item
      inputs: {collection: {parameter: collection}}
      max_parallel: 2
      on_error: fail
    - kind: judge
      id: check
      work: check_excision
      reviews: {node: excisions}
      inputs: {collection: {parameter: collection}}
      approved_when: outcome
      rejected_to: excisions
    - kind: run
      id: mechanical
      work: remove_artifacts
      inputs:
        plan: {node: owner_approves}
        judged: {node: check}
        collection: {parameter: collection}
    - kind: orchestrator-gate
      id: removal_confirmed
      charge: >-
        Read the removal reply. When it says nothing to remove, no source-owned files were traced, that is the
        correct, complete outcome for an untracked source or a plan that found nothing pointing at it, and no
        files should have been touched, approve it as is. Otherwise the reply should name the files removed,
        that the source's source queue row is blocked, and that the knowledge base was indexed again, with no sign of a partial or
        failed removal. Reject only when an actual removal's own reply looks wrong or incomplete, so the removal
        runs again rather than a broken reply reaching the report. Approve it otherwise, and say what the reply
        actually reported.
      reviews: {node: mechanical}
      rejected_to: mechanical
    - kind: run
      id: report
      work: removal_report
      inputs:
        plan: {node: owner_approves}
        results: {node: check}
        removal_reply: {node: removal_confirmed}
        collection: {parameter: collection}
  output: {node: report}
---

Use on demand, once per source, with `collection` and `source` both given. The owner's approval always
holds for the traced plan before anything happens: `owner_approves` reviews what `trace_removal` found,
and a rejection re-traces rather than proceeding on a stale plan. Once approved, every other page that
mixes the source with other material is rewritten or healed, one page per worker, checked whole by a
judge before anything is called done or deleted. A page the judge or the worker cannot cleanly excise
(the removed source was substantially what the page was about) is never force-rewritten. It lands
instead as a page of the knowledge base's own decision page type, in that page type's open status, naming what could not
be cleanly separated, for the owner to look at, or, in a knowledge base declaring no decision page type, as part of
this run's own report instead of a page. One item's failure fails the whole run rather than reaching
the source's own files. A rejected judge
re-runs the whole excision pass from scratch, every item on a fresh worker with no memory of the last
attempt and no feedback carried into it. Only once the judge approves are the source's own mirror and
digest files deleted and its source queue row blocked, that reply itself checked before the run reports. The
run's own values carry the full traced plan (`trace`), the judge's approved results (`check`), and the
deletion's own reply (`mechanical`), so a later reader can see exactly what was found, approved, and
done, not just the final counts.

Do not use to remove a source this base does not track: `trace_removal` reports the refusal as a plan
with nothing found, the owner still reviews it, and no page is touched. Do not use only to preview what
a removal would touch, call `trace_source` directly for that instead, this workflow's gate exists
because it is the one that actually deletes files.
