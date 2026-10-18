"""The snapshot keeps names and shapes and drops the texts docstrings feed."""

import sys
import unittest

from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import snapshot_external_names  # noqa: E402


class WithoutDescriptions(unittest.TestCase):
    """Descriptive texts go, names and shapes stay."""

    def test_description_and_summary_texts_are_removed_at_any_depth(self) -> None:
        """Both annotations disappear wherever they sit."""
        document = {"summary": "List items", "paths": {"/items": {"get": {"description": "Every item.", "operationId": "list_items"}}}}
        self.assertEqual(
            snapshot_external_names.without_descriptions(document), {"paths": {"/items": {"get": {"operationId": "list_items"}}}}
        )

    def test_a_property_named_description_stays(self) -> None:
        """A schema field that happens to be called description is a name, not a text."""
        schema = {"properties": {"description": {"type": "string", "description": "What the item is."}}, "required": ["description"]}
        self.assertEqual(
            snapshot_external_names.without_descriptions(schema),
            {"properties": {"description": {"type": "string"}}, "required": ["description"]},
        )


if __name__ == "__main__":
    unittest.main()
