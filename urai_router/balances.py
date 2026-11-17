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
