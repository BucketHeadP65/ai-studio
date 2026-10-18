"""The ways a loader fails to answer a transcript, and each of them in words."""

from typing import Final


class RunEmptyError(Exception):
    """The source holds the run, but the run recorded no step, so there is no transcript to read."""


class SchemeUnknownError(ValueError):
    """No loader is registered for the scheme a source names."""


UNREADABLE: Final = (RunEmptyError, LookupError, FileNotFoundError, ValueError)
"""Every way a loader can fail to answer a transcript. The one tuple every caller catches."""

NOTHING_TO_READ: Final = (RunEmptyError, LookupError, FileNotFoundError)
"""The loader failures that mean the run holds nothing to read: no step, no such run, or no such file.

A source that is malformed, names a scheme no loader is registered for, or does not parse is not among them.
"""


def unreadable_reason(error: RunEmptyError | LookupError | FileNotFoundError | ValueError) -> str:
    """A loader failure in words.

    Args:
        error: One of the `UNREADABLE` errors a loader raised.

    Returns:
        `no steps` for an empty run, `run gone` for a missing run or file, and `unknown source` for anything else.
    """
    if isinstance(error, RunEmptyError):
        return "no steps"
    if isinstance(error, LookupError | FileNotFoundError):
        return "run gone"
    return "unknown source"
