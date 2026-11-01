"""Local OpenAI-compatible HTTP proxy with automatic key rotation."""

from __future__ import annotations

import json
import os
import signal
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import parse_qs, urlsplit

from .config import Config
from .keys import KeyStore
from .providers import provider_summary
from .router import Router
from .util import VERSION, json_bytes, mask_secret, now, read_json, state_dir, write_json

MAX_BODY = 32 * 1024 * 1024


class ProxyServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple, router: Router, cfg: Config):
        self.router = router
        self.cfg = cfg
        super().__init__(address, ProxyHandler)


class ProxyHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = f"urai-router/{VERSION}"

    @property
    def router(self) -> Router:
        return self.server.router  # type: ignore[attr-defined]

    @property
    def cfg(self) -> Config:
        return self.server.cfg  # type: ignore[attr-defined]

    def log_message(self, fmt: str, *args: Any) -> None:
        if self.cfg.get("proxy.log_requests", True):
            sys.stderr.write(f"[urai-router] {self.address_string()} {fmt % args}\n")

    # ------------------------------------------------------------- plumbing
    def _cors(self) -> Dict[str, str]:
        if not self.cfg.get("proxy.cors", True):
            return {}
        return {
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
            "Access-Control-Allow-Headers": "Authorization, Content-Type, X-Urai-Token, Accept",
        }

    def _auth_ok(self) -> bool:
        token = str(self.cfg.get("token") or "")
        if not token:
            return True
        supplied = self.headers.get("x-urai-token", "")
        auth = self.headers.get("authorization", "")
        if auth.lower().startswith("bearer "):
            supplied = auth[7:].strip()
        return supplied == token

    def _send(self, status: int, body: bytes, headers: Optional[Dict[str, str]] = None) -> None:
        self.send_response(status)
        merged = {"Content-Type": "application/json", "Content-Length": str(len(body)), "Connection": "close"}
        merged.update(self._cors())
        for name, value in (headers or {}).items():
            if value is not None and name.lower() not in ("content-length", "transfer-encoding", "connection"):
                merged[name] = value
        for name, value in merged.items():
            self.send_header(name, value)
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass
        self.close_connection = True

    def _json(self, status: int, value: Any, headers: Optional[Dict[str, str]] = None) -> None:
        self._send(status, json_bytes(value), headers)

    def _error(self, status: int, message: str, err_type: str = "invalid_request_error", code: Optional[str] = None) -> None:
        payload: Dict[str, Any] = {"error": {"message": message, "type": err_type}}
        if code:
            payload["error"]["code"] = code
        self._json(status, payload)

    def _read_body(self) -> Optional[Dict[str, Any]]:
        try:
            length = int(self.headers.get("content-length") or 0)
        except ValueError:
            self._error(411, "invalid Content-Length", code="length_required")
            return None
        if length <= 0:
            self._error(400, "empty request body", code="empty_body")
            return None
        if length > MAX_BODY:
            self._error(413, "request body too large", code="payload_too_large")
            return None
        raw = self.rfile.read(length)
        try:
            value = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            self._error(400, "request body is not valid JSON", code="invalid_json")
            return None
        if not isinstance(value, dict):
            self._error(400, "request body must be a JSON object", code="invalid_json")
            return None
        return value

    def do_OPTIONS(self) -> None:  # noqa: N802
        self._send(204, b"")

    # ------------------------------------------------------------------ GET
    def do_GET(self) -> None:  # noqa: N802
        parsed = urlsplit(self.path)
        path = parsed.path.rstrip("/") or "/"
        if path == "/health":
            self._json(
                200,
                {
                    "status": "ok",
                    "service": "urai-router",
                    "version": VERSION,
                    "keys": len(self.router.store.keys),
                    "providers": self.router.store.providers(),
                    "uptime": round(time.time() - STARTED_AT, 1),
                },
            )
            return
        if path == "/":
            self._json(
                200,
                {
                    "service": "urai-router",
                    "version": VERSION,
                    "openai_base_url": "/v1",
                    "endpoints": [
                        "/health",
                        "/v1/models",
                        "/v1/chat/completions",
                        "/v1/urai/stats",
                        "/v1/urai/keys",
                        "/v1/urai/providers",
                        "/v1/urai/explain",
                    ],
                    "docs": "https://github.com/etside/Urai-router",
                },
            )
            return
        if not self._auth_ok():
            self._error(401, "missing or invalid local router token", "authentication_error", "invalid_token")
            return
        if path == "/v1/models":
            self._models(parsed.query)
            return
        if path in ("/v1/urai/stats", "/v1/urai/health"):
            self._json(200, self.router.status())
            return
        if path == "/v1/urai/keys":
            self._json(200, self.router.store.stats())
            return
        if path == "/v1/urai/providers":
            self._json(200, {"providers": provider_summary(), "config": self.cfg.redacted()})
            return
        if path == "/v1/urai/explain":
            query = parse_qs(parsed.query)
            model = (query.get("model") or [None])[0]
            self._json(200, self.router.explain(model))
            return
        if path == "/v1/urai/config":
            self._json(200, self.cfg.redacted())
            return
        self._error(404, f"unknown endpoint {path}", code="not_found")

    def _models(self, query: str) -> None:
        params = parse_qs(query)
        free_only = (params.get("free") or params.get("free_only") or ["0"])[0].lower() in ("1", "true", "yes")
        force = (params.get("refresh") or ["0"])[0].lower() in ("1", "true", "yes")
        provider_filter = (params.get("provider") or [None])[0]
        try:
            models = self.router.usable_models(force=force)
        except Exception as exc:  # noqa: BLE001
            self._error(502, f"could not build model catalogue: {exc}", "upstream_error", "catalog_error")
            return
        data = []
        for model in models:
            if free_only and not model.free:
                continue
            if provider_filter and model.provider != provider_filter:
                continue
            data.append(
                {
                    "id": model.qualified,
                    "object": "model",
                    "created": int(model.raw.get("created") or 0) if isinstance(model.raw, dict) else 0,
                    "owned_by": model.provider,
                    "name": model.name,
                    "free": model.free,
                    "context_length": model.context,
                    "max_output_tokens": model.output,
                    "pricing": {
                        "input_per_million": model.input_price,
                        "output_per_million": model.output_price,
                    },
                    "capabilities": {"tools": model.tools},
                }
            )
        data.append({"id": "urai/auto", "object": "model", "owned_by": "urai", "name": "Urai auto (prefer free)", "free": True})
        data.append({"id": "urai/free", "object": "model", "owned_by": "urai", "name": "Urai free-only", "free": True})
        self._json(200, {"object": "list", "data": data})

    # ----------------------------------------------------------------- POST
    def do_POST(self) -> None:  # noqa: N802
        if not self._auth_ok():
            self._error(401, "missing or invalid local router token", "authentication_error", "invalid_token")
            return
        path = urlsplit(self.path).path.rstrip("/") or "/"
        if path == "/v1/chat/completions":
            body = self._read_body()
            if body is None:
                return
            body.setdefault("stream", False)
            result = self.router.chat(body)
            self._write_result(result)
            return
        if path == "/v1/urai/reload":
            body = self._read_body() or {}
            changed = self.router.store.reload(force=True)
            if body.get("catalog", True):
                self.router.refresh_catalog(force=bool(body.get("force")))
            self._json(200, {"ok": True, "keys_reloaded": changed, "keys": len(self.router.store.keys)})
            return
        if path == "/v1/urai/test":
            body = self._read_body()
            if body is None:
                return
            prompt = body.pop("prompt", "Reply with the single word: pong")
            model = body.get("model")
            payload = {
                "model": model or self.router.cfg.get("default_model", "auto"),
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": int(body.get("max_tokens") or 32),
                "stream": False,
            }
            self._write_result(self.router.chat(payload))
            return
        if path in ("/v1/completions", "/v1/responses", "/v1/embeddings"):
            self._error(
                501,
                f"{path} is not exposed by Urai Router yet; use /v1/chat/completions.",
                "not_implemented",
                "unsupported_endpoint",
            )
            return
        self._error(404, f"unknown endpoint {path}", code="not_found")

    def _write_result(self, result: Any) -> None:
        self.send_response(result.status)
        headers = {
            "Content-Type": "application/json",
            "Connection": "close",
            "X-Urai-Version": VERSION,
        }
        headers.update(self._cors())
        headers.update(result.headers or {})
        for name, value in headers.items():
            if value is not None and name.lower() not in ("content-length", "transfer-encoding", "connection"):
                self.send_header(name, value)
        if result.stream is None:
            self.send_header("Content-Length", str(len(result.body or b"")))
        self.end_headers()
        self.close_connection = True
        try:
            if result.stream is not None:
                for chunk in result.stream:
                    self.wfile.write(chunk)
                    self.wfile.flush()
            else:
                self.wfile.write(result.body or b"")
        except (BrokenPipeError, ConnectionResetError):
            pass


