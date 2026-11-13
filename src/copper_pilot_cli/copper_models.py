"""Intelligence chat model catalog shared by the TUI, headless CLI, MCP, and ACP.

Balanced turns always run on the server's default model; the server ignores
any model the client sends for them. Intelligence turns (paid plans) send
``"auto"`` (:data:`AUTO_MODEL`) so the server picks the model, or, while the
model selector is on, one model from the catalog. The server publishes the catalog at
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

# Lets users see and pick the Intelligence model in the TUI, headless CLI,
# MCP, and ACP. While False no surface names a model or offers a choice, and
# every Intelligence turn asks for AUTO_MODEL.
ENABLE_MODEL_SELECTOR = False

CHAT_MODELS_PATH = "/api/copperpilot/chat/models"
CATALOG_TIMEOUT_SECONDS = 5.0
# Model id that lets the server pick the Intelligence model (its
# ``default_intelligence_model``), so a server-side default change reaches
# every client without a release.
AUTO_MODEL = "auto"
AUTO_LABEL = "Auto"
# Bundled mirror of the server defaults, used until the catalog loads.
DEFAULT_INTELLIGENCE_MODEL = "gemini-4-carbon"
BALANCED_MODEL = "gemini-4-carbon"
MODEL_FALLBACK_EVENT_KIND = "model_fallback"
MODEL_FALLBACK_GENERIC_NOTICE = (
    "The Intelligence model is unavailable right now. Switched this chat to a backup model."
)


@dataclass(frozen=True, slots=True)
class ChatModel:
    """One user-selectable Intelligence model."""

    id: str
    label: str
    short_label: str
    vendor: str
    input_usd_per_mtok: float = 0.0
    output_usd_per_mtok: float = 0.0
    # Context window and output cap from the server catalog; ``None`` when the
    # deployment does not publish them (the bundled list never guesses).
    context_window: int | None = None
    max_output_tokens: int | None = None


@dataclass(frozen=True, slots=True)
class ContextPolicy:
    """When chats compact, from the catalog's ``context_policy`` block.

    The defaults mirror the server's; the server always has the last word
    because every final frame carries its own ``auto_compact_at``.
    """

    auto_compact_fraction: float = 0.8
    auto_compact_max_tokens: int | None = None
    reserve_output_tokens: int = 32_000
    warn_fraction: float = 0.75
    keep_recent_fraction: float = 0.15
    user_messages_budget_tokens: int = 20_000

    def auto_compact_at(self, context_window: int | None) -> int | None:
        """``floor(fraction * (window - reserve))``, capped; ``None`` without a window."""
        if not context_window or context_window <= 0:
            return None
        usable = max(context_window - self.reserve_output_tokens, 0)
        threshold = int(self.auto_compact_fraction * usable)
        if self.auto_compact_max_tokens:
            threshold = min(threshold, self.auto_compact_max_tokens)
        return threshold if threshold > 0 else None


DEFAULT_CONTEXT_POLICY = ContextPolicy()


# Picker order mirrors the desktop client: Gemini first, then Anthropic, then OpenAI.
BUNDLED_MODELS: tuple[ChatModel, ...] = (
    ChatModel("gemini-4-carbon", "Google Gemini 4 Carbon", "Gemini 4 Carbon", "Google", 2.0, 10.0),
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
    balanced_context_window: int | None = None
    context_policy: ContextPolicy = DEFAULT_CONTEXT_POLICY
    auto_label: str = AUTO_LABEL
    _by_id: dict[str, ChatModel] = field(default_factory=dict, repr=False, compare=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "_by_id", {model.id: model for model in self.models})

    @property
    def auto_model(self) -> ChatModel:
        """Picker entry for ``"auto"``, labelled with the model it runs on today."""
        target = self._by_id.get(self.default_intelligence_model)
        if target is None:
            return ChatModel(AUTO_MODEL, self.auto_label, self.auto_label, "")
        return ChatModel(
            id=AUTO_MODEL,
            label=f"{self.auto_label} ({target.short_label})",
            short_label=self.auto_label,
            vendor="",
            input_usd_per_mtok=target.input_usd_per_mtok,
            output_usd_per_mtok=target.output_usd_per_mtok,
            context_window=target.context_window,
            max_output_tokens=target.max_output_tokens,
        )

    @property
    def picker_models(self) -> tuple[ChatModel, ...]:
        """What a model picker offers: Auto first (the default), then the catalog."""
        return (self.auto_model, *self.models)

    @property
    def ids(self) -> tuple[str, ...]:
        """Catalog model ids (``auto`` excluded)."""
        return tuple(model.id for model in self.models)

    @property
    def picker_ids(self) -> tuple[str, ...]:
        """Every id a user may choose, ``auto`` first."""
        return tuple(model.id for model in self.picker_models)

    def get(self, model_id: Any) -> ChatModel | None:
        """Picker entry for an id (``auto`` included), or None when unknown."""
        normalized = normalize_model_id(model_id)
        if normalized == AUTO_MODEL:
            return self.auto_model
        return self._by_id.get(normalized)

    def selection(self, model_id: Any) -> str:
        """Picker id for a stored choice: a catalog id, else ``auto``."""
        model = self._by_id.get(normalize_model_id(model_id))
        return model.id if model is not None else AUTO_MODEL

    def normalize(self, model_id: Any) -> str:
        """Catalog id as-is; ``auto``, unknown or missing ids resolve to the default."""
        model = self._by_id.get(normalize_model_id(model_id))
        return model.id if model is not None else self.default_intelligence_model

    def effective_model(self, effort: Any, model_id: Any) -> str:
        """Model a turn will actually run on (mirrors the server rule)."""
        if normalize_effort(effort) is not CopperEffort.INTELLIGENCE:
            return self.balanced_model
        return self.normalize(model_id)

    def context_window(self, effort: Any, model_id: Any) -> int | None:
        """Context window of the model a turn runs on; ``None`` when unknown."""
        effective = self.effective_model(effort, model_id)
        model = self.get(effective)
        if model is not None and model.context_window:
            return model.context_window
        if effective == self.balanced_model:
            return self.balanced_context_window
        return None

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
        if model.id == AUTO_MODEL:
            return (
                f"CopperPilot picks the model (now {self.label(self.default_intelligence_model)})"
                " · Intelligence default"
            )
        parts = [model.vendor] if model.vendor else []
        if model.input_usd_per_mtok or model.output_usd_per_mtok:
            parts.append(
                f"${model.input_usd_per_mtok:g} in / ${model.output_usd_per_mtok:g} out per Mtok"
            )
        if model.id == self.balanced_model:
            parts.append("Balanced model")
        return " · ".join(parts)


FALLBACK_CATALOG = ChatModelCatalog()


def normalize_model_id(value: Any) -> str:
    """Lower-cased, trimmed model id; empty for missing values."""
    if value is None:
        return ""
    return str(value).strip().lower()


def model_selector_enabled() -> bool:
    """Whether users may see and pick the Intelligence model."""
    return ENABLE_MODEL_SELECTOR


def request_model(effort: Any, model_id: Any) -> str | None:
    """Model id to send in ``chat_start``; Balanced never sends one.

    While the selector is off every Intelligence turn sends ``auto`` so the
    server picks the model, including threads saved with another one. With
    the selector on, a thread with no choice (or ``auto``) sends ``auto`` too.
    """
    if normalize_effort(effort) is not CopperEffort.INTELLIGENCE:
        return None
    if not model_selector_enabled():
        return AUTO_MODEL
    return normalize_model_id(model_id) or AUTO_MODEL


def turn_model(catalog: ChatModelCatalog, effort: Any, model_id: Any) -> str:
    """Model id a chat's turns run on, as compaction and counting requests name it."""
    return request_model(effort, model_id) or catalog.effective_model(effort, model_id)


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
                context_window=_tokens(entry.get("context_window")),
                max_output_tokens=_tokens(entry.get("max_output_tokens")),
            )
        )
    if not models:
        return None
    default = normalize_model_id(payload.get("default_intelligence_model"))
    if default not in seen:
        flagged = [model.id for model in models if _is_default(raw_models, model.id)]
        default = flagged[0] if flagged else models[0].id
    balanced = normalize_model_id(payload.get("balanced_model")) or BALANCED_MODEL
    auto = payload.get("auto_model")
    auto_label = str(auto.get("label") or "").strip() if isinstance(auto, dict) else ""
    return ChatModelCatalog(
        models=tuple(models),
        default_intelligence_model=default,
        balanced_model=balanced,
        from_server=True,
        balanced_context_window=_tokens(payload.get("balanced_context_window")),
        context_policy=context_policy_from_payload(payload.get("context_policy")),
        auto_label=auto_label or AUTO_LABEL,
    )


