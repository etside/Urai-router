"""Provider registry, model catalogue and upstream request building."""

from __future__ import annotations

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from . import adapters
from .util import cache_dir, http_request, mask_secret, now, read_json, request_json, write_json

# OpenCode Zen free rows are time-limited; this is only an offline fallback.
ZEN_FREE_FALLBACK = [
    "big-pickle",
    "beam-free",
    "flint-40b-free",
    "mimo-v2.6-flash-free",
    "nemotron-3.5-super-vl-free",
    "nemotron-3.5-lightning-free",
    "deepseek-v4.1-flash-free",
    "glm-5.3-flash-free",
]


@dataclass(frozen=True)
class Provider:
    id: str
    name: str
    base_url: str
    models_url: Optional[str] = None
    api_style: str = "openai"  # openai | anthropic
    auth: str = "bearer"  # bearer | x-api-key | none
    key_prefixes: Tuple[str, ...] = ()
    keyless: bool = False
    detect_order: int = 50
    docs: str = ""
    default_models: Tuple[str, ...] = ()
    extra_headers: Tuple[Tuple[str, str], ...] = ()
    public_catalog: bool = False

    def model_url(self) -> str:
        return self.models_url or self.base_url.rstrip("/") + "/models"


PROVIDER_LIST: List[Provider] = [
    Provider(
        "opencode",
        "OpenCode Zen",
        "https://opencode.ai/zen/v1",
        models_url="https://opencode.ai/zen/v1/models",
        key_prefixes=("sk-",),
        detect_order=0,
        docs="https://opencode.ai/docs/zen/",
        default_models=tuple(ZEN_FREE_FALLBACK)
        + ("deepseek-v4.1-flash", "gpt-5-nano", "qwen3.8-flash", "claude-haiku-4-5"),
        public_catalog=True,
    ),
    Provider(
        "openrouter",
        "OpenRouter",
        "https://openrouter.ai/api/v1",
        key_prefixes=("sk-or-v1-", "sk-or-"),
        detect_order=2,
        docs="https://openrouter.ai/docs",
        default_models=("openrouter/auto",),
        public_catalog=True,
        extra_headers=(("HTTP-Referer", "https://github.com/etside/Urai-router"), ("X-Title", "Urai Router")),
    ),
    Provider(
        "groq",
        "Groq",
        "https://api.groq.com/openai/v1",
        key_prefixes=("gsk_",),
        detect_order=5,
        docs="https://console.groq.com/docs",
        default_models=("llama-3.3-70b-versatile", "openai/gpt-oss-120b"),
    ),
    Provider(
        "anthropic",
        "Anthropic",
        "https://api.anthropic.com/v1",
        api_style="anthropic",
        auth="x-api-key",
        key_prefixes=("sk-ant-",),
        detect_order=8,
        docs="https://docs.anthropic.com",
        default_models=("claude-sonnet-4-5", "claude-haiku-4-5", "claude-opus-4-5"),
        extra_headers=(("anthropic-version", "2023-06-01"),),
    ),
    Provider(
        "openai",
        "OpenAI",
        "https://api.openai.com/v1",
        key_prefixes=("sk-proj-", "sk-svcacct-"),
        detect_order=10,
        docs="https://platform.openai.com/docs",
        default_models=("gpt-5", "gpt-5-mini", "gpt-5-nano"),
    ),
    Provider(
        "deepseek",
        "DeepSeek",
        "https://api.deepseek.com/v1",
        key_prefixes=("sk-",),
        detect_order=12,
        docs="https://api-docs.deepseek.com",
        default_models=("deepseek-chat", "deepseek-reasoner"),
    ),
    Provider(
        "google",
        "Google Gemini",
        "https://generativelanguage.googleapis.com/v1beta/openai",
        key_prefixes=("AIza",),
        detect_order=14,
        docs="https://ai.google.dev/gemini-api/docs",
        default_models=("gemini-2.5-flash", "gemini-2.5-pro"),
    ),
    Provider(
        "xai",
        "xAI",
        "https://api.x.ai/v1",
        key_prefixes=("xai-",),
        detect_order=16,
        docs="https://docs.x.ai",
        default_models=("grok-4", "grok-3-mini"),
    ),
    Provider(
        "nvidia",
        "NVIDIA NIM",
        "https://integrate.api.nvidia.com/v1",
        key_prefixes=("nvapi-",),
        detect_order=18,
        docs="https://build.nvidia.com",
    ),
    Provider(
        "moonshot",
        "Moonshot / Kimi",
        "https://api.moonshot.ai/v1",
        key_prefixes=("sk-",),
        detect_order=30,
        docs="https://platform.moonshot.ai/docs",
        default_models=("kimi-k2.5", "kimi-k2-turbo-preview"),
    ),
    Provider(
        "mistral",
        "Mistral",
        "https://api.mistral.ai/v1",
        detect_order=32,
        docs="https://docs.mistral.ai",
        default_models=("mistral-large-latest", "mistral-small-latest"),
    ),
    Provider(
        "together",
        "Together AI",
        "https://api.together.xyz/v1",
        key_prefixes=("tgp_v1_",),
        detect_order=34,
        docs="https://docs.together.ai",
    ),
    Provider(
        "fireworks",
        "Fireworks AI",
        "https://api.fireworks.ai/inference/v1",
        key_prefixes=("fw_",),
        detect_order=36,
        docs="https://docs.fireworks.ai",
    ),
    Provider(
        "cerebras",
        "Cerebras",
        "https://api.cerebras.ai/v1",
        key_prefixes=("csk-",),
        detect_order=38,
        docs="https://inference-docs.cerebras.ai",
    ),
    Provider(
        "huggingface",
        "Hugging Face",
        "https://router.huggingface.co/v1",
        key_prefixes=("hf_",),
        detect_order=40,
        docs="https://huggingface.co/docs/inference-providers",
    ),
    Provider(
        "zai",
        "Z.AI",
        "https://api.z.ai/api/paas/v4",
        detect_order=42,
        docs="https://docs.z.ai",
        default_models=("glm-4.6", "glm-4.5-air"),
    ),
    Provider(
        "minimax",
        "MiniMax",
        "https://api.minimax.io/v1",
        detect_order=44,
        docs="https://platform.minimax.io/docs",
        default_models=("MiniMax-M2",),
    ),
    Provider(
        "deepinfra",
        "DeepInfra",
        "https://api.deepinfra.com/v1/openai",
        detect_order=46,
        docs="https://deepinfra.com/docs",
    ),
    Provider(
        "perplexity",
        "Perplexity",
        "https://api.perplexity.ai",
        key_prefixes=("pplx-",),
        detect_order=48,
        docs="https://docs.perplexity.ai",
        default_models=("sonar", "sonar-pro"),
    ),
    Provider(
        "github",
        "GitHub Models",
        "https://models.github.ai/inference",
        key_prefixes=("github_pat_", "ghp_"),
        detect_order=50,
        docs="https://docs.github.com/en/github-models",
    ),
    Provider(
        "nebius",
        "Nebius Token Factory",
        "https://api.studio.nebius.ai/v1",
        detect_order=52,
        docs="https://docs.nebius.com",
    ),
    Provider(
        "sambanova",
        "SambaNova",
        "https://api.sambanova.ai/v1",
        detect_order=54,
        docs="https://docs.sambanova.ai",
    ),
    Provider(
        "cohere",
        "Cohere",
        "https://api.cohere.ai/compatibility/v1",
        detect_order=56,
        docs="https://docs.cohere.com",
    ),
    Provider(
        "ollama",
        "Ollama (local)",
        "http://127.0.0.1:11434/v1",
        keyless=True,
        auth="none",
        detect_order=90,
        docs="https://docs.ollama.com",
        public_catalog=True,
    ),
    Provider(
        "lmstudio",
        "LM Studio (local)",
        "http://127.0.0.1:1234/v1",
        keyless=True,
        auth="none",
        detect_order=92,
        docs="https://lmstudio.ai/docs",
        public_catalog=True,
    ),
    Provider(
        "llamacpp",
        "llama.cpp server (local)",
        "http://127.0.0.1:8080/v1",
        keyless=True,
        auth="none",
        detect_order=94,
        docs="https://github.com/ggml-org/llama.cpp",
        public_catalog=True,
    ),
]

