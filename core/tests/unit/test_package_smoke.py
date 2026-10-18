import importlib
import subprocess
import sys


def test_core_ai_surface_is_importable():
    module = importlib.import_module("core.ai")

    assert module.__name__ == "core.ai"
    assert module.__all__ == [
        "create_azure_model",
        "create_azure_openai_client",
        "create_embedding_model",
        "create_gateway_model",
        "create_model",
    ]


def test_the_ai_package_pulls_in_neither_the_workflow_engine_nor_its_artifacts():
    """`core.ai` is model and embedding factories. Every heavier surface is reached by submodule import."""
    probe = "import core.ai, sys; print(sorted(m for m in sys.modules if m.startswith('core.ai.')))"

    answered = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True, check=True)  # noqa: S603

    assert "core.ai.workflows" not in answered.stdout
    assert "core.ai.artifacts" not in answered.stdout


def test_the_artifact_surface_is_importable_on_its_own():
    module = importlib.import_module("core.ai.artifacts")

    assert module.ArtifactRef.model_fields.keys() == {"id", "label", "run_id", "node_id", "turn", "value_digest", "media_type"}
