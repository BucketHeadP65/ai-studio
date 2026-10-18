"""The public surface of the progressive-disclosure protocol."""

from core.ai.disclosure.capability import ProgressiveDisclosure
from core.ai.disclosure.domain import ChildSummary, DisclosureEnvelope, DisclosureMode, DisclosureStatus, approx_tokens
from core.ai.disclosure.result_disclosure import Binding, Disclosable, ResultDisclosure

__all__ = [
    "Binding",
    "ChildSummary",
    "Disclosable",
    "DisclosureEnvelope",
    "DisclosureMode",
    "DisclosureStatus",
    "ProgressiveDisclosure",
    "ResultDisclosure",
    "approx_tokens",
]
