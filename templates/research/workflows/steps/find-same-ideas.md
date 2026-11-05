---
title: Find stance proposals of the same idea
summary: The run's stance proposals and the base's pending stance flags that hold the same idea, answered as groups of their ids alone, so one pass never writes two flags for one idea and a proposal that returns under a new id updates the flag that already holds it.
version: 1
step:
  name: find_same_ideas
  instructions: |-
    You hold the new stances one stance refresh proposes and the pending stance flags the research knowledge base holds. Each proposal carries stance_id, title, which states the position it takes, and reasons, why each records page behind it led to it. Each flag carries flag_id and title: a stance an earlier refresh proposed, which nobody has decided on yet.

    Find the proposals and flags that are the same idea: they take the same position on the same question, so one stance page would hold them all, however their titles word it. A position on a neighbouring question is another idea, and so is a narrower or a broader position than this one.

    Answer groups, one per idea that two or more of your ids hold, each group the list of those ids. Name a proposal by its stance_id and a flag by its flag_id, copied exactly from your input. Every group holds at least one proposal. Name each id once in your whole answer, never an id your input does not hold, and leave out every proposal and flag that matches no other. When nothing matches, answer an empty list of groups.

    The run keeps each group as one flag: the first flag the group names, or its first proposal when it names no flag. It backs and judges that flag with every records page of the group's proposals, and deletes the group's other flags, so a group of two different ideas loses one of them for good. A group that names an id twice, an id your input does not hold, or no proposal is refused whole, and its proposals stand alone.

    You read nothing and write nothing. Answer from your input alone.
  inputs:
    candidates: {like: list_candidates, element: true}
  output:
    json_schema:
      type: object
      title: SameIdeas
      required: [groups]
      properties:
        groups:
          type: array
          description: One entry per idea that two or more ids hold. Empty when nothing matches.
          items:
            type: object
            title: SameIdea
            required: [ids]
            properties:
              ids:
                type: array
                description: The stance_id of each proposal and the flag_id of each flag that holds this idea, copied exactly.
                items: {type: string}
  tier: helper
  budget: {requests: 3}
---

Use once per stance refresh, inside a map over list_candidates, which hands it one item when the run's proposals and
the base's other pending stance flags number two or more, and none otherwise, so a run with nothing to group makes no
model call. It answers groups of the ids that hold one idea, and nothing it could copy into a page. A later program
checks every id against its input, refuses a group that names an id twice, an id the input does not hold or no
proposal, keeps each group as its first flag, or its first proposal when it names none, merges the group's proposals
into it, and lists the group's other flags for removal. It reads nothing and writes nothing.
