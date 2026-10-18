---
title: Finish a pass
summary: The owner notification as the pass's one result.
version: 2
step:
  name: finish
  source: |
    {"notified": notified}
  inputs:
    notified: {like: notify_owner}
  output:
    json_schema:
      type: object
      title: FinishResult
      properties:
        notified: {type: string}
      required: [notified]
---

Use as the tail of a workflow whose last act is notify_owner. Do not use anywhere notify_owner has not run: its slot
is typed against it by identity.
