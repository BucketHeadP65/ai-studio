---
id: harness-glossary
aliases: ["harness-glossary", "studio-glossary-workflows"]
type: glossary
app: harness
status: current
tags: []
title: "harness workflow domain glossary"
---

# harness workflow domain glossary

The shared vocabulary for this package lives in `docs/initiatives/2026-09-02-vocabulary/design.md`. This package
implements the Workflows context, the workflow as data and its execution (runs, gates, schedules, spend), covered in
section 4.5, and the whole of the Sessions context, covered in section 4.6. Section 3 draws the boundary between
these two contexts and the rest of the repository.

## How it boots

`create_app(settings)` in `main.py` is the HTTP composition root. It builds one container from
`HarnessSettings` with `create_container(settings)`, puts a resolver over that container on the
application state, and mounts one controller per slice. What the container holds comes from one
`wiring.py` per slice beside the sections of the settings root and the leaves in `adapters/wiring.py`.
The lifespan resolves what it needs from the container rather than installing anything into a module,
so nothing in this package holds the process's collaborators. The ACP entry builds the same container. The
CLI builds one for the verbs that reach the workflow library, and its other verbs read the sections they need
off a settings root of their own, because a console verb is a process with no container.

## One code name that reads differently from the word

A turn's raw model messages are its transcript, and they live in the record in `aifactory_sessions`,
because the library that writes them is pydantic-ai's `StepPersistence`. Everything else in this
package spells the word the design spells.
