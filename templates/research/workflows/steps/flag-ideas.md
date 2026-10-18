---
title: Flag ideas
summary: What the owner should see from one group of new digests, and the defects in the base's own pages, grounded in the base's purpose and the owner's past decisions, as proposals and defects with evidence.
version: 6
step:
  name: flag_ideas
  instructions: |-
    Start by calling knowledge_base_profile for this base. It gives you the base's purpose, its vocabulary, and every flag page on file with its status.

    You are given one group of new digests: an object with a group_id and a members list, where each member is a digest {digest_id, page_type, title, source_id, path, findings}. The members are one slice of this pass's batch. Other workers read the other slices in parallel, so you do not see the whole batch. Do not propose flags on their behalf. Read your group's members together with what the base already holds (search it for each theme you notice).

    For every candidate flag, run two checks before you propose it. First, a novelty check: does a standing page already say this? If so, drop it. Second, a contradiction check: does one of your group's digests disagree with something the base already holds as a claim? If so, that disagreement is itself a flag, and you cite both sides, the digest and the standing claim. Never propose anything the flag history already shows as dismissed.

    The base's stance pages are what the system it serves does, one topic each, with a status that is its stance. For each
    theme you notice, search the stance pages (search with the theme's words, and read every hit whose id starts with
    stance.). A digest that shows a stance's technique working elsewhere, disputes it, or suggests a variant to try is a
    flag about that stance: name the stance page in the proposal and say whether it should move to trying, adopted or
    rejected, or gain the disputing citation. A theme no stance covers is still a flag, and its proposal says so.

    Some of what you notice is not about the subject but about our own pages. A defect is a page this pipeline wrote that misstates its own source or falls outside the base's purpose, fixed by changing that page alone. It is one of: off_purpose (no finding of the digest bears on the base's purpose), misquoted (a claim's quote is not the source's words, such as a quote holding only a section marker like `§7`), mislabelled_claim (a claim's `kind` misstates who makes the claim), empty (the page says nothing its source says), duplicate (a digest of another source already covers the same work), remove_source (the source itself should leave the base, which only the owner may do). Return each defect in defects, never as a flag: its page_ids (ids of your group's members only), the one defect, and its evidence in one or two sentences. A member's findings are defects the index already found, so do not repeat them, and still read that member's content for flags. A proposal that would only edit our own pages is a defect. A flag that cites a defective page stays a flag, since the page's content is still evidence.

    Propose flags the owner should see: an idea to try, a feature we could adopt, a risk to our approach (never a defect in one of our pages), or a question worth answering. Each flag names the digest ids it comes from, carries a one-sentence summary the owner can read in a listing (a whole sentence, never a cut-off prefix of the why), says why it matters for this base's purpose, states one concrete proposal, and cites its evidence as wikilinks, with a short quote where the exact words matter. Every page or claim you mention anywhere in a flag (why, proposal or evidence) is written as a wikilink of its full page id: [[cmp.self-improving-harness]], and a claim as [[paper.self-taught-optimizer-stop#^c4]]. A bare id outside brackets is dead text in the owner's knowledge base. A later merge pass deduplicates and combines candidates across groups, so propose everything your group genuinely supports. Fewer strong flags still beat many weak ones, but never suppress a candidate only because it might overlap another group's. Return one object with flags and defects, either list possibly empty.
  inputs:
    group: {like: recent_digests, element: true}
    collection:
      json_schema: {type: string}
  output:
    json_schema:
      type: object
      title: FlagsAndDefects
      required: [flags, defects]
      properties:
        flags:
          type: array
          items:
            type: object
            title: Flag
            required: [title, summary, flag_kind, about, why, proposal]
            properties:
              why: {type: string}
              about:
                type: array
                items: {type: string}
              title: {type: string}
              summary: {type: string}
              evidence: {type: string}
              proposal: {type: string}
              flag_kind:
                enum: [idea, feature, risk, question]
                type: string
        defects:
          type: array
          items:
            type: object
            title: Defect
            required: [page_ids, defect, evidence]
            properties:
              page_ids:
                type: array
                items: {type: string}
              defect:
                enum: [off_purpose, misquoted, mislabelled_claim, empty, duplicate, remove_source]
                type: string
              evidence: {type: string}
  tools: [search, fetch, knowledge_base_profile]
---

Use after recent_digests, once per group, in parallel with every other group's worker (never on one digest alone or the whole batch: the value is in what one group says together). A later merge_flags pass deduplicates and combines candidates across every group, so propose everything this group's digests genuinely support.
