"""Intelligence chat model catalog shared by the TUI, headless CLI, MCP, and ACP.

Balanced turns always run on the server's default model; the server ignores
any model the client sends for them. Intelligence turns (paid plans) may pick
one model from the catalog. The server publishes the catalog at
``GET /api/copperpilot/chat/models``; the bundled list below mirrors it and is
used until the first successful fetch, or when the deployment predates the
endpoint, so the pickers always have something to show.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any

import httpx

from copper_pilot_cli.copper_auth import DeviceCredential
from copper_pilot_cli.copper_protocol import CopperEffort, normalize_effort

logger = logging.getLogger(__name__)

CHAT_MODELS_PATH = "/api/copperpilot/chat/models"
CATALOG_TIMEOUT_SECONDS = 5.0
DEFAULT_INTELLIGENCE_MODEL = "gemini-4-argon"
BALANCED_MODEL = "gemini-3.8-flash"
MODEL_FALLBACK_EVENT_KIND = "model_fallback"


@dataclass(frozen=True, slots=True)
class ChatModel:
    """One user-selectable Intelligence model."""

    id: str
    label: str
    short_label: str
    vendor: str
    input_usd_per_mtok: float = 0.0
    output_usd_per_mtok: float = 0.0


# Picker order mirrors the desktop client: Gemini first, then Anthropic, then OpenAI.
BUNDLED_MODELS: tuple[ChatModel, ...] = (
    ChatModel("gemini-4-argon", "Google Gemini 4 Argon", "Gemini 4 Argon", "Google", 2.0, 10.0),
    ChatModel(
        "gemini-3.8-flash", "Google Gemini 3.8 Flash", "Gemini 3.8 Flash", "Google", 1.5, 7.5
    ),
    ChatModel("claude-opus-5-5", "Claude Opus 5.5", "Opus 5.5", "Anthropic", 4.0, 20.0),
    ChatModel("claude-sonnet-5-5", "Claude Sonnet 5.5", "Sonnet 5.5", "Anthropic", 2.0, 10.0),
    ChatModel("gpt-6-astra", "OpenAI GPT-6 Astra", "GPT-6 Astra", "OpenAI", 10.0, 50.0),
    ChatModel("gpt-6.1-sol", "OpenAI GPT-6.1 Sol", "GPT-6.1 Sol", "OpenAI", 2.0, 10.0),
)


@dataclass(frozen=True, slots=True)
class ChatModelCatalog:
    """Selectable Intelligence models plus the server's routing defaults."""

    models: tuple[ChatModel, ...] = BUNDLED_MODELS
    default_intelligence_model: str = DEFAULT_INTELLIGENCE_MODEL
    balanced_model: str = BALANCED_MODEL
    from_server: bool = False
    _by_id: dict[str, ChatModel] = field(default_factory=dict, repr=False, compare=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "_by_id", {model.id: model for model in self.models})

    @property
    def ids(self) -> tuple[str, ...]:
        return tuple(model.id for model in self.models)

    def get(self, model_id: Any) -> ChatModel | None:
        """Catalog entry for an id, or None when unknown."""
        return self._by_id.get(normalize_model_id(model_id))

    def normalize(self, model_id: Any) -> str:
        """Known id as-is; unknown or missing ids resolve to the Intelligence default."""
        model = self.get(model_id)
        return model.id if model is not None else self.default_intelligence_model

    def effective_model(self, effort: Any, model_id: Any) -> str:
        """Model a turn will actually run on (mirrors the server rule)."""
        if normalize_effort(effort) is not CopperEffort.INTELLIGENCE:
            return self.balanced_model
        return self.normalize(model_id)

    def label(self, model_id: Any) -> str:
        """Human label for an id; falls back to the raw id."""
        model = self.get(model_id)
        if model is not None:
            return model.label
        return normalize_model_id(model_id) or "the selected model"

    def short_label(self, model_id: Any) -> str:
        model = self.get(model_id)
        if model is not None:
            return model.short_label
        return normalize_model_id(model_id) or "the selected model"

    def describe(self, model: ChatModel) -> str:
        """One-line picker description: vendor, list price, and routing role."""
        parts = [model.vendor] if model.vendor else []
        if model.input_usd_per_mtok or model.output_usd_per_mtok:
            parts.append(
                f"${model.input_usd_per_mtok:g} in / ${model.output_usd_per_mtok:g} out per Mtok"
            )
        if model.id == self.default_intelligence_model:
            parts.append("Intelligence default")
        if model.id == self.balanced_model:
            parts.append("Balanced model")
        return " · ".join(parts)


FALLBACK_CATALOG = ChatModelCatalog()


def normalize_model_id(value: Any) -> str:
    """Lower-cased, trimmed model id; empty for missing values."""
    if value is None:
        return ""
    return str(value).strip().lower()


def request_model(effort: Any, model_id: Any) -> str | None:
    """Model id to send in ``chat_start``; Balanced never sends one."""
    if normalize_effort(effort) is not CopperEffort.INTELLIGENCE:
        return None
    return normalize_model_id(model_id) or None


