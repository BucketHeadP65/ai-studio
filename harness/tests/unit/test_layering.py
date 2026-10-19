"""The layer rules, enforced: dependencies point inward, and every sanctioned exception is pinned.

The tiers, inward first: `domain/` (ports and contract vocabulary, imports nothing of ours),
`settings` (geometry and knobs, imports domain only), the kernel (`session`, `plugins`, `knowledge_base`,
`rendering`, `sandbox`, `tracing`, `workspace` -- the shared surface every slice may hold), `adapters/`
(third-party construction: databases, the settings file, embeddings, models, the step environment),
`features/<slice>` (a slice may reach inward and sideways into its own slice, never into another
slice or outward), `composition` (the one module that knows every slice, the container,
and every `wiring` module -- a wiring module says where a thing comes from, which is composition
wherever it sits), and the entries (`main`, `acp`, `cli`). `vendor/` is third-party source and exempt.

One rule stands beside the tiers: a slice's `ports` or `contracts` module is its published surface, so another
slice's `controller` or `wiring` may declare it (a controller takes a peer's port instead of its
concrete class, which is what lets a test hand it a fake). Everything else a slice holds is private
to it.

Exceptions are pinned EXACTLY: each one is asserted to still exist, so fixing one forces its
removal from the list -- the list can only shrink, never quietly grow or go stale.
"""

from __future__ import annotations

import ast

from harness import settings as _settings

SRC = _settings.ROOT

KERNEL = frozenset({"session", "plugins", "knowledge_base", "rendering", "sandbox", "tracing", "workspace"})
COMPOSITION = frozenset({"composition", "container"})
ENTRIES = frozenset({"main", "acp", "cli"})

SANCTIONED: frozenset[tuple[str, str]] = frozenset(
    {
        # The session state bag holds each slice's on-screen state; inverting it means threading
        # four payload types through every route. Pinned, not hidden.
        ("session", "features.authoring.contracts"),
        ("session", "features.running.contracts"),
        ("session", "features.selfauthoring.contracts"),
        # The environment stamp digests the promoted custom capabilities: the fingerprint must see a
        # source change under an unchanged name. The custom capabilities are slice-owned, so the stamp
        # reaches up: one sanctioned upward edge, imported lazily at call time.
        ("adapters.model_tiers", "features.selfauthoring.contracts"),
        # The authoring agent HOSTS the self-authoring toolsets and the skills-listing plugin: the one
        # place the two slices deliberately meet outside composition. Hosting them means handing them
        # the log they record through, so the agent's own authoring acts and the owner's
        # `harness activate` land on one timeline.
        ("features.authoring.agent", "features.selfauthoring.contracts"),
        # The authoring agent HOSTS the follow-me plugin and its approval gate, the same reach it
        # has into self-authoring.
        ("features.authoring.agent", "features.followme.contracts"),
        # The chat service decides which pending call a browser answer is allowed to fill, and the
        # follow-me contracts publish those names. The same reach the agent build has above.
        ("features.authoring.service", "features.followme.contracts"),
        # The authoring agent mounts the friction voice beside its other plugins, and the sessions
        # slice owns it because it owns the flags table the flag lands on. The agent build also reads
        # the project's context policy and skill directories from the sessions slice's config loader.
        ("features.authoring.agent", "features.sessions.contracts"),
        # The skills-listing plugin reads the same catalog the projects station's skills screen
        # does, and the project config that catalog is built over.
        ("features.selfauthoring.skills_plugin", "features.projects.contracts"),
        ("features.selfauthoring.skills_plugin", "features.sessions.contracts"),
        # The ticker fires a schedule as exactly the run the Run button starts: launch, phase,
        # and the one-run-per-session refusal are the running slice's own meanings, and a second
        # copy in the scheduling slice would drift. One sanctioned sideways edge.
        ("features.scheduling.service", "features.running"),
        # A fire opens its runner session in the pool the sessions slice publishes as a port.
        ("features.scheduling.service", "features.sessions.contracts"),
        # A schedule's own fire writes the same records a Run-button launch does, handed in whole.
        ("features.scheduling.tools", "features.running.contracts"),
        # The authoring agent mounts NoteOverlay beside the files plugin, and Preferences beside it: the one
        # place the agent's own capability list meets a project-scoped feature.
        ("features.authoring.agent", "features.projects.contracts"),
        # The skill hint offers the skills the agent's own skills mount offers: the same catalog, the same
        # selected profile, and the project's skill directories.
        ("features.authoring.skill_hint", "features.projects.contracts"),
        ("features.authoring.skill_hint", "features.selfauthoring.contracts"),
        ("features.authoring.skill_hint", "features.sessions.contracts"),
        # The chat-side workflow process replays a run through the running slice's tool.
        ("features.authoring.process", "features.running.contracts"),
        # The chat-side workflow process hosts the scheduling agent tools beside its other plugins,
        # the same way it hosts run_workflow and check_runs.
        ("features.authoring.process", "features.scheduling.contracts"),
        # The chat-side workflow process holds every record a run writes as a capability field,
        # typed against the running slice's own shapes rather than duplicating them.
        # The chat-side workflow process holds the telemetry reporter as a capability field and
        # hosts spend_report beside run_workflow and check_runs, the same reach as the two edges above.
        # The kernel reads the tier store, the change channel and the activity context off the
        # adapters; no other kernel module gets the same reach.
        ("session", "adapters.contracts"),
        # A workspace's session log is a real `SessionLog`, not the protocol -- the one
        # adapter a project's own state home is built from.
        ("workspace", "adapters.contracts"),
        # The late seam a tool reaches the pool through: an adapter over the resolver, answering
        # the kernel's own pool type.
        ("adapters.sessions", "session"),
    }
)


