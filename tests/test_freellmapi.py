"""FreeLLMAPI gateway integration: key detection, catalogue and requests."""

from __future__ import annotations

import json

from urai_router.providers import (
    PROVIDERS,
    build_upstream_request,
    configure_freellmapi,
    detect_by_prefix,
    parse_models,
)


FREELMAPI_SAMPLE = {
    "object": "list",
    "data": [
        {
            "id": "auto",
            "object": "model",
            "owned_by": "freellmapi",
            "name": "Auto (router picks the best available model)",
            "context_window": 200000,
            "context_length": 200000,
            "available": True,
            "unavailable_reason": None,
        },
        {
            "id": "auto:fast",
            "object": "model",
            "owned_by": "freellmapi",
            "name": "Auto: fast",
            "context_window": 131072,
            "context_length": 131072,
            "available": True,
            "unavailable_reason": None,
        },
        {
            "id": "gemini-2.5-flash",
            "object": "model",
            "owned_by": "google",
            "name": "Gemini 2.5 Flash",
            "context_window": 1048576,
            "context_length": 1048576,
            "available": True,
            "unavailable_reason": None,
            "execution_status": "ready",
            "supported_parameters": ["tools", "temperature", "response_format"],
        },
        {
            "id": "kimi-k3.1-pro",
            "object": "model",
            "owned_by": "moonshot",
            "name": "Kimi K3.1 Pro",
            "context_window": 262144,
            "available": False,
            "unavailable_reason": "no_key",
            "execution_status": "needsKey",
            "supported_parameters": [],
        },
    ],
}


def test_freellmapi_catalog_parse():
    models = parse_models(PROVIDERS["freellmapi"], FREELMAPI_SAMPLE)
    by_id = {model.id: model for model in models}

    # Router entries carry the free pool; auto is the single best target.
    assert by_id["auto"].free is True
    assert by_id["auto:fast"].free is True
    assert by_id["auto"].context == 200000
    assert by_id["auto"].api_style == "openai"

    # Individual catalog rows stay addressable but do not flood model=auto.
    assert by_id["gemini-2.5-flash"].free is False
    assert by_id["gemini-2.5-flash"].tools is True
    assert by_id["gemini-2.5-flash"].context == 1048576

    # A model with no upstream key is not advertised as free.
    assert by_id["kimi-k3.1-pro"].free is False
    assert by_id["kimi-k3.1-pro"].raw["execution_status"] == "needsKey"


def test_freellmapi_key_autodetected():
    key = "freellmapi-" + "a" * 48
    assert detect_by_prefix(key) == ["freellmapi"]


def test_freellmapi_request_shape():
    provider = PROVIDERS["freellmapi"]
    model = parse_models(provider, FREELMAPI_SAMPLE)[0]
    url, headers, payload = build_upstream_request(
        provider,
        "freellmapi-" + "b" * 48,
        {"model": "auto", "messages": [{"role": "user", "content": "hi"}]},
        model,
    )
    body = json.loads(payload)
    assert url == provider.base_url.rstrip("/") + "/chat/completions"
    assert headers["Authorization"].startswith("Bearer freellmapi-")
    assert body["model"] == "auto"


def test_freellmapi_config_rebuilds_models_url():
    original = PROVIDERS["freellmapi"]
    try:
        configure_freellmapi(
            {"base_url": "http://192.168.1.50:3001/v1/", "models_query": "execution_status=ready"}
        )
        updated = PROVIDERS["freellmapi"]
        assert updated.base_url == "http://192.168.1.50:3001/v1"
        assert updated.models_url == (
            "http://192.168.1.50:3001/v1/models?execution_status=ready"
        )
        assert updated.model_url() == updated.models_url
    finally:
        PROVIDERS["freellmapi"] = original


def test_freellmapi_cli_context_honours_config(tmp_path):
    import argparse

    from urai_router import cli
    from urai_router.config import freellmapi_settings

    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps({"freellmapi": {"base_url": "http://127.0.0.1:3011/v1"}}))
    keys_path = tmp_path / "keys.txt"
    keys_path.write_text("")

    original = PROVIDERS["freellmapi"]
    try:
        args = argparse.Namespace(
            config=str(cfg_path),
            keys=str(keys_path),
            no_detect=True,
            state_dir=None,
            host=None,
            port=None,
            token=None,
        )
        cfg, _store = cli._context(args)
        assert freellmapi_settings(cfg)["base_url"] == "http://127.0.0.1:3011/v1"
        # Commands that never build a Router (keys balance, doctor) still see it.
        assert PROVIDERS["freellmapi"].base_url == "http://127.0.0.1:3011/v1"
        assert PROVIDERS["freellmapi"].models_url.endswith("?execution_status=ready")
    finally:
        PROVIDERS["freellmapi"] = original


def test_freellmapi_usage_ledger(monkeypatch):
    from urai_router import balances

    payload = {
        "month": "2026-11",
        "resetsAt": "2026-12-01T00:00:00.000Z",
        "requests": 152,
        "tokens": 104000000,
        "byPlatform": [{"platform": "google", "requests": 90, "tokens": 80000000}],
    }

    class FakeResponse:
        def read(self):
            return json.dumps(payload).encode()

        def close(self):
            pass

    monkeypatch.setattr(balances, "http_request", lambda *args, **kwargs: FakeResponse())
    row = balances.probe_balance(PROVIDERS["freellmapi"], "freellmapi-" + "c" * 48)
    assert row["ok"] is True
    assert row["balance"] == 104000000
    assert row["requests"] == 152
    assert row["month"] == "2026-11"
    assert "tokens=104000000" in row["summary"]
    assert row["by_platform"][0]["platform"] == "google"