PROVIDERS: Dict[str, Provider] = {p.id: p for p in PROVIDER_LIST}

ALIASES: Dict[str, str] = {
    "zen": "opencode",
    "opencode-zen": "opencode",
    "oc": "opencode",
    "or": "openrouter",
    "claude": "anthropic",
    "google": "google",
    "gemini": "google",
    "local": "ollama",
    "llama": "llamacpp",
    "lm-studio": "lmstudio",
    "kimi": "moonshot",
    "glm": "zai",
    "github-models": "github",
    "hf": "huggingface",
}


def provider_id(name: str) -> Optional[str]:
    if not name:
        return None
    key = str(name).strip().lower().replace("_", "-").replace(" ", "-")
    if key in PROVIDERS:
        return key
    return ALIASES.get(key)


def all_providers() -> List[Provider]:
    return list(PROVIDERS.values())


def register_custom(id: str, values: Dict[str, Any]) -> Provider:
    """Register a custom OpenAI-compatible provider from config."""
    base_url = str(values.get("base_url") or values.get("baseURL") or "").rstrip("/")
    if not id or not base_url:
        raise ValueError("custom provider needs an id and base_url")
    provider = Provider(
        id=id,
        name=str(values.get("name") or id),
        base_url=base_url,
        models_url=values.get("models_url"),
        api_style=str(values.get("api_style") or "openai"),
        auth=str(values.get("auth") or ("none" if not values.get("key") else "bearer")),
        keyless=bool(values.get("keyless", False)),
        detect_order=int(values.get("detect_order", 60)),
        docs=str(values.get("docs") or ""),
        default_models=tuple(values.get("default_models") or ()),
        extra_headers=tuple((str(k), str(v)) for k, v in (values.get("headers") or {}).items()),
        public_catalog=bool(values.get("public_catalog", False)),
    )
    PROVIDERS[id] = provider
    return provider


