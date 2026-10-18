"""The library's test toolkit: one set of fakes, factories and database fixtures for every consumer.

Import submodules directly. Nothing here loads test tooling on `import engram`.

* `engram.testing.fakes.*`: hand-written `Fake*` classes implementing engram's Protocols, with
  scriptable state as public attributes.
* `engram.testing.factories.*`: polyfactory `ModelFactory` builders (needs the `testing` extra).
* `engram.testing.specs`: fixture `KnowledgeBaseSchema`s, `minimal_spec()` (two generic page types, built in
  code) and `research_spec()` (the `research` template, read by path).
* `engram.testing.knowledge_base_builder`: builds a knowledge base on disk. Golden vocabulary pages ship as
  package data, anchored by `GOLDEN_MINI_VOCABULARIES`.
* `engram.testing.infrastructure.synthetic_knowledge_base`: builds a synthetic knowledge base for freshness and eval tests.
* `engram.testing.infrastructure.knowledge_base_db`: the containerized database pytest plugin. Register it with
  ``pytest_plugins = ["engram.testing.infrastructure.knowledge_base_db"]``. It runs the checkout's alembic chain.

The runtime library never imports this subpackage.
"""