STARTED_AT = time.time()
RUNTIME_FILE = "proxy.json"


def runtime_path() -> Path:
    return state_dir() / RUNTIME_FILE


def proxy_status(cfg: Optional[Config] = None) -> Dict[str, Any]:
    info = read_json(runtime_path(), {}) or {}
    if not isinstance(info, dict) or not info.get("port"):
        return {"running": False}
    url = f"http://{info.get('host', '127.0.0.1')}:{info.get('port')}"
    try:
        import urllib.request

        with urllib.request.urlopen(url + "/health", timeout=2) as response:  # noqa: S310 - localhost
            payload = json.loads(response.read().decode("utf-8"))
        return {"running": True, "url": url, "pid": info.get("pid"), **payload}
    except Exception:
        return {"running": False, "stale": True, "url": url, "pid": info.get("pid")}


def serve(router: Router, cfg: Config, host: Optional[str] = None, port: Optional[int] = None) -> None:
    host = host or str(cfg.get("host", "127.0.0.1"))
    port = int(port if port is not None else cfg.get("port", 8787))
    server = ProxyServer((host, port), router, cfg)
    actual_port = server.server_address[1]
    url = f"http://{host}:{actual_port}"
    write_json(
        runtime_path(),
        {"pid": os.getpid(), "host": host, "port": actual_port, "url": url, "started_at": now(), "version": VERSION},
        mode=0o600,
    )
    sys.stderr.write(f"[urai-router] listening on {url}  keys={len(router.store.keys)}  file={router.store.path}\n")

    def _stop(signum: int, frame: Any) -> None:
        sys.stderr.write("[urai-router] shutting down\n")
        try:
            server.shutdown()
        except Exception:
            pass
        try:
            runtime_path().unlink()
        except OSError:
            pass

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, _stop)
        except (ValueError, OSError):
            pass
    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        server.server_close()
        try:
            info = read_json(runtime_path(), {}) or {}
            if info.get("pid") == os.getpid():
                runtime_path().unlink()
        except OSError:
            pass


def ensure_proxy(router: Router, cfg: Config) -> Dict[str, Any]:
    """Start the proxy in the background if it is not already running."""
    status = proxy_status(cfg)
    if status.get("running"):
        return status
    import subprocess

    log_path = state_dir() / "proxy.log"
    port = int(cfg.get("port", 8787))
    command = [sys.executable, "-m", "urai_router", "serve", "--port", str(port)]
    if cfg.path.exists():
        command += ["--config", str(cfg.path)]
    command += ["--keys", str(router.store.path)]
    with open(log_path, "ab") as log:
        process = subprocess.Popen(
            command,
            stdout=log,
            stderr=log,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
            close_fds=True,
        )
    deadline = time.time() + 10
    while time.time() < deadline:
        time.sleep(0.25)
        status = proxy_status(cfg)
        if status.get("running"):
            return status
        if process.poll() is not None:
            break
    return {"running": False, "error": f"proxy failed to start; see {log_path}", "pid": process.pid}
