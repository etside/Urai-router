"""Shared fixtures: a tiny fake OpenAI-compatible upstream."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List

import pytest

from urai_router.config import Config
from urai_router.keys import KeyStore
from urai_router.router import Router
from urai_router.util import deep_merge, write_json


class FakeUpstream(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    requests: List[Dict[str, Any]] = []

    def log_message(self, fmt: str, *args: Any) -> None:
        pass

    def _key(self) -> str:
        auth = self.headers.get("authorization", "")
        if auth.lower().startswith("bearer "):
            return auth[7:]
        return self.headers.get("x-api-key", "")

    def do_GET(self) -> None:  # noqa: N802
        if self.path.rstrip("/").endswith("/models"):
            payload = {
                "object": "list",
                "data": [
                    {"id": "test-model", "name": "Test Model"},
                    {"id": "free-model", "name": "Free Model", "pricing": {"prompt": "0", "completion": "0"}},
                ],
            }
            body = json.dumps(payload).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(404)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("content-length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        key = self._key()
        self.__class__.requests.append({"key": key, "body": body, "path": self.path})
        model = body.get("model")
        if model == "bad-model":
            self._json(400, {"error": {"message": "bad model", "type": "invalid_request_error"}})
            return
        if key == "bad":
            self._json(401, {"error": {"message": "invalid key", "type": "authentication_error"}})
            return
        if key == "rate":
            self.send_response(429)
            payload = json.dumps({"error": {"message": "rate limited"}}).encode()
            self.send_header("Retry-After", "1")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        if key == "server":
            self._json(500, {"error": {"message": "upstream boom"}})
            return
        if body.get("stream"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Connection", "close")
            self.end_headers()
            for token in ("he", "llo"):
                chunk = json.dumps(
                    {
                        "id": "chatcmpl-test",
                        "object": "chat.completion.chunk",
                        "model": model,
                        "choices": [{"index": 0, "delta": {"content": token}, "finish_reason": None}],
                    }
                )
                self.wfile.write(f"data: {chunk}\n\n".encode())
                self.wfile.flush()
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
            return
        self._json(
            200,
            {
                "id": "chatcmpl-test",
                "object": "chat.completion",
                "model": model,
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": f"ok:{key}"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            },
            headers={"x-upstream-key": key},
        )

    def _json(self, status: int, payload: Dict[str, Any], headers: Dict[str, str] | None = None) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture()
def fake_upstream():
    FakeUpstream.requests = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), FakeUpstream)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_address[1]}"
    yield {"url": url, "server": server, "requests": FakeUpstream.requests}
    server.shutdown()
    server.server_close()


@pytest.fixture()
def tmp_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    (tmp_path / "home").mkdir()
    return tmp_path


@pytest.fixture()
def fake_router(tmp_env: Path, fake_upstream):
    config_path = tmp_env / "config.json"
    write_json(
        config_path,
        {
            "providers": {
                "fake": {
                    "base_url": fake_upstream["url"],
                    "api_style": "openai",
                    "name": "Fake",
                    "default_models": ["test-model"],
                }
            },
            "catalog_ttl": 0,
            "request_timeout": 10,
            "max_attempts": 6,
        },
    )
    cfg = Config(data=deep_merge({}, json.loads(config_path.read_text())), path=config_path)
    keys_path = tmp_env / "keys.txt"
    keys_path.write_text("fake:good # label=good\n")
    store = KeyStore(keys_path, cfg, state_path=tmp_env / "keys-state.json")
    router = Router(cfg, store)
    # Isolate the registry so catalogue tests never touch the public network.
    from urai_router import providers as provider_mod

    saved = dict(provider_mod.PROVIDERS)
    fake = provider_mod.PROVIDERS["fake"]
    provider_mod.PROVIDERS.clear()
    provider_mod.PROVIDERS["fake"] = fake
    try:
        yield {"router": router, "cfg": cfg, "store": store, "keys_path": keys_path, "upstream": fake_upstream}
    finally:
        provider_mod.PROVIDERS.clear()
        provider_mod.PROVIDERS.update(saved)
