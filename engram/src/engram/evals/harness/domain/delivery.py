"""Rewrite a tuned retrieval policy's instructions into the text of the files that carry them.

Both rewriters are pure and give the same result when applied twice.
`replace_managed_section` swaps the text between two sentinel lines.
`rewrite_default_instructions` rewrites the `DEFAULT_INSTRUCTIONS` assignment of `policy.py`.
"""

from __future__ import annotations

import re

# The sentinel lines around the skill's managed instructions block. Only the text between them is rewritten.
_BEGIN = "<!-- BEGIN ENGRAM TUNED INSTRUCTIONS -->"
_END = "<!-- END ENGRAM TUNED INSTRUCTIONS -->"

# The managed block from sentinel to sentinel, matched non-greedily so a second rewrite matches exactly once.
_MANAGED_BLOCK = re.compile(re.escape(_BEGIN) + r".*?" + re.escape(_END), re.DOTALL)

# The `DEFAULT_INSTRUCTIONS` assignment in `policy.py`, in either of two forms: the parenthesized
# concatenation, or the single string literal this module writes. The parenthesized form ends at a `)`
# in column 0, so a parenthesis inside a string literal never ends the match early.
_STRING_LITERAL = r'"(?:[^"\\]|\\.)*"|\'(?:[^\'\\]|\\.)*\''
_DEFAULT_INSTRUCTIONS_ASSIGN = re.compile(rf"DEFAULT_INSTRUCTIONS = (?:\(.*?^\)|{_STRING_LITERAL})", re.DOTALL | re.MULTILINE)


def replace_managed_section(text: str, content: str) -> str:
    """Replace the text between the managed block's sentinel lines in `text` with `content`.

    The sentinel lines stay, and every byte outside the block is kept. Applying it twice gives the same result.

    Args:
        text: The document carrying exactly one managed block, such as the skill markdown.
        content: The new inner content to place between the sentinel lines.

    Returns:
        The document with the managed block's content replaced.

    Raises:
        ValueError: When the sentinel lines are absent from `text`.
    """
    if not _MANAGED_BLOCK.search(text):
        msg = f"managed-section sentinels not found (expected {_BEGIN!r} ... {_END!r})"
        raise ValueError(msg)
    # A function replacement, so a backslash in `content` is written as it is and never read as a `re.sub` escape.
    block = f"{_BEGIN}\n{content}\n{_END}"
    return _MANAGED_BLOCK.sub(lambda _: block, text, count=1)


def rewrite_default_instructions(source: str, new_instructions: str) -> str:
    """Rewrite the `DEFAULT_INSTRUCTIONS` assignment in `policy.py` source to one `repr()` string literal.

    The rest of the module source is kept. Applying it twice gives the same result.

    Args:
        source: The full source text of `policy.py`.
        new_instructions: The new default-instruction string to inline.

    Returns:
        The rewritten module source.

    Raises:
        ValueError: When the `DEFAULT_INSTRUCTIONS = (...)` assignment is not present in `source`.
    """
    if not _DEFAULT_INSTRUCTIONS_ASSIGN.search(source):
        msg = "DEFAULT_INSTRUCTIONS = (...) assignment not found in the policy source"
        raise ValueError(msg)
    # A function replacement, so a backslash in the `repr()` literal is written as it is and never read as a `re.sub` escape.
    replacement = f"DEFAULT_INSTRUCTIONS = {new_instructions!r}"
    return _DEFAULT_INSTRUCTIONS_ASSIGN.sub(lambda _: replacement, source, count=1)
