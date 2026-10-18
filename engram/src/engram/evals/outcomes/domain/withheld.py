"""The references one case withholds, how a text names one, and what the model is told in its place."""

from engram.evals.shared.contracts import BadRefError, parse_ref, qualify_ref

# The characters a reference can continue with. A withheld reference found inside a longer
# identifier is a different unit, not a leak, so the leak check requires a boundary on both sides.
_REF_CHARACTERS = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_.~:@+#/")

# The ref prefixes that name one unit, any of which reads the same unit by its id.
_UNIT_PREFIXES = ("page", "section", "unit")


class WithheldRefs:
    """The units one case withholds, matched however a result or an argument spells them.

    A knowledge base names a unit by a `page:`, `section:` or `unit:` ref, with or without its
    collection, or by a bare id resolved within the trial's own collection. Every spelling that
    resolves to a withheld unit is matched, whatever spelling withheld it, so a call cannot reach a
    withheld unit by naming it another way.
    """

    def __init__(self, collection: str, refs: frozenset[str] = frozenset()) -> None:
        """Hold the withheld references of one case, resolved within one collection.

        Args:
            collection: The knowledge base a bare id resolves within.
            refs: The withheld references, bare or collection-qualified.
        """
        self.collection = collection
        self.refs = refs
        self._units = frozenset(self.unit_of(ref) for ref in refs)
        # Free text is searched only for handle-shaped spellings. A bare id short enough to be an
        # ordinary word would match prose that names no unit at all, and the row and argument checks
        # already resolve a bare id to its unit before matching it.
        spellings = (spelling for ref in refs for spelling in (ref, *self._qualified_spellings(ref)))
        self._handles = frozenset(spelling for spelling in spellings if ":" in spelling or "/" in spelling)

    def canonical(self, ref: str) -> str:
        """`ref` in its collection-qualified form.

        Args:
            ref: A bare or collection-qualified reference.

        Returns:
            The collection-qualified reference, or `ref` unchanged when it is not a readable reference.
        """
        try:
            return qualify_ref(ref, self.collection)
        except BadRefError:
            return ref

    def unit_of(self, ref: str) -> str:
        """The unit `ref` resolves to, one value for every spelling of the same unit.

        Args:
            ref: A bare, prefixed or collection-qualified reference.

        Returns:
            `<collection>/<id>` for a page, section or unit ref or a bare id, the collection-qualified
            reference for a map or a group, or `ref` unchanged when it is not a readable reference.
        """
        try:
            resolved = parse_ref(ref, self.collection)
        except BadRefError:
            return ref
        if resolved.kind in {"collection", "group"}:
            return self.canonical(ref)
        return f"{resolved.collection}/{resolved.unit_id}"

    def holds(self, ref: str) -> bool:
        """Whether `ref` names a unit this case withholds.

        Args:
            ref: A bare, prefixed or collection-qualified reference.

        Returns:
            True when `ref` resolves to a withheld unit, whichever spelling withheld it.
        """
        return self.unit_of(ref) in self._units

    def _qualified_spellings(self, ref: str) -> tuple[str, ...]:
        """Every collection-qualified ref reading the unit `ref` resolves to, or its canonical form for a map or a group."""
        try:
            resolved = parse_ref(ref, self.collection)
        except BadRefError:
            return ()
        if resolved.kind in {"collection", "group"}:
            return (self.canonical(ref),)
        return tuple(f"{prefix}:{resolved.collection}/{resolved.unit_id}" for prefix in _UNIT_PREFIXES)

    def named_in(self, text: str) -> str | None:
        """The first withheld handle `text` names.

        A handle found inside a longer identifier is a different unit, so a match counts only when
        neither side of it continues the reference.

        Args:
            text: The text to search.

        Returns:
            The first withheld handle found, or `None` when `text` names none.
        """
        return next((handle for handle in sorted(self._handles) if _names_handle(text, handle)), None)


def _names_handle(text: str, handle: str) -> bool:
    """Whether `text` carries `handle` as a whole reference rather than inside a longer one."""
    start = text.find(handle)
    while start != -1:
        end = start + len(handle)
        opens = start == 0 or text[start - 1] not in _REF_CHARACTERS
        closes = end == len(text) or text[end] not in _REF_CHARACTERS
        if opens and closes:
            return True
        start = text.find(handle, start + 1)
    return False


def refusal_text(ref: str) -> str:
    """What the model is told when it names a withheld reference.

    Args:
        ref: The withheld reference the model named.

    Returns:
        The refusal the model reads in place of a result.
    """
    return (
        f"{ref!r} is not available in this evaluation and was not read. "
        f"Answer from the sources you can reach, or say the evidence for this question is unavailable."
    )
