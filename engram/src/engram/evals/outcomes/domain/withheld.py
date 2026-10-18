"""The references one case withholds, how a text names one, and what the model is told in its place."""

from engram.evals.shared.contracts import BadRefError, qualify_ref

# The characters a reference can continue with. A withheld reference found inside a longer
# identifier is a different unit, not a leak, so the leak check requires a boundary on both sides.
_REF_CHARACTERS = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_.~:@+#/")


class WithheldRefs:
    """The references one case withholds, matched however a result or an argument spells them.

    A knowledge base names a unit either by its collection-qualified reference or by a bare id
    resolved within the trial's own collection. Both spellings are matched, so a call cannot reach a
    withheld unit by naming it the other way round.
    """

    def __init__(self, collection: str, refs: frozenset[str] = frozenset()) -> None:
        """Hold the withheld references of one case, resolved within one collection.

        Args:
            collection: The knowledge base a bare id resolves within.
            refs: The withheld references, bare or collection-qualified.
        """
        self.collection = collection
        self.refs = refs
        self._matched = frozenset(refs) | frozenset(self.canonical(ref) for ref in refs)
        # Free text is searched only for handle-shaped spellings. A bare id short enough to be an
        # ordinary word would match prose that names no unit at all, and the row and argument checks
        # already canonicalize a bare id before matching it.
        self._handles = frozenset(ref for ref in self._matched if ":" in ref or "/" in ref)

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

    def holds(self, ref: str) -> bool:
        """Whether `ref` names a unit this case withholds.

        Args:
            ref: A bare or collection-qualified reference.

        Returns:
            True when either spelling of `ref` is withheld.
        """
        return ref in self._matched or self.canonical(ref) in self._matched

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