PACKAGES = ("domain", "adapters", "features", "vendor")
"""Tiers that are a package: the module itself, or anything under it, belongs to that tier."""


def _tier(module: str) -> str:
    """Classify one module (path relative to the package, dot-form) into a named tier."""
    if module in {"", "__init__"}:
        return "package"
    if module.endswith(".wiring"):
        return "composition"
    for package in PACKAGES:
        if module == package or module.startswith(f"{package}."):
            return package
    if module == "settings":
        return "settings"
    if module in KERNEL:
        return "kernel"
    if module in COMPOSITION:
        return "composition"
    if module in ENTRIES:
        return "entries"
    return "UNCLASSIFIED"


def _slice_of(module: str) -> str | None:
    parts = module.split(".")
    return parts[1] if module.startswith("features.") and len(parts) >= 2 else None


COMPOSING = frozenset({"controller", "wiring"})
"""The modules that name their collaborators: a controller's constructor, a slice's registrations."""


def _published_surface(source: str, target: str) -> bool:
    """Whether `source` is a composing module holding another slice's published port surface.

    A slice's `ports` or `contracts` module is what it publishes for another slice to declare -- a controller
    takes a peer's port rather than its concrete class, which is what lets a test hand it a fake.
    Everything else a slice holds stays private to it, exception list or nothing.
    """
    return target.endswith((".ports", ".contracts")) and source.rsplit(".", maxsplit=1)[-1] in COMPOSING


REACHES = {
    "package": set[str](),
    "domain": {"domain"},
    "settings": {"domain"},
    "kernel": {"domain", "settings", "kernel"},
    "adapters": {"domain", "settings", "adapters", "vendor"},
    "features": {"domain", "settings", "kernel", "adapters", "vendor", "features"},
    "composition": {"domain", "settings", "kernel", "adapters", "vendor", "features", "composition"},
    "entries": {"domain", "settings", "kernel", "adapters", "vendor", "features", "composition"},
    "vendor": {"vendor"},
}
"""Which tiers each tier may reach, inward only. The rule the whole gate rests on."""


def _allowed_by_tier(source: str, target: str) -> bool:
    """Whether the tiers alone allow this edge, with no exception consulted."""
    src_tier, dst_tier = _tier(source), _tier(target)
    if dst_tier == "package":
        return True
    if dst_tier not in REACHES[src_tier]:
        return False
    if src_tier == "features" and dst_tier == "features":
        return _slice_of(source) == _slice_of(target) or _published_surface(source, target)
    return True


def _edge_allowed(source: str, target: str) -> bool:
    return (source, target) in SANCTIONED or _allowed_by_tier(source, target)