def apply_provider_overrides(overrides: Dict[str, Dict[str, Any]]) -> None:
    for pid, values in (overrides or {}).items():
        if pid in PROVIDERS:
            base = asdict(PROVIDERS[pid])
            base.update({k: v for k, v in values.items() if k in base})
            if "base_url" in values:
                base["base_url"] = values["base_url"]
            base["key_prefixes"] = tuple(base.get("key_prefixes") or ())
            base["default_models"] = tuple(base.get("default_models") or ())
            base["extra_headers"] = tuple(
                (str(k), str(v)) for k, v in dict(base.get("extra_headers") or {}).items()
            )
            base["models_url"] = base.get("models_url") or None
            PROVIDERS[pid] = Provider(**base)
        else:
            try:
                register_custom(pid, values)
            except ValueError:
                continue


def detect_by_prefix(key: str) -> List[str]:
    """Return providers sharing the *most specific* matching prefix.

    ``sk-or-v1-...`` also starts with the generic ``sk-`` shared by DeepSeek,
    Moonshot and Zen, so only the longest prefix should win.
    """
    matches: List[Tuple[int, str]] = []
    for provider in PROVIDERS.values():
        if provider.keyless:
            continue
        for prefix in provider.key_prefixes:
            if key.startswith(prefix):
                matches.append((len(prefix), provider.id))
                break
    if not matches:
        return []
    best = max(length for length, _ in matches)
    seen: List[str] = []
    for length, pid in sorted(matches, key=lambda row: (-row[0], row[1])):
        if length == best and pid not in seen:
            seen.append(pid)
    return seen


