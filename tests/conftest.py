import pytest

from copper_pilot_cli import copper_models


@pytest.fixture
def model_selector_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """Run a test with the Intelligence model selector switched on."""
    monkeypatch.setattr(copper_models, "ENABLE_MODEL_SELECTOR", True)
