"""How a knowledge base obtains and mirrors source material.

The `Connector` and `DocumentConverter` ports, the registry that discovers a knowledge base's
declared connectors by name, the source queue and the watch list, Obsidian mirrors, and the sync
orchestration over them. Every adapter behind these ports (a bibliographic manager, a video host,
the general URL connector, and their document converters) lives outside engram, in the
`connectors` project.
"""
