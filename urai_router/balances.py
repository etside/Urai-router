"""Read-only balance probes for paid providers.

Never logs, echoes or persists a key value — only masked keys and numeric
balance fields are returned to callers.
"""

from __future__ import annotations

import json
from typing import Any, Dict

from .providers import Provider
from .util import http_request

# Provider id -> balance path relative to its OpenAI-compatible base URL.
BALANCE_PATHS: Dict[str, str] = {
    # Relative to Provider.base_url, which already ends in /v1.
    "bai": "/balance",
}


def probe_balance(provider: Provider, key_value: str, timeout: float = 12.0) -> Dict[str, Any]:
    if provider.id == "freellmapi":
        return _probe_freellmapi_usage(provider, key_value, timeout)
    path = BALANCE_PATHS.get(provider.id)
    if not path:
        return {
            "ok": False, "supported": False, "provider": provider.id,
            "error": "no balance endpoint defined for this provider",
        }
    url = provider.base_url.rstrip("/") + path
    try:
        resp = http_request(
            url, method="GET",
            headers={"Authorization": f"Bearer {key_value}"},
            timeout=timeout,
        )
        try:
            raw = resp.read()
        finally:
            resp.close()
        payload = json.loads(raw.decode("utf-8", "replace"))
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "supported": True, "provider": provider.id,
                "endpoint": url, "error": f"{type(exc).__name__}: {exc}"}

    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        return {"ok": False, "supported": True, "provider": provider.id,
                "endpoint": url, "error": "unexpected balance response shape"}
    result: Dict[str, Any] = {
        "ok": bool(payload.get("success", True)),
        "supported": True,
        "provider": provider.id,
        "endpoint": url,
        "balance": data.get("personal_balance", data.get("balance")),
        "unit": "provider units",  # the gateway does not publish a currency label
        "status": data.get("active_status"),
        "account_type": data.get("api_key_type"),
    }
    if result["balance"] is None:
        result["ok"] = False
        result["error"] = "balance field missing"
    return result


def _probe_freellmapi_usage(provider: Provider, key_value: str, timeout: float = 12.0) -> Dict[str, Any]:
    """Read the gateway's monthly token ledger (``GET /v1/usage``).

    The unified key is the only credential here; upstream provider keys never
    leave the FreeLLMAPI process, so urai-router only ever sees aggregate
    request/token counts.
    """
    url = provider.base_url.rstrip("/") + "/usage"
    try:
        resp = http_request(
            url,
            method="GET",
            headers={"Authorization": f"Bearer {key_value}", "Accept": "application/json"},
            timeout=timeout,
        )
        try:
            raw = resp.read()
        finally:
            resp.close()
        payload = json.loads(raw.decode("utf-8", "replace"))
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "supported": True,
            "provider": provider.id,
            "endpoint": url,
            "error": f"{type(exc).__name__}: {exc}",
        }

    if not isinstance(payload, dict):
        return {
            "ok": False,
            "supported": True,
            "provider": provider.id,
            "endpoint": url,
            "error": "unexpected usage response shape",
        }
    tokens = payload.get("tokens")
    requests = payload.get("requests")
    month = payload.get("month") or ""
    result: Dict[str, Any] = {
        "ok": tokens is not None or requests is not None,
        "supported": True,
        "provider": provider.id,
        "endpoint": url,
        "balance": tokens,
        "unit": "tokens this month",
        "requests": requests,
        "month": month,
        "resets_at": payload.get("resetsAt"),
        "by_platform": payload.get("byPlatform") or [],
    }
    if not result["ok"]:
        result["error"] = "usage response missing tokens/requests"
    result["summary"] = (
        f"tokens={tokens if tokens is not None else '?'} "
        f"requests={requests if requests is not None else '?'} "
        f"month={month or '?'}"
    )
    return result