def context_policy_from_payload(value: Any) -> ContextPolicy:
    """Parse ``context_policy``; missing or invalid fields keep the defaults."""
    if not isinstance(value, dict):
        return DEFAULT_CONTEXT_POLICY
    base = DEFAULT_CONTEXT_POLICY

    def fraction(key: str, default: float) -> float:
        raw = value.get(key)
        if isinstance(raw, (int, float)) and not isinstance(raw, bool) and 0 < raw <= 1:
            return float(raw)
        return default

    return ContextPolicy(
        auto_compact_fraction=fraction("auto_compact_fraction", base.auto_compact_fraction),
        auto_compact_max_tokens=_tokens(value.get("auto_compact_max_tokens")),
        reserve_output_tokens=_tokens(value.get("reserve_output_tokens"))
        or base.reserve_output_tokens,
        warn_fraction=fraction("warn_fraction", base.warn_fraction),
        keep_recent_fraction=fraction("keep_recent_fraction", base.keep_recent_fraction),
        user_messages_budget_tokens=_tokens(value.get("user_messages_budget_tokens"))
        or base.user_messages_budget_tokens,
    )


def _tokens(value: Any) -> int | None:
    """A positive token count, else ``None``."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return int(value) if value > 0 else None


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
    if not model_selector_enabled():
        return MODEL_FALLBACK_GENERIC_NOTICE
    message = str(value.get("message") or "").strip()
    if message:
        return message
    source = catalog or cached_chat_model_catalog()
    origin = str(value.get("from_label") or "").strip() or source.label(value.get("from_model"))
    target = str(value.get("to_label") or "").strip() or source.label(value.get("to_model"))
    return f"{origin} is unavailable right now. Switched this chat to {target}."
