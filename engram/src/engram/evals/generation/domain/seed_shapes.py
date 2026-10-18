"""The structural shape thresholds a seed must meet for each archetype."""

MIN_SURVEY_FANOUT = 3
"""The fewest edges of one relation that must point into a page for it to seed a `survey`."""

MIN_SYNTHESIS_FANOUT = 2
"""The fewest distinct pages a page must reach over one relation to seed a `synthesis`."""

TRACE_HOPS = 2
"""The number of hops a `trace` path spans. A `src -> mid -> dst` chain is two hops."""

MIN_PASSAGE_CHARS = 400
"""The fewest characters a section's body must have to seed a `passage`."""
