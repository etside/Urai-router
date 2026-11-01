import http.client
import json
import threading
import urllib.error
import urllib.request

from urai_router.proxy import ProxyServer


def _start(router, cfg):
    server = ProxyServer(("127.0.0.1", 0), router, cfg)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, f"http://127.0.0.1:{server.server_address[1]}"


def _post(url, payload, headers=None, timeout=10):
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", **(headers or {})},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.headers, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.headers, exc.read()


def test_health_and_models(fake_router):
    server, url = _start(fake_router["router"], fake_router["cfg"])
    try:
        with urllib.request.urlopen(url + "/health", timeout=5) as response:
            health = json.loads(response.read())
        assert health["status"] == "ok"
        assert health["keys"] == 1
        with urllib.request.urlopen(url + "/v1/models", timeout=5) as response:
            models = json.loads(response.read())
        ids = {row["id"] for row in models["data"]}
        assert "fake/test-model" in ids
        assert "urai/auto" in ids
    finally:
        server.shutdown()
        server.server_close()


def test_chat_completion_and_headers(fake_router):
    server, url = _start(fake_router["router"], fake_router["cfg"])
    try:
        status, headers, body = _post(
            url + "/v1/chat/completions",
            {"model": "fake/test-model", "messages": [{"role": "user", "content": "hi"}]},
        )
        assert status == 200
        assert headers["x-urai-provider"] == "fake"
        assert headers["x-urai-model"] == "test-model"
        assert json.loads(body)["choices"][0]["message"]["content"] == "ok:good-key-0001"
    finally:
        server.shutdown()
        server.server_close()


def test_streaming_chat(fake_router):
    server, url = _start(fake_router["router"], fake_router["cfg"])
    try:
        conn = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=10)
        payload = json.dumps({"model": "fake/test-model", "messages": [{"role": "user", "content": "hi"}], "stream": True})
        conn.request("POST", "/v1/chat/completions", body=payload, headers={"Content-Type": "application/json"})
        response = conn.getresponse()
        body = response.read().decode()
        assert response.status == 200
        assert "data: [DONE]" in body
        assert '"content":"he"' in body
        conn.close()
    finally:
        server.shutdown()
        server.server_close()


def test_local_token(fake_router):
    fake_router["cfg"].set("token", "secret-token")
    server, url = _start(fake_router["router"], fake_router["cfg"])
    try:
        try:
            urllib.request.urlopen(url + "/v1/models", timeout=5)
            assert False, "expected 401"
        except urllib.error.HTTPError as exc:
            assert exc.code == 401
        request = urllib.request.Request(url + "/v1/models", headers={"Authorization": "Bearer secret-token"})
        with urllib.request.urlopen(request, timeout=5) as response:
            assert response.status == 200
        # health is always open
        with urllib.request.urlopen(url + "/health", timeout=5) as response:
            assert response.status == 200
    finally:
        server.shutdown()
        server.server_close()


def test_unknown_endpoint(fake_router):
    server, url = _start(fake_router["router"], fake_router["cfg"])
    try:
        try:
            urllib.request.urlopen(url + "/nope", timeout=5)
            assert False
        except urllib.error.HTTPError as exc:
            assert exc.code == 404
    finally:
        server.shutdown()
        server.server_close()
