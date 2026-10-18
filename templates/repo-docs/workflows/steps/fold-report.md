---
title: Report a notes to analysis pass
summary: Counts how many initiatives were written and how many threads were resolved, with no model call.
version: 1
step:
  name: fold_report
  source: |
    resolved = 0
    for w in written:
        resolved = resolved + w["resolved"]
    {"initiatives": len(written), "threads_resolved": resolved}
  inputs:
    written: {like: write_analyses}
  output:
    json_schema:
      type: object
      title: FoldReport
      properties:
        initiatives: {type: integer}
        threads_resolved: {type: integer}
      required: [initiatives, threads_resolved]
---

Use as the tail of a pass that ran write_analyses over every initiative. Do not use with any other producer, its
written slot is typed against write_analyses, not against a plain list.
