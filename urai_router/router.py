"""The routing core: model selection, key failover and response adapters."""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, Iterator, List, Optional, Set, Tuple

from . import adapters
from .config import Config
from .keys import Key, KeyStore
from .providers import (
    PROVIDERS,
    Catalog,
    ModelInfo,
    Provider,
    build_upstream_request,
    provider_id,
)
from .util import http_request, iter_sse_json, json_bytes, now

AUTO_NAMES = {"", "auto", "best", "default", "urai/auto", "urai-router/auto"}
FREE_NAMES = {"free", "urai/free", "free-only", "free_only"}

AUTO_RANK = [
    "deepseek-v4.1-flash-free",
    "glm-5.3-flash-free",
    "big-pickle",
    "mimo-v2.6-flash-free",
    "nemotron-3.5-lightning-free",
    "beam-free",
    "flint-40b-free",
]

RETRYABLE = {401, 403, 408, 409, 425, 429, 500, 502, 503, 504, 529}


def openai_error(status: int, message: str, err_type: str = "invalid_request_error", code: Optional[str] = None) -> Tuple[int, bytes]:
    payload: Dict[str, Any] = {"error": {"message": message, "type": err_type}}
    if code:
        payload["error"]["code"] = code
    return status, json_bytes(payload)


@dataclass
class ChatResult:
    status: int
    headers: Dict[str, str] = field(default_factory=dict)
    body: bytes = b""
    stream: Optional[Iterable[bytes]] = None
    provider: str = ""
    model: str = ""
    key_label: str = ""
    attempts: List[Dict[str, Any]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300


@dataclass
class RouteTarget:
    provider: Provider
    model: ModelInfo


class Router:
    def __init__(self, cfg: Config, store: KeyStore, catalog: Optional[Catalog] = None):
        self.cfg = cfg
        self.store = store
        from .providers import apply_provider_overrides

        apply_provider_overrides(cfg.provider_overrides)
        self.catalog = catalog or Catalog(ttl=float(cfg.get("catalog_ttl", 900)), zen_ttl=float(cfg.get("zen_ttl", 300)))
        self._lock = threading.RLock()
        self._detection_done = False

    # ------------------------------------------------------------- catalogue
    def _keys_by_provider(self) -> Dict[str, List[str]]:
        result: Dict[str, List[str]] = {}
        for key in self.store.keys:
            result.setdefault(key.provider, []).append(key.value)
        return result

    def ensure_detection(self) -> None:
        if self._detection_done:
            return
        self._detection_done = True
        if not self.cfg.get("detect_unknown_keys", True):
            return
        if any(key.provider == "unknown" for key in self.store.keys):
            try:
                self.store.detect_unknown()
            except Exception:
                pass

    def refresh_catalog(self, force: bool = False) -> int:
        self.ensure_detection()
        self.store.maybe_reload()
        models = self.catalog.all_models(self._keys_by_provider(), force=force)
        self.catalog.save()
        return len(models)

    def all_models(self, force: bool = False) -> List[ModelInfo]:
        self.refresh_catalog(force=force)
        seen: Set[str] = set()
        result: List[ModelInfo] = []
        for provider in PROVIDERS.values():
            for model in self.catalog.get(provider.id) or self.catalog.fallback(provider):
                key = f"{provider.id}/{model.id}"
                if key in seen:
                    continue
                seen.add(key)
                result.append(model)
        result.sort(key=lambda m: (not m.free, PROVIDERS[m.provider].detect_order if m.provider in PROVIDERS else 99, m.id.lower()))
        return result

    def free_models(self, force: bool = False) -> List[ModelInfo]:
        return [m for m in self.all_models(force=force) if m.free]

    def usable_models(self, force: bool = False) -> List[ModelInfo]:
        """Models this machine can actually call (key present, or free/public)."""
        return [m for m in self.all_models(force=force) if m.free or self._provider_has_key(m.provider)]

    # --------------------------------------------------------------- targets
    def _provider_has_key(self, pid: str) -> bool:
        provider = PROVIDERS.get(pid)
        if not provider:
            return False
        if provider.keyless:
            return True
        return bool(self.store.available_keys(pid)) or bool(self.store.keys_for(pid))

    def resolve(self, requested: Optional[str], free_only: bool = False) -> List[RouteTarget]:
        self.store.maybe_reload()
        name = (requested or "").strip()
        aliases = self.cfg.aliases
        if name in aliases:
            name = aliases[name]
        if free_only:
            name = "free"

        if name in FREE_NAMES:
            models = self.free_models()
            return self._targets_from_models(models)

        if name in AUTO_NAMES:
            models = self.free_models() if self.cfg.get("prefer_free", True) else []
            if not models:
                models = self.all_models()
            return self._targets_from_models(models)

        # provider/model
        if "/" in name:
            head, rest = name.split("/", 1)
            pid = provider_id(head)
            if pid:
                return self._targets_for(pid, rest)
        if ":" in name:
            head, rest = name.split(":", 1)
            pid = provider_id(head)
            if pid:
                return self._targets_for(pid, rest)

        hits = self.catalog.find(name)
        if not hits:
            # maybe a fully qualified id whose provider is unknown (e.g. org/model)
            hits = [m for m in self.all_models() if m.id == name or m.qualified == name]
        if hits:
            hits.sort(key=lambda m: (not m.free, self._provider_has_key(m.provider) is False, m.provider))
            return [RouteTarget(PROVIDERS[m.provider], m) for m in hits if m.provider in PROVIDERS]
        return []

    def _targets_for(self, pid: str, model_id: str) -> List[RouteTarget]:
        provider = PROVIDERS.get(pid)
        if not provider:
            return []
        matches = [m for m in self.catalog.get(pid) if m.id == model_id]
        if matches:
            return [RouteTarget(provider, matches[0])]
        return [RouteTarget(provider, ModelInfo(id=model_id, provider=pid, name=model_id, api_style=provider.api_style))]

    def _targets_from_models(self, models: List[ModelInfo]) -> List[RouteTarget]:
        usable = [m for m in models if m.provider in PROVIDERS and self._provider_has_key(m.provider)]
        local = {"ollama", "lmstudio", "llamacpp"}
        usable.sort(
            key=lambda m: (
                not m.free,
                m.provider != "opencode",
                m.provider in local,
                AUTO_RANK.index(m.id) if m.id in AUTO_RANK else 50,
                PROVIDERS[m.provider].detect_order,
                m.id.lower(),
            )
        )
        # keep one target per model id to avoid hammering identical ids
        seen: Set[str] = set()
        result: List[RouteTarget] = []
        for model in usable:
            if model.qualified in seen:
                continue
            seen.add(model.qualified)
            result.append(RouteTarget(PROVIDERS[model.provider], model))
        return result

    def explain(self, requested: Optional[str] = None) -> Dict[str, Any]:
        targets = self.resolve(requested or self.cfg.get("default_model", "auto"))
        rows = []
        for target in targets[:12]:
            pid = target.provider.id
            keys = self.store.available_keys(pid)
            rows.append(
                {
                    "provider": pid,
                    "model": target.model.id,
                    "qualified": target.model.qualified,
                    "free": target.model.free,
                    "api_style": target.model.api_style or target.provider.api_style,
                    "endpoint": target.model.endpoint or target.provider.base_url,
                    "keys_ready": len(keys),
                    "keys_total": len(self.store.keys_for(pid)),
                    "keyless": target.provider.keyless,
                }
            )
        return {
            "requested": requested,
            "prefer_free": self.cfg.get("prefer_free", True),
            "targets": rows,
            "keys_file": str(self.store.path),
            "aliases": self.cfg.aliases,
        }

    # ----------------------------------------------------------------- chat
    def chat(self, body: Dict[str, Any], requested_model: Optional[str] = None) -> ChatResult:
        if not isinstance(body, dict):
            status, payload = openai_error(400, "request body must be a JSON object")
            return ChatResult(status=status, body=payload)
        model_name = requested_model or body.get("model") or self.cfg.get("default_model", "auto")
        free_only = str(model_name) in FREE_NAMES
        targets = self.resolve(str(model_name), free_only=free_only)
        if not targets:
            status, payload = openai_error(
                404,
                f"no route for model {model_name!r}. Run `urai-router models` or use provider/model, e.g. opencode/deepseek-v4.1-flash-free.",
                code="model_not_found",
            )
            return ChatResult(status=status, body=payload, model=str(model_name))

        attempts: List[Dict[str, Any]] = []
        max_attempts = max(1, int(self.cfg.get("max_attempts", 8)))
        tried_keys: Dict[str, Set[str]] = {}
        last_error: Optional[Tuple[int, bytes]] = None
        saw_rate_limit = False
        saw_auth = False

        for target in targets:
            if len(attempts) >= max_attempts:
                break
            provider = target.provider
            keys = self.store.available_keys(provider.id)
            keyless = provider.keyless
            if not keys and not keyless:
                attempts.append(
                    {
                        "provider": provider.id,
                        "model": target.model.id,
                        "status": 0,
                        "error": "no key available",
                    }
                )
                continue
            key_pool: List[Optional[Key]] = list(keys) if keys else [None]
            endpoints = self._endpoints(target)
            for key in key_pool:
                if len(attempts) >= max_attempts:
                    break
                for endpoint in endpoints:
                    if key is not None:
                        if key.id in tried_keys.setdefault(provider.id, set()):
                            continue
                    result = self._attempt(target, key, endpoint, body, attempts)
                    if result is None:
                        continue
                    if result.ok:
                        return result
                    last_error = (result.status, result.body)
                    if result.status in (404, 405):
                        if endpoint is not endpoints[-1]:
                            continue  # endpoint metadata was wrong; try the provider default
                        return result
                    if key is not None:
                        tried_keys[provider.id].add(key.id)
                    if result.status == 429:
                        saw_rate_limit = True
                    if result.status in (401, 403):
                        saw_auth = True
                    if result.status not in RETRYABLE and result.status not in (404, 405):
                        return result  # request/model problem: another key will not help
                    break  # try next key for key-level errors

        if last_error:
            status, payload = last_error
            if status in (401, 403) and saw_auth:
                status = 503
                try:
                    parsed = json.loads(payload.decode("utf-8", "replace"))
                    detail = parsed.get("error", {}).get("message", "upstream rejected the key")
                except ValueError:
                    detail = "upstream rejected the key"
                payload = openai_error(
                    status,
                    f"all usable keys were rejected or are cooling down ({detail}). Check {self.store.path} and run `urai-router keys check`.",
                    err_type="authentication_error",
                    code="no_usable_key",
                )[1]
            elif status == 429:
                payload = openai_error(
                    429,
                    "all usable keys hit rate limits; cooldowns are active. Add more keys or retry later.",
                    err_type="rate_limit_error",
                    code="all_keys_cooling",
                )[1]
            return ChatResult(status=status, body=payload, provider="", model=str(model_name), attempts=attempts)

        status, payload = openai_error(
            503,
            f"no usable route for {model_name!r}. Check `urai-router doctor` and add keys to {self.store.path}.",
            err_type="service_unavailable",
            code="no_route",
        )
        return ChatResult(status=status, body=payload, attempts=attempts, model=str(model_name))

    def _endpoints(self, target: RouteTarget) -> List[Optional[str]]:
        bases: List[Optional[str]] = []
        if target.model.endpoint:
            bases.append(target.model.endpoint)
        provider_style = target.provider.api_style
        model_style = target.model.api_style or provider_style
        if model_style == provider_style and target.provider.base_url not in bases:
            bases.append(None)  # None means provider base URL
        if not bases:
            bases.append(None)
        return bases

    def _attempt(
        self,
        target: RouteTarget,
        key: Optional[Key],
        endpoint: Optional[str],
        body: Dict[str, Any],
        attempts: List[Dict[str, Any]],
    ) -> Optional[ChatResult]:
        provider = target.provider
        key_value = key.value if key is not None else None
        attempt_info = {
            "provider": provider.id,
            "model": target.model.id,
            "key": key.label if key and key.label else (key.id[:8] if key else "keyless"),
            "endpoint": endpoint or provider.base_url,
        }
        if key is not None and not key.available():
            attempt_info["error"] = "cooling"
            attempts.append(attempt_info)
            return None
        try:
            url, headers, payload = build_upstream_request(provider, key_value, body, target.model, endpoint=endpoint)
            resp = http_request(
                url,
                method="POST",
                headers=headers,
                body=payload,
                timeout=float(self.cfg.get("request_timeout", 120)),
            )
        except Exception as exc:  # noqa: BLE001
            if key is not None:
                self.store.mark_failure(key, "network")
            attempt_info["error"] = f"network: {exc}"
            attempt_info["status"] = 0
            attempts.append(attempt_info)
            return ChatResult(status=502, body=openai_error(502, f"network error contacting {provider.id}: {exc}")[1], attempts=attempts)

        status = resp.status
        attempt_info["status"] = status
        if 200 <= status < 300:
            if key is not None:
                self.store.mark_success(key)
            style = target.model.api_style or provider.api_style
            response_headers = {
                "x-urai-provider": provider.id,
                "x-urai-model": target.model.id,
                "x-urai-key": key.label if key and key.label else (key.id[:8] if key else "keyless"),
                "x-urai-attempts": str(len(attempts) + 1),
            }
            if body.get("stream"):
                response_headers["Content-Type"] = "text/event-stream; charset=utf-8"
                response_headers["Cache-Control"] = "no-cache"
                response_headers["Connection"] = "close"
                if style == "anthropic":
                    stream = self._anthropic_stream(resp, target.model.id)
                else:
                    stream = self._raw_stream(resp)
                return ChatResult(
                    status=status,
                    headers=response_headers,
                    stream=stream,
                    provider=provider.id,
                    model=target.model.id,
                    key_label=response_headers["x-urai-key"],
                    attempts=attempts + [attempt_info],
                )
            try:
                content = resp.read()
            finally:
                resp.close()
            if style == "anthropic":
                try:
                    converted = adapters.anthropic_to_openai(json.loads(content.decode("utf-8", "replace")), target.model.id)
                    content = json_bytes(converted)
                    response_headers["Content-Type"] = "application/json"
                except ValueError:
                    pass
            else:
                response_headers["Content-Type"] = resp.headers.get("content-type", "application/json")
            return ChatResult(
                status=status,
                headers=response_headers,
                body=content,
                provider=provider.id,
                model=target.model.id,
                key_label=response_headers["x-urai-key"],
                attempts=attempts + [attempt_info],
            )

        # failure: classify and decide whether another key may help
        try:
            error_body = resp.read()
        finally:
            resp.close()
        attempt_info["error"] = f"HTTP {status}"
        attempts.append(attempt_info)
        retry_after = _retry_after(resp.headers)
        if key is not None:
            if status in (401, 403):
                self.store.mark_failure(key, "invalid" if status == 401 else "forbidden")
            elif status == 429:
                self.store.mark_failure(key, "rate", retry_after=retry_after)
            elif status in (408, 425, 500, 502, 503, 504, 529):
                self.store.mark_failure(key, "server", retry_after=retry_after)
        if status in RETRYABLE:
            return ChatResult(status=status, body=error_body, attempts=attempts)
        return ChatResult(status=status, body=error_body, provider=provider.id, model=target.model.id, attempts=attempts)

    def _raw_stream(self, resp: Any) -> Iterator[bytes]:
        try:
            while True:
                chunk = resp.raw.read(8192)
                if not chunk:
                    break
                yield chunk
        finally:
            resp.close()

    def _anthropic_stream(self, resp: Any, model: str) -> Iterator[bytes]:
        try:
            yield from adapters.anthropic_stream_to_openai(iter_sse_json(resp), model)
        finally:
            resp.close()

    # --------------------------------------------------------------- status
    def status(self) -> Dict[str, Any]:
        stats = self.store.stats()
        catalog_rows = {}
        for pid, models in self.catalog.models.items():
            catalog_rows[pid] = {
                "count": len(models),
                "free": len([m for m in models if m.free]),
                "fetched_at": round(self.catalog.fetched_at.get(pid, 0), 1),
                "stale": self.catalog.stale(pid),
                "error": self.catalog.errors.get(pid),
            }
        return {
            "version": "0.1.0",
            "keys_file": stats["keys_file"],
            "keys_total": stats["total"],
            "providers_with_keys": stats["providers"],
            "unknown_keys": stats["unknown"],
            "catalog": catalog_rows,
            "prefer_free": self.cfg.get("prefer_free", True),
            "default_model": self.cfg.get("default_model", "auto"),
        }


def _retry_after(headers: Dict[str, str]) -> Optional[float]:
    raw = headers.get("retry-after") or headers.get("x-ratelimit-reset-requests")
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        return None
