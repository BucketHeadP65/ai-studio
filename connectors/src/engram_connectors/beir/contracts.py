"""The BEIR connector, its settings, its dataset values and the errors a caller handles."""

from engram_connectors.beir.domain.dataset import BeirDocument, JudgedQuery
from engram_connectors.beir.domain.settings import BeirSettings
from engram_connectors.beir.exceptions import BeirDatasetError, BeirSettingsError
from engram_connectors.beir.infrastructure.connector import BeirConnector

__all__ = [
    "BeirConnector",
    "BeirDatasetError",
    "BeirDocument",
    "BeirSettings",
    "BeirSettingsError",
    "JudgedQuery",
]
