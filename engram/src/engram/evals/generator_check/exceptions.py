"""The error a caller of the generator check handles."""


class SetupsTiedError(ValueError):
    """A dataset gives every search setup the same nDCG@10 at the precision the setups are ranked on, so it ranks none above another."""
