import sys

import pytest

from copper_pilot_cli import copper_models, copper_theme


@pytest.fixture
def model_selector_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """Run a test with the Intelligence model selector switched on."""
    monkeypatch.setattr(copper_models, "ENABLE_MODEL_SELECTOR", True)


@pytest.fixture(autouse=True)
def no_update_notice_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep an already-imported app's background update check off the network."""
    module = sys.modules.get("copper_pilot_cli.copper_app")
    if module is None:
        return

    async def no_notice() -> None:
        return None

    monkeypatch.setattr(module, "check_for_background_notice", no_notice)


@pytest.fixture(autouse=True)
def default_theme(
    request: pytest.FixtureRequest,
    tmp_path_factory: pytest.TempPathFactory,
    monkeypatch: pytest.MonkeyPatch,
):
    """Keep the user's saved theme out of tests and reset the shared palette.

    Live tests keep the real home because they need the stored device credential.
    """
    if request.node.get_closest_marker("live") is None:
        monkeypatch.setenv("COPPER_PILOT_CLI_HOME", str(tmp_path_factory.mktemp("cli-home")))
    # Windows CI has no UTF-8 locale, so the app would draw the ASCII fallback.
    # The suite asserts the Unicode chrome. A test opts into ASCII with COPPERPILOT_ASCII.
    monkeypatch.delenv("COPPERPILOT_ASCII", raising=False)
    monkeypatch.setenv("LC_ALL", "C.UTF-8")
    monkeypatch.setenv("LC_CTYPE", "C.UTF-8")
    monkeypatch.setenv("LANG", "C.UTF-8")
    copper_theme.set_active_theme(copper_theme.DEFAULT_THEME)
    yield
    copper_theme.set_active_theme(copper_theme.DEFAULT_THEME)