def _edges() -> set[tuple[str, str]]:
    """Every intra-package import edge, function-local (lazy) imports included."""
    found: set[tuple[str, str]] = set()
    for py in sorted(SRC.rglob("*.py")):
        rel = py.relative_to(SRC).with_suffix("")
        parts = [p for p in rel.parts if p != "__init__"]
        source = ".".join(parts)
        if _tier(source) == "vendor":
            continue
        for node in ast.walk(ast.parse(py.read_text(encoding="utf-8"))):
            if isinstance(node, ast.ImportFrom) and node.module:
                if node.module == "harness":
                    found.update((source, alias.name) for alias in node.names)
                elif node.module.startswith("harness."):
                    found.add((source, node.module.removeprefix("harness.")))
            elif isinstance(node, ast.Import):
                found.update(
                    (source, alias.name.removeprefix("harness.")) for alias in node.names if alias.name.startswith("harness.")
                )
    return found


def test_every_module_belongs_to_a_named_tier() -> None:
    unclassified = sorted(
        ".".join(p for p in py.relative_to(SRC).with_suffix("").parts if p != "__init__")
        for py in SRC.rglob("*.py")
        if _tier(".".join(p for p in py.relative_to(SRC).with_suffix("").parts if p != "__init__")) == "UNCLASSIFIED"
    )
    assert unclassified == [], f"modules outside every named tier: {unclassified}"


def test_every_import_edge_points_inward() -> None:
    violations = sorted(f"{source} -> {target}" for source, target in _edges() if not _edge_allowed(source, target))
    assert violations == [], "layer violations (fix the import, or -- deliberately -- pin it in SANCTIONED):\n" + "\n".join(violations)


def test_no_module_imports_an_entry_or_composition_from_below() -> None:
    upward = sorted(
        f"{source} -> {target}"
        for source, target in _edges()
        if _tier(target) == "entries" or (_tier(target) == "composition" and _tier(source) not in {"entries", "composition"})
    )
    assert upward == [], "nothing below the entries may import them:\n" + "\n".join(upward)


def test_the_sanctioned_exceptions_still_exist() -> None:
    """A fixed exception must leave the list -- SANCTIONED can only shrink deliberately."""
    edges = _edges()
    stale = sorted(f"{source} -> {target}" for source, target in SANCTIONED if (source, target) not in edges)
    assert stale == [], "sanctioned edges no longer present -- remove them from SANCTIONED:\n" + "\n".join(stale)


def test_no_sanctioned_exception_is_one_the_tiers_already_allow() -> None:
    """An exception the rule itself permits is not an exception, and reads as a violation that was waived."""
    redundant = sorted(f"{source} -> {target}" for source, target in SANCTIONED if _allowed_by_tier(source, target))
    assert redundant == [], "these edges need no exception -- remove them from SANCTIONED:\n" + "\n".join(redundant)


def test_no_controller_knows_another_slice() -> None:
    """A controller reaches its own slice and inward, never sideways, unless the edge is pinned.

    `SANCTIONED` is consulted the same way `_edge_allowed` consults it, so an edge a slice already
    holds by name keeps its exception when its routes become a controller. A peer slice's `ports` or
    `contracts` module is the one thing it may hold unpinned, for the reason `_published_surface` states.
    """
    offenders = sorted(
        f"{source} -> {target}"
        for source, target in _edges()
        if source.endswith(".controller")
        and _tier(target) == "features"
        and _slice_of(source) != _slice_of(target)
        and (source, target) not in SANCTIONED
        and not _published_surface(source, target)
    )
    assert offenders == [], "a controller reached into another slice:\n" + "\n".join(offenders)


def test_a_controller_holds_a_peer_slices_port_and_nothing_else_of_it() -> None:
    """The published-surface clause, stated as the rule rather than as a list of pairs."""
    assert _edge_allowed("features.authoring.controller", "features.sessions.ports")
    assert _edge_allowed("features.authoring.wiring", "features.sessions.ports")
    assert not _edge_allowed("features.authoring.controller", "features.running.controller")
    assert not _edge_allowed("features.authoring.controller", "features.running.service")
    assert not _edge_allowed("features.authoring.agent", "features.sessions.ports"), "a port is declared where collaborators are named"