def _short_label(label: str, vendor: str) -> str:
    prefix = f"{vendor.strip()} "
    return label[len(prefix) :] if vendor.strip() and label.startswith(prefix) else label


def catalog_from_payload(payload: Any) -> ChatModelCatalog | None:
    """Parse the server catalog; None when the payload is unusable."""
    if not isinstance(payload, dict):
        return None
    raw_models = payload.get("models")
    if not isinstance(raw_models, list) or not raw_models:
        return None
    models: list[ChatModel] = []
    seen: set[str] = set()
    for entry in raw_models:
        if not isinstance(entry, dict):
            continue
        model_id = normalize_model_id(entry.get("id"))
        label = str(entry.get("label") or "").strip()
        if not model_id or not label or model_id in seen:
            continue
        seen.add(model_id)
        vendor = str(entry.get("vendor") or "").strip()
        short = str(entry.get("short_label") or "").strip() or _short_label(label, vendor)
        models.append(
            ChatModel(
                id=model_id,
                label=label,
                short_label=short,
                vendor=vendor,
                input_usd_per_mtok=_price(entry.get("input_usd_per_mtok")),
                output_usd_per_mtok=_price(entry.get("output_usd_per_mtok")),
            )
        )
    if not models:
        return None
    default = normalize_model_id(payload.get("default_intelligence_model"))
    if default not in seen:
        flagged = [model.id for model in models if _is_default(raw_models, model.id)]
        default = flagged[0] if flagged else models[0].id
    balanced = normalize_model_id(payload.get("balanced_model")) or BALANCED_MODEL
    return ChatModelCatalog(
        models=tuple(models),
        default_intelligence_model=default,
        balanced_model=balanced,
        from_server=True,
    )


def _is_default(raw_models: list[Any], model_id: str) -> bool:
    for entry in raw_models:
        if isinstance(entry, dict) and normalize_model_id(entry.get("id")) == model_id:
            return bool(entry.get("default"))
    return False


def _price(value: Any) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    return number if number > 0 else 0.0


async def fetch_chat_models(
    credential: DeviceCredential,
    *,
    timeout_seconds: float = CATALOG_TIMEOUT_SECONDS,
    transport: httpx.AsyncBaseTransport | None = None,
) -> ChatModelCatalog | None:
    """Fetch the server catalog; None on any transport, HTTP, or payload problem."""
    try:
        async with httpx.AsyncClient(timeout=timeout_seconds, transport=transport) as client:
            response = await client.get(
                f"{credential.base_url.rstrip('/')}{CHAT_MODELS_PATH}",
                headers={
                    "Authorization": f"Bearer {credential.api_key}",
                    "X-CopperPilot-Fingerprint": credential.fingerprint,
                },
            )
        if response.status_code != 200:
            logger.debug("Chat model catalog unavailable (HTTP %s)", response.status_code)
            return None
        return catalog_from_payload(response.json())
    except (httpx.HTTPError, ValueError, OSError) as exc:
        logger.debug("Chat model catalog fetch failed: %s", type(exc).__name__)
        return None


_cached: ChatModelCatalog | None = None
_inflight: asyncio.Task[ChatModelCatalog | None] | None = None


def cached_chat_model_catalog() -> ChatModelCatalog:
    """Last server catalog loaded in this process, else the bundled fallback."""
    return _cached or FALLBACK_CATALOG


def reset_chat_model_catalog(catalog: ChatModelCatalog | None = None) -> None:
    """Test/reset hook."""
    global _cached, _inflight
    _cached = catalog
    _inflight = None


async def load_chat_model_catalog(
    credential: DeviceCredential | None, *, force: bool = False
) -> ChatModelCatalog:
    """Return the server catalog, fetching it once per process; fallback on failure.

    Concurrent callers share one in-flight request. A failed fetch keeps the
    bundled list and is retried on the next call, so a deployment that gains
    the endpoint later is picked up without a restart.
    """
    global _cached, _inflight
    if credential is None:
        return cached_chat_model_catalog()
    if _cached is not None and not force:
        return _cached
    if _inflight is None or _inflight.done():
        _inflight = asyncio.ensure_future(fetch_chat_models(credential))
    try:
        result = await asyncio.shield(_inflight)
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 - the fetch already logged
        result = None
    if result is not None:
        _cached = result
    return cached_chat_model_catalog()


def is_model_fallback_event(value: Any) -> bool:
    """Server status event announcing that the turn switched models."""
    return (
        isinstance(value, dict)
        and value.get("kind") == MODEL_FALLBACK_EVENT_KIND
        and isinstance(value.get("to_model"), str)
    )


def model_fallback_notice(value: dict[str, Any], catalog: ChatModelCatalog | None = None) -> str:
    """Transcript copy for a fallback event (server message, else a safe default)."""
    message = str(value.get("message") or "").strip()
    if message:
        return message
    source = catalog or cached_chat_model_catalog()
    origin = str(value.get("from_label") or "").strip() or source.label(value.get("from_model"))
    target = str(value.get("to_label") or "").strip() or source.label(value.get("to_model"))
    return f"{origin} is unavailable right now. Switched this chat to {target}."
