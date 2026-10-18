---
title: Merge flag candidates
summary: Every group's candidate flags and defects, each list deduplicated and merged, shared themes across groups combined into one flag.
version: 2
step:
  name: merge_flags
  instructions: |-
    You are given every group worker's answer, each an object with flags and defects, each grounded in the slice of digests its worker read. Merge them into one object before the gate reviews anything.

    For flags: drop an exact duplicate, keeping the better-evidenced wording. Merge candidates that propose substantially the same thing, combining their evidence. When candidates from different groups point at one shared theme, propose the combined flag they support together. This is the pass's chance to catch a connection no single group could see. Never propose a flag no candidate supports.

    For defects: drop one that repeats another (the same defect on the same pages), keeping the better evidence. When two defects name pages of one source (a split digest's section pages carry the lead page's id as their prefix), keep one, the graver of the two in this order: remove_source, off_purpose, duplicate, empty, misquoted, mislabelled_claim, with both evidences joined. Move an item between the lists only when the gate's feedback names it, never on your own judgement.

    Keep the citation discipline: every surviving flag names the digest ids it comes from, keeps a one-sentence summary that is a whole sentence, says why it matters for this base's purpose, states one concrete proposal, and cites its evidence as wikilinks of full page ids ([[cmp.self-improving-harness]], [[paper.self-taught-optimizer-stop#^c4]] for a claim), with quotes where the exact words matter. Return one object with flags and defects, in the shape you received.
  inputs:
    candidates: {like: flag_ideas, many: true}
    collection:
      json_schema: {type: string}
  output: {like: flag_ideas}
  tools: [search, knowledge_base_profile]
---

Use after every flag_ideas group worker finishes, before the gate reviews anything. Do not use to invent a flag no candidate supports: only merge, dedupe, and combine what the groups already proposed.
