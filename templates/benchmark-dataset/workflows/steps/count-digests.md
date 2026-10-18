---
title: Count a digest pass
summary: Counts the digests a pass wrote and the documents it skipped, with no model call.
version: 2
step:
  name: count_digests
  source: |
    written = [d for d in digests if d["status"] == "written"]
    skipped = [d for d in digests if d["status"] == "skipped"]
    {"written": len(written), "skipped": len(skipped)}
  inputs:
    digests: {like: digest_source, many: true}
  output:
    json_schema:
      type: object
      title: DigestCount
      properties:
        written: {type: integer}
        skipped: {type: integer}
      required: [written, skipped]
---

Use as the tail of a pass that mapped digest_source alone over the undigested documents. Its `digests` slot is
typed against digest_source, so wire it from that turn's collected outputs. Do not use after a pass that also relates
digests or extracts references. That pass needs a report that counts links and reference decisions too. The reason
for each skipped document stays in the run's values under the map node. This step only counts them. A document whose
turn fails is not among the digests, so it is left out of both counts. It stays undigested, and the next run picks it up.
