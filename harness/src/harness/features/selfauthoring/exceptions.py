"""The errors the self-authoring slice raises, each carrying its reason in the message."""


class StepValidationError(Exception):
    """A custom step's code cannot be imported, or registers no public async function."""


class AgentValidationError(Exception):
    """Custom agent YAML that will not become a usable document.

    Raised when the text does not parse to a mapping, fails `AgentSpec` validation, or names a
    capability the registry does not know.
    """


class ProfileValidationError(Exception):
    """A profile document that is not valid, with the reason."""


class SelectionError(Exception):
    """A selection that cannot be honored, with the reason. It is refused whole, never half-applied."""


class PromotionError(Exception):
    """A custom capability cannot be promoted or demoted, or a promoted name cannot be resolved."""
