---
id: studio-adr-0003-studio-wiring-and-controllers
aliases: ["studio-adr-0003-studio-wiring-and-controllers"]
type: adr
app: studio
status: accepted
tags: []
title: "0003: Declared settings, listed registrations, class-based controllers"
---

# 0003: Declared settings, listed registrations, class-based controllers

**Status:** Accepted (2026-09-06).

**Context.** The knowledge base application ran on a container from the start, but three habits around it
had drifted from what the shape is for. Settings sections carried default factories, so a section
that was never configured still resolved, and a missing one was invisible until something read a
zero value. The active knowledge base was a settings field that only an init kwarg could fill, with
a custom settings source refusing it from the file and the environment, which is a lot of machinery
to say that a knowledge base is not configuration. Routes were closures over the application object
that pulled collaborators out of a container stashed on the application state, one resolve per
request, so a route's dependencies appeared nowhere in its signature and the only way to test one
was to hand the application a stand-in container.

**Options considered.**

- **Keep the closures and inject through `Depends`.** Rejected: it spreads a second resolution
  mechanism beside the container and still leaves the route bodies reaching for the application.
- **Keep the section defaults and document which ones matter.** Rejected: prose does not fail a
  boot. A section with a default is a section nobody has to declare, and the first sign of a missing
  one is behaviour rather than a message.
- **Keep the factories-only rule.** Rejected for the cases where the class we register is our own
  and its constructor already takes exactly what the factory injected. The factory is then a second
  spelling of the constructor, and the two drift.

**Decision.**

1. Every section of `ApplicationSettings` and `ResourcesSettings` is declared. No default factory,
   no optional section. A settings file that leaves one out refuses construction naming it. A scalar
   field may keep a default.
2. The knowledge base a process acts on is an argument of `create_container`, not a settings field.
   It selects the container variant. With one, `KnowledgeBaseSettings` resolves and the bus routes
   every command. With none, resolving it refuses and the bus carries the registry alone.
3. The container's sections are derived from the settings tree rather than seeded by hand. Every
   model-typed field is registered under its own type, recursively.
4. Each slice lists what it registers in its own `wiring/<slice>.py` as `REGISTRATIONS`. A class this
   package defines is registered as itself under its port when its constructor takes exactly what a
   factory would inject. A factory function stays where a class cannot serve: the type is one we do
   not define, or the object is assembled from values the container does not register. This amends
   the factories-only rule of engram ADR-0038 for this package, and a gate pins the new rule,
   including that a registered class's constructor annotations resolve at module scope.
5. No registration declares a lifetime. Singleton is the default, and a declared lifetime is either
   noise or a non-singleton, so a file-level gate refuses the string outright.
6. Every route group is a controller class with its own router, its own prefix and
   `route_class=WireupRoute`, mounted through the container's FastAPI integration. A controller takes
   the settings it always needs through its constructor and anything that reaches the database as an
   injected argument of the method. A group serving two path families keeps the bare `/api` prefix.
7. The served `(method, path)` set is a baseline in `tests/unit/knowledge_base/http/test_routes.py`.
   The client is built against those exact pairs, so a moved route is a break rather than a
   refactor, and a duplicate pair is refused.

**Consequences.**
- (+) A process that boots has every section, and one that does not says which is missing.
- (+) A route's dependencies are its signature. A test overrides a leaf on the real container instead
  of standing in for the container.
- (+) The application owns one container per call to `create_app`, so two applications never share
  state.
- (-) The container's FastAPI integration owns the container's close, so the application disposes its
  engines and leaves the rest to it.
- (-) Building the container is part of building the application, so a construction failure surfaces
  at `create_app` rather than at the first request. That is the intent.
- (-) A test's container is a real one, so a registration it never overrides can be resolved for real.
  Every database leaf is overridden explicitly, and the gate that sweeps them is the guard.

**References.** engram ADR-0038 (the DI conventions this amends), engram ADR-0025 F12/F36 (wrapper
types rather than qualifiers), root ADR-0018 (a knowledge base's vocabulary lives in its template).
