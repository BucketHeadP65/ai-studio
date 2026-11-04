---
id: core-adr-0004-a-workflows-identity-leaves-default-values-out
aliases: ["core-adr-0004-a-workflows-identity-leaves-default-values-out"]
type: adr
app: core
status: accepted
tags: []
title: "0004: A workflow's identity leaves default values out"
---

# 0004: A workflow's identity leaves default values out

## Status

Accepted, 2026-11-04.

## Context

A workflow's trust is counted by its content address, `spec_identity`. Every run, approval and grade in the evidence log
carries it as `spec_hash`, and `history_for` and `trust_level` find a workflow's events by it. The address was the
SHA-256 of the workflow's JSON with every default value included.

The workflow format grows. Each time a field with a default joined a model, every saved workflow's JSON gained that
field, every identity moved, and every workflow went back to unverified although nothing its owner wrote had changed.
Four model serializers already dropped one defaulted field each from the dump to keep identities still, one field at
a time.

## Decision

A workflow's identity is the SHA-256 of its canonical JSON: the workflow dumped with `mode="json"`, keys sorted, every
field at its default left out at every depth (nested models, tuples of models and mapping values alike), and each
node's `kind` kept.

- A field added to the format with a default moves no identity.
- A field written at its default and a field left out are one identity, however the workflow was built or stored.
- A changed default value of an existing field moves no identity either, so trust carries over that change. This is
  the cost the owner accepted on 2026-11-04.
- A node keeps its `kind` although `kind` has a default, because `kind` picks which node model a document is and is
  never a value somebody chose. A new union picked by a defaulted field must keep that field in the canonical document
  the same way, or two different workflows could share one identity and so one trust. A test holds that the canonical
  document reads back as the same workflow, for every node kind and every inline step kind.

The evidence log is the only store that holds an identity. The run archive, the run logs and the library pages hold
the workflow itself and recompute its identity when they need it. The evidence log was moved forward once, when this
rule took effect, by a script kept outside the repository: every stored workflow's old identity was rewritten into its
new one, and an identity that matched no stored workflow stayed as it was.

## Consequences

- Adding a field with a default to `Workflow` or any model inside it is a free change for trust.
- A pydantic release that serialises differently, or a field added without a default, still moves every identity.
  The pinned digests in core's identity tests fail when that happens, so it is a deliberate decision that comes with a
  forward migration of the evidence log, never a silent re-key.
- The serializers that drop one defaulted field from the dump no longer matter to the identity. They still shape every
  dump that is stored or shown.

## Alternatives considered

Keeping defaults in the identity and adding a serializer per new defaulted field was rejected: it has to be remembered
for every field, and the first field anyone forgets resets every workflow's trust. Hashing only the fields a person set
was rejected because which fields were set is a property of how the object was built and does not survive a store.
Dropping `kind` with the other defaults was rejected because it would let two node kinds with the same other fields
share an identity.
