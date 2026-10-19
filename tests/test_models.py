from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from copper_pilot_cli import copper_models
from copper_pilot_cli.copper_auth import DeviceCredential
from copper_pilot_cli.copper_models import (
    BALANCED_MODEL,
    BUNDLED_MODELS,
    DEFAULT_INTELLIGENCE_MODEL,
    FALLBACK_CATALOG,
    ChatModelCatalog,
    cached_chat_model_catalog,
    catalog_from_payload,
    fetch_chat_models,
    is_model_fallback_event,
    load_chat_model_catalog,
    model_fallback_notice,
    normalize_model_id,
    request_model,
    reset_chat_model_catalog,
)
from copper_pilot_cli.copper_protocol import CopperEffort

CREDENTIAL = DeviceCredential(
    api_key="cf_live_test", fingerprint="fp", base_url="https://example.test/"
)

SERVER_PAYLOAD = {
    "models": [
        {
            "id": "claude-opus-5-5",
            "label": "Claude Opus 5.5",
            "short_label": "Opus 5.5",
            "vendor": "Anthropic",
            "provider": "anthropic",
            "input_usd_per_mtok": 4.0,
            "output_usd_per_mtok": 20.0,
            "default": True,
        },
        {
            "id": "Future-Model-9",
            "label": "Vendor Future Model 9",
            "vendor": "Vendor",
            "input_usd_per_mtok": "1.25",
            "output_usd_per_mtok": None,
        },
        {"id": "", "label": "ignored"},
        {"id": "claude-opus-5-5", "label": "duplicate"},
        "junk",
    ],
    "default_intelligence_model": "claude-opus-5-5",
    "balanced_model": "gemini-3.8-flash",
    "fallback_models": ["gemini-4-argon", "gemini-3.8-flash"],
}


@pytest.fixture(autouse=True)
def _reset_catalog():
    reset_chat_model_catalog()
    yield
    reset_chat_model_catalog()


def test_bundled_catalog_mirrors_the_desktop_client() -> None:
    assert FALLBACK_CATALOG.ids == (
        "gemini-4-argon",
        "gemini-3.8-flash",
        "claude-opus-5-5",
        "claude-sonnet-5-5",
        "gpt-6-astra",
        "gpt-6.1-sol",
    )
    assert DEFAULT_INTELLIGENCE_MODEL == "gemini-4-argon"
    assert FALLBACK_CATALOG.default_intelligence_model == DEFAULT_INTELLIGENCE_MODEL
    assert FALLBACK_CATALOG.balanced_model == BALANCED_MODEL
    assert FALLBACK_CATALOG.from_server is False
    assert len(BUNDLED_MODELS) == 6


def test_normalize_and_effective_model() -> None:
    catalog = FALLBACK_CATALOG
    assert normalize_model_id("  GPT-6.1-Sol ") == "gpt-6.1-sol"
    assert normalize_model_id(None) == ""
    assert catalog.normalize("GPT-6.1-Sol") == "gpt-6.1-sol"
    assert catalog.normalize("mystery") == DEFAULT_INTELLIGENCE_MODEL
    assert catalog.normalize(None) == DEFAULT_INTELLIGENCE_MODEL
    assert catalog.effective_model("balanced", "claude-opus-5-5") == BALANCED_MODEL
    assert catalog.effective_model(CopperEffort.INTELLIGENCE, None) == DEFAULT_INTELLIGENCE_MODEL
    assert catalog.effective_model("intelligence", "gemini-4-argon") == "gemini-4-argon"
    assert catalog.label("gpt-6-astra") == "OpenAI GPT-6 Astra"
    assert catalog.label("mystery") == "mystery"
    assert catalog.label("") == "the selected model"
    assert catalog.short_label("claude-sonnet-5-5") == "Sonnet 5.5"


def test_describe_lists_vendor_price_and_role() -> None:
    catalog = FALLBACK_CATALOG
    argon = catalog.get("gemini-4-argon")
    opus = catalog.get("claude-opus-5-5")
    flash = catalog.get("gemini-3.8-flash")
    assert argon is not None and opus is not None and flash is not None
    assert catalog.describe(argon) == "Google · $2 in / $10 out per Mtok · Intelligence default"
    assert catalog.describe(opus) == "Anthropic · $4 in / $20 out per Mtok"
    assert catalog.describe(flash) == "Google · $1.5 in / $7.5 out per Mtok · Balanced model"


@pytest.mark.usefixtures("model_selector_enabled")
def test_request_model_is_sent_only_for_intelligence() -> None:
    assert request_model("balanced", "claude-opus-5-5") is None
    assert request_model("intelligence", " Claude-Opus-5-5 ") == "claude-opus-5-5"
    assert request_model("intelligence", None) is None
    assert request_model("intelligence", "   ") is None


def test_catalog_from_payload_parses_server_shape() -> None:
    catalog = catalog_from_payload(SERVER_PAYLOAD)
    assert catalog is not None
    assert catalog.from_server is True
    assert catalog.ids == ("claude-opus-5-5", "future-model-9")
    future = catalog.get("future-model-9")
    assert future is not None
    assert future.short_label == "Future Model 9"
    assert future.input_usd_per_mtok == 1.25
    assert future.output_usd_per_mtok == 0.0
    assert catalog.default_intelligence_model == "claude-opus-5-5"
    assert catalog.balanced_model == "gemini-3.8-flash"