@dataclass
class ModelInfo:
    id: str
    provider: str
    name: str = ""
    free: bool = False
    context: int = 0
    output: int = 0
    input_price: float = -1.0
    output_price: float = -1.0
    api_style: str = ""
    endpoint: str = ""
    tools: bool = True
    raw: Dict[str, Any] = field(default_factory=dict)

    @property
    def qualified(self) -> str:
        return f"{self.provider}/{self.id}"

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        data.pop("raw", None)
        return data

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ModelInfo":
        allowed = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in data.items() if k in allowed})


def _price(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return -1.0


def parse_models(provider: Provider, payload: Any) -> List[ModelInfo]:
    if not isinstance(payload, dict):
        return []
    rows = payload.get("data")
    if rows is None and isinstance(payload.get("models"), list):
        rows = payload["models"]
    if not isinstance(rows, list):
        return []
    models: List[ModelInfo] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        mid = row.get("id") or row.get("name") or row.get("model")
        if not mid:
            continue
        mid = str(mid)
        if provider.id == "opencode":
            info = _parse_zen_model(provider, mid, row)
        elif provider.id == "openrouter":
            info = _parse_openrouter_model(provider, mid, row)
        else:
            info = _parse_generic_model(provider, mid, row)
        models.append(info)
    return models


def _parse_zen_model(provider: Provider, mid: str, row: Dict[str, Any]) -> ModelInfo:
    text_cost = ((row.get("cost") or {}).get("text") or {}) if isinstance(row.get("cost"), dict) else {}
    input_price = _price((text_cost.get("input") or {}).get("price"))
    output_price = _price((text_cost.get("output") or {}).get("price"))
    free = input_price == 0 and output_price == 0
    api = row.get("api") or {}
    endpoint = ""
    style = "openai"
    if isinstance(api, dict):
        block = api.get("chat-completions")
        if isinstance(block, dict):
            endpoint = str(block.get("base_url") or "")
        else:
            block = api.get("messages")
            if isinstance(block, dict):
                endpoint = str(block.get("base_url") or "")
                style = "anthropic"
    limit = row.get("limit") or {}
    caps = row.get("capabilities") or {}
    tools = bool((caps.get("tools") or {}).get("supported", True))
    return ModelInfo(
        id=mid,
        provider=provider.id,
        name=str(row.get("name") or mid),
        free=free,
        context=int(limit.get("context") or 0),
        output=int(limit.get("output") or 0),
        input_price=input_price,
        output_price=output_price,
        api_style=style,
        endpoint=endpoint,
        tools=tools,
        raw=row,
    )


def _parse_openrouter_model(provider: Provider, mid: str, row: Dict[str, Any]) -> ModelInfo:
    pricing = row.get("pricing") or {}
    input_price = _price(pricing.get("prompt"))
    output_price = _price(pricing.get("completion"))
    if input_price >= 0:
        input_price *= 1_000_000
    if output_price >= 0:
        output_price *= 1_000_000
    top = row.get("top_provider") or {}
    supported = row.get("supported_parameters") or []
    return ModelInfo(
        id=mid,
        provider=provider.id,
        name=str(row.get("name") or mid),
        free=input_price == 0 and output_price == 0,
        context=int(row.get("context_length") or top.get("context_length") or 0),
        output=int(top.get("max_completion_tokens") or 0),
        input_price=input_price,
        output_price=output_price,
        api_style="openai",
        tools=("tools" in supported) if supported else True,
        raw=row,
    )


def _parse_generic_model(provider: Provider, mid: str, row: Dict[str, Any]) -> ModelInfo:
    price = row.get("pricing") or {}
    input_price = _price(price.get("prompt", price.get("input", -1)))
    output_price = _price(price.get("completion", price.get("output", -1)))
    if 0 < input_price < 0.01:
        input_price *= 1_000_000
    if 0 < output_price < 0.01:
        output_price *= 1_000_000
    return ModelInfo(
        id=mid,
        provider=provider.id,
        name=str(row.get("name") or mid),
        free=("free" in mid.lower()) or (input_price == 0 and output_price == 0),
        context=int(row.get("context_length") or row.get("context_window") or 0),
        output=int(row.get("max_output_tokens") or row.get("max_completion_tokens") or 0),
        input_price=input_price,
        output_price=output_price,
        api_style=provider.api_style,
        raw=row,
    )


def _auth_headers(provider: Provider, key: Optional[str]) -> Dict[str, str]:
    headers: Dict[str, str] = {"Accept": "application/json"}
    if provider.auth == "none" or not key:
        pass
    elif provider.auth == "x-api-key":
        headers["x-api-key"] = key
    else:
        headers["Authorization"] = f"Bearer {key}"
    for name, value in provider.extra_headers:
        headers[name] = value
    return headers


def fetch_models(provider: Provider, key: Optional[str] = None, timeout: float = 20.0) -> List[ModelInfo]:
    status, payload, _ = request_json(
        provider.model_url(),
        headers=_auth_headers(provider, key),
        timeout=timeout,
    )
    if status >= 400:
        raise RuntimeError(f"HTTP {status} from {provider.model_url()}")
    models = parse_models(provider, payload)
    if not models:
        raise RuntimeError(f"empty model list from {provider.model_url()}")
    return models


class Catalog:
    def __init__(self, cache_path: Optional[Path] = None, ttl: float = 900, zen_ttl: float = 300):
        self.cache_path = cache_path or (cache_dir() / "catalog.json")
        self.ttl = ttl
        self.zen_ttl = zen_ttl
        self.models: Dict[str, List[ModelInfo]] = {}
        self.fetched_at: Dict[str, float] = {}
        self.errors: Dict[str, str] = {}
        self._lock = threading.Lock()
        self._load()

    def _load(self) -> None:
        raw = read_json(self.cache_path, {}) or {}
        providers = raw.get("providers") if isinstance(raw, dict) else None
        if not isinstance(providers, dict):
            return
        for pid, entry in providers.items():
            if not isinstance(entry, dict):
                continue
            rows = entry.get("models") or []
            try:
                self.models[pid] = [ModelInfo.from_dict(row) for row in rows if isinstance(row, dict)]
                self.fetched_at[pid] = float(entry.get("fetched_at") or 0)
            except Exception:
                continue

    def save(self) -> None:
        with self._lock:
            payload = {
                "providers": {
                    pid: {
                        "fetched_at": self.fetched_at.get(pid, 0),
                        "models": [m.to_dict() for m in models],
                        "error": self.errors.get(pid),
                    }
                    for pid, models in self.models.items()
                }
            }
        try:
            write_json(self.cache_path, payload, mode=0o600)
        except OSError:
            pass

    def ttl_for(self, pid: str) -> float:
        return self.zen_ttl if pid == "opencode" else self.ttl

    def stale(self, pid: str) -> bool:
        return (now() - self.fetched_at.get(pid, 0)) > self.ttl_for(pid)

    def get(self, pid: str) -> List[ModelInfo]:
        return list(self.models.get(pid) or [])

    def set(self, pid: str, models: List[ModelInfo]) -> None:
        with self._lock:
            self.models[pid] = models
            self.fetched_at[pid] = now()
            self.errors.pop(pid, None)

    def fallback(self, provider: Provider) -> List[ModelInfo]:
        rows = [ModelInfo(id=mid, provider=provider.id, name=mid, free=("free" in mid or mid == "big-pickle"), api_style=provider.api_style) for mid in provider.default_models]
        if provider.id == "opencode" and not rows:
            rows = [ModelInfo(id=mid, provider="opencode", name=mid, free=True, api_style="openai") for mid in ZEN_FREE_FALLBACK]
        return rows

    def refresh(self, provider: Provider, key: Optional[str] = None, force: bool = False, timeout: float = 20.0) -> bool:
        if not force and not self.stale(provider.id):
            return True
        try:
            models = fetch_models(provider, key=key, timeout=timeout)
        except Exception as exc:  # noqa: BLE001 - reported through state
            with self._lock:
                self.errors[provider.id] = str(exc)
            if not self.models.get(provider.id):
                self.models[provider.id] = self.fallback(provider)
                self.fetched_at[provider.id] = now()
            return False
        # keep stable ordering: free first, then name
        models.sort(key=lambda m: (not m.free, m.id.lower()))
        self.set(provider.id, models)
        return True

    def all_models(self, keys_by_provider: Dict[str, List[str]], force: bool = False) -> List[ModelInfo]:
        targets: List[Provider] = []
        for provider in PROVIDERS.values():
            keys = keys_by_provider.get(provider.id) or []
            if keys or provider.keyless or provider.public_catalog:
                targets.append(provider)
        if not targets:
            return []
        workers = min(8, len(targets))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {}
            for provider in targets:
                key = (keys_by_provider.get(provider.id) or [None])[0]
                futures[pool.submit(self.refresh, provider, key, force, 15.0)] = provider
            for future in as_completed(futures):
                try:
                    future.result()
                except Exception:
                    pass
        result: List[ModelInfo] = []
        for provider in targets:
            models = self.models.get(provider.id) or self.fallback(provider)
            result.extend(models)
        return result

    def find(self, model_id: str) -> List[ModelInfo]:
        hits: List[ModelInfo] = []
        for models in self.models.values():
            for model in models:
                if model.id == model_id:
                    hits.append(model)
        return hits


def build_upstream_request(
    provider: Provider,
    key: Optional[str],
    body: Dict[str, Any],
    model: ModelInfo,
    endpoint: Optional[str] = None,
) -> Tuple[str, Dict[str, str], bytes]:
    """Return (url, headers, payload) for one upstream attempt."""
    style = model.api_style or provider.api_style
    base = (endpoint or model.endpoint or provider.base_url).rstrip("/")
    headers = _auth_headers(provider, key)
    payload = dict(body)
    payload.pop("provider", None)
    for internal in ("urai_provider", "urai_session", "urai_key"):
        payload.pop(internal, None)
    payload["model"] = model.id

    if style == "anthropic":
        if base.endswith("/messages"):
            url = base
        else:
            url = base + "/messages"
        headers["Content-Type"] = "application/json"
        headers.setdefault("anthropic-version", "2023-06-01")
        converted = adapters.openai_to_anthropic(payload)
        return url, headers, json.dumps(converted).encode("utf-8")

    if base.endswith("/chat/completions"):
        url = base
    else:
        url = base + "/chat/completions"
    headers["Content-Type"] = "application/json"
    return url, headers, json.dumps(payload).encode("utf-8")


def provider_summary() -> List[Dict[str, Any]]:
    rows = []
    for provider in sorted(PROVIDERS.values(), key=lambda p: p.detect_order):
        rows.append(
            {
                "id": provider.id,
                "name": provider.name,
                "base_url": provider.base_url,
                "api_style": provider.api_style,
                "auth": provider.auth,
                "keyless": provider.keyless,
                "key_prefixes": list(provider.key_prefixes),
                "docs": provider.docs,
                "default_models": list(provider.default_models),
            }
        )
    return rows


def key_hint(provider: Provider) -> str:
    if provider.keyless:
        return "(no key needed)"
    if provider.key_prefixes:
        return " or ".join(f"{p}..." for p in provider.key_prefixes)
    return "any provider key"


def probe_key(provider: Provider, key: str, timeout: float = 12.0) -> bool:
    """Validate a key with a 1-token chat request (models endpoints can be public)."""
    body = {
        "model": (provider.default_models or ("",))[0],
        "messages": [{"role": "user", "content": "ping"}],
        "max_tokens": 1,
        "stream": False,
    }
    if not body["model"]:
        return False
    model = ModelInfo(id=body["model"], provider=provider.id, api_style=provider.api_style)
    try:
        url, headers, payload = build_upstream_request(provider, key, body, model)
        resp = http_request(url, method="POST", headers=headers, body=payload, timeout=timeout)
        try:
            resp.read()
        finally:
            resp.close()
        return 200 <= resp.status < 300
    except Exception:
        return False
