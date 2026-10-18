---
title: Promote executed designs
description: >-
  Turns each executed design that no ADR absorbs yet into a proposed root ADR, numbered next and naming the design
  it absorbs, for the owner to accept.
uses: [st.pending_designs.v1, st.draft_adr.v3, st.check_adrs.v1, st.write_adrs.v3, st.finish.v2]
presentation:
  purpose: >-
    Keep the decision record in step with the work. Once a design has been executed it holds decisions the
    repository now lives by, and until an ADR absorbs them they are findable only inside the initiative that
    made them.
  produces: >-
    One proposed ADR per executed design, numbered past the highest root ADR on file, carrying Context, Decision
    and Consequences sections and a link to the design it absorbs. The designs themselves are left where they are.
  owner_participation: >-
    You pick the knowledge base and start the run. A judge, not you, reads the drafts whole and rejects the ones
    that restate a design without naming a decision. Accepting each ADR afterwards is yours, by changing its
    status from proposed, and a draft the judge cannot judge waits for you rather than landing.
  not_for: >-
    Not for editing an ADR that is already accepted. Not for a design still being written, since only a design at
    status executed is picked up. Not for an app's own ADR chain, which carries its own prefix rather than the
    root numbering.
  inputs:
    - {key: collection, label: Knowledge base, control: knowledge-base}
  result_fields:
    - {label: ADRs written (recorded), path: values.write_adrs}
  actions:
    - kind: record_owner_review
      id: adrs_read
      label: Record that you have read the ADRs
      artifact_ids: [proposed_adrs]
workflow:
  name: docs-promote-initiative
  version: 6
  description: List the executed designs without an ADR, draft one ADR per design, gate, write them as proposed, notify.
  parameters:
    collection:
      description: The knowledge base to work in, by collection name.
      json_schema: {type: string, minLength: 1}
  result:
    summary: One proposed ADR per executed design, waiting for the owner to accept it.
    primary_work: draft_adrs
    empty_primary: noop
    required_gates:
      - {gate_id: check_adrs, accepted_sources: [judge]}
    artifacts:
      - id: proposed_adrs
        label: The ADR drafts the judge approved
        source: {node: check_adrs}
        media_type: application/json
  nodes:
    - kind: run
      id: list_pending
      work: pending_designs
      inputs: {collection: {parameter: collection}}
    - kind: map
      id: draft_adrs
      turns: [draft_adr]
      over: {node: list_pending}
      each: design
      inputs: {collection: {parameter: collection}}
      max_parallel: 2
      on_error: skip
      tier: orchestrator
    - kind: judge
      id: check_adrs
      work: check_adrs
      reviews: {node: draft_adrs}
      inputs: {collection: {parameter: collection}}
      approved_when: outcome
      rejected_to: draft_adrs
    - kind: run
      id: write_adrs
      work: write_adrs
      inputs:
        drafts: {node: check_adrs}
        collection: {parameter: collection}
    - kind: run
      id: notify
      work: notify_owner
      inputs:
        collection: {parameter: collection}
        written: {node: write_adrs}
    - kind: run
      id: finish
      work: finish
      inputs:
        notified: {node: notify}
  output: {node: finish}
---

Use on demand, with `collection` set to the repo knowledge base, after an initiative's design reaches status executed.

The stages: `list_pending` takes the executed designs the base's promote-initiative policy entry admits. `draft_adrs`
runs one agent step per design on the orchestrator tier, two at a time. `check_adrs` is the judge, on the
orchestrator tier too, reading every draft whole before it decides, and a rejection sends the drafting round again. A
cannot judge verdict leaves the run waiting for the owner. `write_adrs` numbers each approved draft past the highest
root ADR on file and writes it as proposed, then `notify` tells the owner and `finish` closes the run. Every ADR
lands as proposed, the owner accepts it by changing its status. Do not use on a host whose steps lack `notify_owner`.
