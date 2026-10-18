---
title: Report a digest pass
summary: Counts written, skipped, and each reference decision from a digest pass, with no model call.
version: 3
step:
  name: report
  source: |
    written = [d for d in digests if d["status"] == "written"]
    skipped = [d for d in digests if d["status"] == "skipped"]
    flat = [r for refs in references for r in refs]
    decisions = [r.get("decision", "") for r in flat]
    {
        "written": len(written),
        "skipped": len(skipped),
        "requested": len([d for d in decisions if d == "requested"]),
        "seen": len([d for d in decisions if d == "seen"]),
        "dropped_by_rule": len([d for d in decisions if d == "dropped_by_rule"]),
        "dropped_by_judgment": len([d for d in decisions if d == "dropped_by_judgment"]),
        "no_url": len([d for d in decisions if d == "no_url"]),
        "edges_added": sum(d.get("edges_added", 0) for d in digests),
    }
  inputs:
    digests: {like: relate, many: true}
    references: {like: extract_references, many: true}
  output:
    json_schema:
      type: object
      title: DigestReport
      properties:
        written: {type: integer}
        skipped: {type: integer}
        requested: {type: integer}
        seen: {type: integer}
        dropped_by_rule: {type: integer}
        dropped_by_judgment: {type: integer}
        no_url: {type: integer}
        edges_added: {type: integer}
      required: [written, skipped, requested, seen, dropped_by_rule, dropped_by_judgment, no_url, edges_added]
---

Use as the tail of a pass that mapped digest_source (chained into relate) and extract_references over the same list.
Do not use with any other producers: its `digests` slot is typed against relate, not digest_source, and its
`references` slot against extract_references. The per-reference reasons stay in the run's values under the
references node, this step only counts them.
