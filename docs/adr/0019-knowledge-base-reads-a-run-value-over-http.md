---
id: adr-0019-knowledge-base-reads-a-run-value-over-http
type: adr
title: The knowledge base application reads a run's fixed value over one configured first-party HTTP route
summary: 'Studio resolves a workflow run''s artifact through the harness''s own read-only resolve route at a base URL its settings declare, imports no harness module, reads no harness store, and takes no address from a request'
status: accepted
aliases:
- adr-0019-knowledge-base-reads-a-run-value-over-http
app: root
absorbs:
- '[[spec-2026-09-13-workflow-automation]]'
created: '2026-09-27'
---

## Context

A person reads what a workflow run produced and decides to keep it as a note in a knowledge base. The value
they are keeping belongs to the run. It lives in the agent application's archived run record, addressed by a
reference the run itself minted: which run, which node, which turn, the media type, and the digest of the exact
bytes. The application that writes notes is the knowledge base application. So the note is written by one
process out of a value held by another.

Before this, the two backends never spoke. `studio` on :8100 and `harness` on :8770 each answered the SPA and
nothing else, and the root architecture master drew exactly that: no edge between them. Every cross-application
question was carried by the browser.

Four ways to get the value to the writer were open. The SPA could read it from the agent and post it to the
knowledge base. The knowledge base application could import a harness module and read the record directly. It
could open the agent's own run store. Or it could ask the agent for it.

The constraints that decided it. The digest is the whole point of the reference: a value that has been through
a third party is a value somebody could have edited, and a note whose provenance is a claim rather than a check
is worth less than no note. The two applications have independent lockfiles and separate deployments, and the
ownership rule keeps an application's domain inside it, so an import would make the run archive's internal
shape a shared type. Reading another application's store behind its back makes its storage a public contract
and means its own invalidation and retention rules do not run. And a value's address must never be something a
caller supplies, because an address in a request body is a request to fetch whatever the caller names.

## Decision

D1. **The knowledge base application asks the agent application over HTTP, at one route it does not choose.**
`studio.knowledge_base.artifact_reader.HarnessArtifactReader` posts to
`/api/runs/history/{run_id}/artifacts/resolve`, the harness's own read-only resolver. The path is fixed in the
reader. The run id in it comes from the reference, never from a caller.

D2. **The request body is the reference under one key and nothing else.** `{"ref": <ArtifactRef>}`. The route's
model forbids extra fields, so a URL cannot be smuggled alongside it, and a bare reference spread across the
body is refused. Both sides assert that literal envelope in their own tests, because two applications that
agree by convention drift on a deployment rather than in a suite.

D3. **Where to ask is configuration, never a request.** `resources.harness_artifacts` carries the base URL and
a timeout. It is a required member of `ResourcesSettings`, so a deployment that has not said which agent it
reads runs from refuses to construct rather than falling back to a guess. The address is trusted because the
owner wrote it, and it is a loopback address because this whole toolchain is one person's local machine with no
auth anywhere in it.

D4. **The answer is checked before anybody sees it.** Every body is validated as an `ArtifactDocument`, which
recomputes the digest and checks the media type and the size, and then the reference that came back must be the
one that was asked about. An answer that is a refusal, is not a document, or is about something else is a
refusal in the agent's own words. A body past the ceiling is refused while it arrives, not after.

D5. **No harness module is imported and no harness store is opened.** The knowledge base application depends on
the agent application for nothing but this one route. The agent application does not depend on the knowledge
base application at all.

## Consequences

What becomes easier. The value a note is written from is the value the run fixed, checked by digest at the
moment it is written, and the check runs inside the process that does the writing. The agent application keeps
its run archive private and can change how it stores a run without telling anybody. A person keeping a note can
edit its words freely, because the words are the draft and the reference is the warrant, and the warrant is
re-checked on every request rather than carried along in the body.

What becomes harder. The knowledge base application now has a boot-time dependency it did not have: a
deployment must declare `resources.harness_artifacts` or studio-api will not start, and the local
`application_settings.json` of anyone running the stack needs that block added. The agent application must be
up for a keep to work, and its being down is a 409 a person sees at the press. Two processes now share a wire
shape that lives in two repositories' worth of code with independent lockfiles, so the route's request model and
the reader's body are a contract that has to be held by tests on both sides rather than by a compiler.

What the repo must keep doing to honor the decision. Keep the resolve path fixed in the reader and the run id
taken from the reference. Keep the request body the reference alone. Keep the base URL in settings, required,
with no default at the resources level. Keep every answer validated and every returned reference compared
against the asked one. If a second cross-application read ever appears, give it the same shape rather than a
second one, and never let either application import the other.

## Options considered

**The SPA carries the value.** Read the artifact in the browser and post it to the knowledge base with the
keep. Rejected: the value would arrive at the writer having passed through a party that could change it, so the
digest would be checking bytes the browser chose rather than bytes the run fixed. It would also put a
cross-application contract in a third place.

**Import a harness module.** Read the archived record through the agent application's own code. Rejected: it
makes the run archive's internal shape a type two applications share, breaks the ownership rule that keeps an
application's domain inside it, and ties two independent lockfiles together for one read.

**Read the agent's run store directly.** Rejected: it makes the storage layout a public contract, and none of
the owner's own rules about what a run may still be read as would run.

**Copy the value into the knowledge base application when a run ends.** Rejected: it duplicates every artifact
whether or not anybody ever keeps one, and a copy is a second answer to what the run produced.