def test_catalog_from_payload_defaults_and_rejects_junk() -> None:
    assert catalog_from_payload(None) is None
    assert catalog_from_payload({"models": []}) is None
    assert catalog_from_payload({"models": ["x", {"id": "a"}]}) is None
    flagged = catalog_from_payload(
        {"models": [{"id": "a", "label": "A"}, {"id": "b", "label": "B", "default": True}]}
    )
    assert flagged is not None
    assert flagged.default_intelligence_model == "b"
    assert flagged.balanced_model == BALANCED_MODEL
    first = catalog_from_payload({"models": [{"id": "a", "label": "A"}]})
    assert first is not None and first.default_intelligence_model == "a"


def _transport(handler):
    return httpx.MockTransport(handler)


def test_fetch_chat_models_success_and_failures() -> None:
    seen: dict[str, object] = {}

    def ok(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization")
        seen["fingerprint"] = request.headers.get("x-copperpilot-fingerprint")
        return httpx.Response(200, json=SERVER_PAYLOAD)

    catalog = asyncio.run(fetch_chat_models(CREDENTIAL, transport=_transport(ok)))
    assert catalog is not None and catalog.from_server
    assert seen == {
        "url": "https://example.test/api/copperpilot/chat/models",
        "auth": "Bearer cf_live_test",
        "fingerprint": "fp",
    }

    def not_found(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"detail": "Not Found"})

    assert asyncio.run(fetch_chat_models(CREDENTIAL, transport=_transport(not_found))) is None

    def bad_json(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"not json")

    assert asyncio.run(fetch_chat_models(CREDENTIAL, transport=_transport(bad_json))) is None

    def boom(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    assert asyncio.run(fetch_chat_models(CREDENTIAL, transport=_transport(boom))) is None


@pytest.mark.asyncio
async def test_load_catalog_caches_success_and_retries_failure(monkeypatch) -> None:
    calls = {"n": 0}
    results: list[ChatModelCatalog | None] = [None, catalog_from_payload(SERVER_PAYLOAD)]

    async def fake_fetch(_credential):
        calls["n"] += 1
        await asyncio.sleep(0)
        return results.pop(0)

    monkeypatch.setattr(copper_models, "fetch_chat_models", fake_fetch)
    assert await load_chat_model_catalog(None) is FALLBACK_CATALOG
    assert calls["n"] == 0
    # First fetch fails: bundled list, and the next call retries.
    assert (await load_chat_model_catalog(CREDENTIAL)) is FALLBACK_CATALOG
    assert cached_chat_model_catalog() is FALLBACK_CATALOG
    # Concurrent callers share one in-flight fetch.
    first, second = await asyncio.gather(
        load_chat_model_catalog(CREDENTIAL), load_chat_model_catalog(CREDENTIAL)
    )
    assert first is second and first.from_server
    assert calls["n"] == 2
    assert await load_chat_model_catalog(CREDENTIAL) is first
    assert calls["n"] == 2
    assert cached_chat_model_catalog() is first


@pytest.mark.usefixtures("model_selector_enabled")
def test_model_fallback_event_and_notice() -> None:
    event = {
        "kind": "model_fallback",
        "from_model": "claude-opus-5-5",
        "to_model": "gemini-4-argon",
        "message": "  Claude Opus 5.5 is unavailable right now. Switched this chat to Gemini.  ",
    }
    assert is_model_fallback_event(event)
    assert not is_model_fallback_event({"kind": "model_fallback"})
    assert not is_model_fallback_event({"kind": "subagent", "to_model": "x"})
    assert not is_model_fallback_event("model_fallback")
    assert model_fallback_notice(event) == (
        "Claude Opus 5.5 is unavailable right now. Switched this chat to Gemini."
    )
    bare = {"kind": "model_fallback", "from_model": "gpt-6-astra", "to_model": "gemini-4-argon"}
    assert model_fallback_notice(bare) == (
        "OpenAI GPT-6 Astra is unavailable right now. Switched this chat to Google Gemini 4 Argon."
    )
    labelled = {**bare, "from_label": "Astra", "to_label": "Argon"}
    assert model_fallback_notice(labelled) == (
        "Astra is unavailable right now. Switched this chat to Argon."
    )
    json.dumps(event)


def test_model_selector_ships_off() -> None:
    assert copper_models.ENABLE_MODEL_SELECTOR is False
    assert copper_models.model_selector_enabled() is False


def test_request_model_pins_the_default_while_the_selector_is_off() -> None:
    assert request_model("balanced", "claude-opus-5-5") is None
    assert request_model("intelligence", "claude-opus-5-5") == DEFAULT_INTELLIGENCE_MODEL
    assert request_model("intelligence", None) == DEFAULT_INTELLIGENCE_MODEL
    assert DEFAULT_INTELLIGENCE_MODEL == "gemini-4-argon"


def test_model_fallback_notice_names_no_model_while_the_selector_is_off() -> None:
    event = {
        "kind": "model_fallback",
        "from_model": "gemini-4-argon",
        "to_model": "gemini-3.8-flash",
        "message": "Google Gemini 4 Argon is unavailable right now.",
    }
    notice = model_fallback_notice(event)
    assert notice == copper_models.MODEL_FALLBACK_GENERIC_NOTICE
    assert "Gemini" not in notice
