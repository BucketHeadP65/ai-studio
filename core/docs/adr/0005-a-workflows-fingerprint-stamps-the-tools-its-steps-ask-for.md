---
id: core-adr-0005-a-workflows-fingerprint-stamps-the-tools-its-steps-ask-for
aliases: ["core-adr-0005-a-workflows-fingerprint-stamps-the-tools-its-steps-ask-for"]
type: adr
app: core
status: accepted
tags: []
title: "0005: A workflow's fingerprint stamps the tools its own steps ask for"
---

# 0005: A workflow's fingerprint stamps the tools its own steps ask for

## Status

Accepted, 2026-11-06.

## Context

A saved workflow carries a fingerprint, the environment it was saved against. A fetch stamps the environment again
and `drift` compares the two. What moved comes back as staleness: the agent is told to say what drifted before it
offers or runs the workflow, and the Studio shows the same lines.

The fingerprint stamped the host's tools as one digest of every tool name the registry grants. Any tool joining the
registry moved that digest, so every saved workflow reported that its tools changed, including workflows whose steps
ask for no tool at all. The step maintenance work adds one grantable tool, `check_change`, which would have made every
saved workflow stale at the next restart. The finding also named no tool, so nobody could tell whether the change
mattered to the workflow.

No decision record covered the fingerprint or drift before this one.

## Decision

The fingerprint's `grants` holds the name of each tool the workflow's inline steps ask for (`tools` on a briefing, a
program or an agent step) that the registry grants, sorted, each once.

- At save it holds every tool the steps ask for, because the validator refuses an inline step asking for a tool the
  registry does not grant.
- At fetch the current stamp is computed from the same workflow against the registry now, so a tool the registry no
  longer grants is left out of it. The recorded stamp and the current stamp are never both read off the workflow
  alone, or drift could never fire.
- `drift` reports each tool the recorded stamp holds and the current stamp lacks in a finding of its own that names
  it, so a reader can check whether the workflow still needs that tool. A tool granted now that the recorded stamp
  does not hold is not drift, the rule steps and agents already follow.
- A stamp of a workflow whose steps ask for no tool holds the empty tuple. The library drops an empty list when it
  stores a page, so the field defaults to empty and a stamp stored without it reads back as asking for none.
- No code reads the old shape. A stored stamp whose `grants` is still a digest string fails to parse, which every
  library answers as no stamp, so its staleness is unknown.

Every stored stamp was moved forward once, when this rule took effect, by a script kept outside the repository. Each
workflow page in the library, live, superseded and archived, had `fingerprint.grants` set to the tools its own
workflow's steps ask for. The workflow, its identity, the page's other keys, its body and its supersession link were
left as they were.

## Consequences

- Granting the host a new tool moves no saved workflow. Only removing a tool a workflow's step asks for makes that
  workflow stale.
- A tool removed from the host that no saved workflow's step asks for warns nobody. That is the intended cost.
- A stamp written before this rule reads as no stamp until the migration rewrites it, so the migration runs before
  the first restart on this code.
- A registered step's use of a tool is part of its code, not of a workflow, and stays covered by the stamp of its
  contract in `steps`.
- A stamp that holds nothing (no registered step, no string agent reference, no tool and no host stamp) is stored by
  the library as no stamp, because the library drops empty mappings, so it reads as unknown. The old digest was never
  empty, so this case is new. The harness adds its `promoted_capabilities` stamp to every fingerprint it writes, so
  every stamp it writes holds something. The migration met one archived page whose stamp held only the old digest and
  whose steps ask for no tool, and that page now reads as unstamped.

## Alternatives considered

Keeping the digest of every granted tool was rejected: every saved workflow went stale whenever the host gained a
tool, and a warning that fires for every workflow teaches readers to ignore staleness. Leaving the digest and letting
a save clear the warning was rejected for the same reason, because a workflow saved from chat stays stale until
somebody saves it again. Stamping the declared tools without checking them against the registry at fetch was rejected,
because both stamps would then be read off the workflow alone and could never differ.
