---
title: Report a source's removal
summary: Counts what an excision pass rewrote, healed and set aside (`deferred`) against the plan's own paths, a `deferred` page's own account carried into the summary, with no model call.
version: 2
step:
  name: removal_report
  source: |
    rewritten = len([r for r in results if r["status"] == "rewritten"])
    healed = len([r for r in results if r["status"] == "healed"])
    deferred_results = [r for r in results if r["status"] == "deferred"]
    files_gone = len(plan["expected_paths"])
    removed = plan["source_id"]
    counts = str(rewritten) + " rewritten, " + str(healed) + " healed, " + str(len(deferred_results)) + " deferred, " + str(files_gone) + " files gone"
    if plan.get("nothing_found"):
        summary = "Nothing to remove for " + removed + " in " + collection + ": " + (plan.get("only_x_summary") or "no page depended on it") + "."
    else:
        summary = "Removed " + removed + " from " + collection + ": " + counts + ". " + removal_reply
        if deferred_results:
            deferred_detail = ", ".join(r["page_id"] + ": " + r["detail"] for r in deferred_results)
            summary = summary + " Deferred: " + deferred_detail + "."
    {"removed": removed, "files_gone": files_gone, "rewritten": rewritten, "healed": healed, "deferred": len(deferred_results), "summary": summary}
  inputs:
    plan: {like: trace_removal}
    results: {like: excise_source, many: true}
    removal_reply: {like: remove_artifacts}
    collection: {json_schema: {type: string}}
  output:
    json_schema:
      type: object
      title: RemovalReport
      properties:
        removed: {type: string}
        files_gone: {type: integer}
        rewritten: {type: integer}
        healed: {type: integer}
        deferred: {type: integer}
        summary: {type: string}
      required: [removed, files_gone, rewritten, healed, deferred, summary]
---

Use as the tail of a source-removal pass, after the judge has approved the excision and the source's own files
are gone. Do not use with any other producers: its results slot is typed against excise_source, its plan slot
against trace_removal, and its removal_reply slot against remove_artifacts.
