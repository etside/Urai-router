import json

from urai_router.keys import KeyStore
from urai_router.router import Router


def _chat(router, model="fake/test-model", stream=False):
    return router.chat(
        {
            "model": model,
            "messages": [{"role": "user", "content": "hi"}],
            "stream": stream,
        }
    )


def test_failover_on_401(fake_router):
    store = fake_router["store"]
    store.path.write_text("fake:bad # label=bad\nfake:good # label=good\n")
    store.reload(force=True)
    result = _chat(fake_router["router"])
    assert result.ok
    assert result.provider == "fake"
    assert result.model == "test-model"
    assert json.loads(result.body)["choices"][0]["message"]["content"] == "ok:good"
    bad = [key for key in store.keys if key.value == "bad"][0]
    assert bad.strikes == 1 and not bad.available()


def test_failover_on_429_and_cooldown(fake_router):
    store = fake_router["store"]
    store.path.write_text("fake:rate # label=rate\nfake:good # label=good\n")
    store.reload(force=True)
    result = _chat(fake_router["router"])
    assert result.ok
    rate = [key for key in store.keys if key.value == "rate"][0]
    assert not rate.available()
    assert 0 < rate.cooldown_until - __import__("time").time() <= 2


def test_500_failover(fake_router):
    store = fake_router["store"]
    store.path.write_text("fake:server # label=server\nfake:good # label=good\n")
    store.reload(force=True)
    assert _chat(fake_router["router"]).ok


def test_400_does_not_rotate(fake_router):
    fake_router["upstream"]["requests"].clear()
    result = _chat(fake_router["router"], model="fake/bad-model")
    assert result.status == 400
    assert len(fake_router["upstream"]["requests"]) == 1


def test_no_keys(fake_router):
    fake_router["store"].path.write_text("# empty\n")
    fake_router["store"].reload(force=True)
    result = _chat(fake_router["router"])
    assert result.status == 503
    assert b"no usable route" in result.body or b"no key" in result.body


def test_stream_passthrough(fake_router):
    result = _chat(fake_router["router"], stream=True)
    assert result.ok
    body = b"".join(result.stream).decode()
    assert '"content":"he"' in body
    assert '"content":"llo"' in body
    assert "data: [DONE]" in body


def test_explain(fake_router):
    payload = fake_router["router"].explain("fake/test-model")
    assert payload["targets"][0]["provider"] == "fake"
    assert payload["targets"][0]["keys_ready"] == 1


def test_models_catalogue(fake_router):
    models = fake_router["router"].all_models(force=True)
    ids = {m.qualified for m in models}
    assert "fake/test-model" in ids
    assert "fake/free-model" in ids


def test_auto_prefers_free(fake_router):
    models = fake_router["router"].usable_models(force=True)
    free = [m for m in models if m.free]
    assert free
    target = fake_router["router"].resolve("free")
    assert target and target[0].model.free
