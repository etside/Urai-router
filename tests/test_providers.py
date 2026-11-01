import json

from urai_router.adapters import anthropic_stream_to_openai, anthropic_to_openai, openai_to_anthropic
from urai_router.providers import ModelInfo, PROVIDERS, build_upstream_request, parse_models, provider_id


ZEN_SAMPLE = {
    "object": "list",
    "data": [
        {
            "id": "deepseek-v4.1-flash-free",
            "name": "DeepSeek V4.1 Flash Free",
            "cost": {"text": {"input": {"price": 0, "cache": {"read": 0}}, "output": {"price": 0}}},
            "api": {
                "chat-completions": {
                    "base_url": "https://opencode.ai/inference/openai/v1",
                    "sdk": {"aisdk": "@ai-sdk/openai-compatible"},
                }
            },
            "limit": {"context": 200000, "output": 32000},
            "capabilities": {"tools": {"supported": True}},
        },
        {
            "id": "claude-sonnet-4-5",
            "name": "Claude Sonnet 4.5",
            "cost": {"text": {"input": {"price": 3}, "output": {"price": 15}}},
            "api": {
                "messages": {
                    "base_url": "https://opencode.ai/inference/anthropic/v1",
                    "sdk": {"aisdk": "@ai-sdk/anthropic"},
                }
            },
            "limit": {"context": 1000000, "output": 64000},
            "capabilities": {"tools": {"supported": True}},
        },
    ],
}


def test_zen_catalog_parse():
    models = parse_models(PROVIDERS["opencode"], ZEN_SAMPLE)
    assert models[0].qualified == "opencode/deepseek-v4.1-flash-free"
    assert models[0].free is True
    assert models[0].api_style == "openai"
    assert models[0].endpoint.endswith("/inference/openai/v1")
    assert models[0].context == 200000
    assert models[1].free is False
    assert models[1].api_style == "anthropic"
    assert models[1].endpoint.endswith("/inference/anthropic/v1")


def test_openrouter_pricing_normalised():
    payload = {
        "data": [
            {
                "id": "vendor/free:free",
                "name": "Free",
                "pricing": {"prompt": "0", "completion": "0"},
                "context_length": 128000,
                "supported_parameters": ["tools"],
            },
            {
                "id": "vendor/paid",
                "pricing": {"prompt": "0.000001", "completion": "0.000002"},
            },
        ]
    }
    models = parse_models(PROVIDERS["openrouter"], payload)
    assert models[0].free is True
    assert models[1].free is False
    assert abs(models[1].input_price - 1.0) < 1e-9
    assert abs(models[1].output_price - 2.0) < 1e-9


def test_provider_aliases():
    assert provider_id("zen") == "opencode"
    assert provider_id("OpenCode Zen") == "opencode"
    assert provider_id("claude") == "anthropic"
    assert provider_id("nope") is None


def test_build_request_openai_and_anthropic():
    model = ModelInfo(id="m", provider="fake", api_style="openai")
    url, headers, payload = build_upstream_request(
        PROVIDERS["openai"], "sk-test", {"model": "m", "messages": [], "stream": True}, model
    )
    assert url == "https://api.openai.com/v1/chat/completions"
    assert headers["Authorization"] == "Bearer sk-test"
    assert json.loads(payload)["stream"] is True

    anthropic_model = ModelInfo(id="claude", provider="anthropic", api_style="anthropic")
    body = {
        "model": "claude",
        "messages": [
            {"role": "system", "content": "be brief"},
            {"role": "user", "content": "hi"},
        ],
        "max_tokens": 16,
        "tools": [
            {
                "type": "function",
                "function": {"name": "lookup", "description": "d", "parameters": {"type": "object", "properties": {}}},
            }
        ],
    }
    url, headers, payload = build_upstream_request(PROVIDERS["anthropic"], "sk-ant-test", body, anthropic_model)
    assert url == "https://api.anthropic.com/v1/messages"
    assert headers["x-api-key"] == "sk-ant-test"
    parsed = json.loads(payload)
    assert parsed["system"] == "be brief"
    assert parsed["tools"][0]["name"] == "lookup"
    assert parsed["max_tokens"] == 16


def test_openai_to_anthropic_tool_roundtrip():
    body = {
        "model": "claude",
        "messages": [
            {"role": "user", "content": "weather?"},
            {
                "role": "assistant",
                "content": "checking",
                "tool_calls": [
                    {"id": "t1", "type": "function", "function": {"name": "weather", "arguments": '{"city":"Dhaka"}'}}
                ],
            },
            {"role": "tool", "tool_call_id": "t1", "content": "31C"},
        ],
        "max_tokens": 32,
    }
    converted = openai_to_anthropic(body)
    assert converted["messages"][1]["content"][1]["type"] == "tool_use"
    assert converted["messages"][2]["content"][0]["type"] == "tool_result"

    response = {
        "id": "msg_1",
        "model": "claude",
        "content": [
            {"type": "thinking", "thinking": "reason"},
            {"type": "text", "text": "sunny"},
            {"type": "tool_use", "id": "t2", "name": "forecast", "input": {"days": 2}},
        ],
        "stop_reason": "tool_use",
        "usage": {"input_tokens": 10, "output_tokens": 5},
    }
    openai = anthropic_to_openai(response)
    message = openai["choices"][0]["message"]
    assert message["content"] == "sunny"
    assert message["reasoning_content"] == "reason"
    assert message["tool_calls"][0]["function"]["name"] == "forecast"
    assert openai["choices"][0]["finish_reason"] == "tool_calls"
    assert openai["usage"]["total_tokens"] == 15


def test_anthropic_stream_mapping():
    events = [
        {"type": "message_start", "message": {"id": "msg", "model": "claude", "usage": {"input_tokens": 3}}},
        {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "hi"}},
        {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 1}},
        {"type": "message_stop"},
    ]
    chunks = list(anthropic_stream_to_openai(iter(events), "claude"))
    joined = b"".join(chunks).decode()
    assert '"content":"hi"' in joined
    assert '"finish_reason":"stop"' in joined
    assert "data: [DONE]" in joined
