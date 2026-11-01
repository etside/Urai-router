import json

from urai_router.config import Config
from urai_router.keys import Key, KeyStore, parse_key_line, parse_keys, write_example_keys
from urai_router.util import deep_merge


def test_parse_variants():
    text = """
# comment
opencode:sk-aaaaaaaa
openrouter:sk-or-v1-bbbbbbbb
groq=gsk_cccccccc
deepseek,sk-dddddddd
AIzaeeeeeeee
opencode sk-ffffffff
sk-or-v1-gggggggg
"""
    keys, issues = parse_keys(text)
    assert not issues
    assert [(k.provider, k.value) for k in keys] == [
        ("opencode", "sk-aaaaaaaa"),
        ("openrouter", "sk-or-v1-bbbbbbbb"),
        ("groq", "gsk_cccccccc"),
        ("deepseek", "sk-dddddddd"),
        ("google", "AIzaeeeeeeee"),
        ("opencode", "sk-ffffffff"),
        ("openrouter", "sk-or-v1-gggggggg"),
    ]


def test_parse_duplicate_and_annotations():
    keys, issues = parse_keys(
        "opencode:sk-aaaaaaaa # label=main weight=3\n"
        "opencode:sk-aaaaaaaa\n"
        "opencode:sk-bbbbbbbb @backup *2\n"
        "abc\n"
    )
    assert len(keys) == 2
    assert keys[0].label == "main" and keys[0].weight == 3
    assert keys[1].label == "backup" and keys[1].weight == 2
    assert any("duplicate" in i.reason for i in issues)
    assert any("too short" in i.reason for i in issues)


def test_parse_env(monkeypatch):
    monkeypatch.setenv("MY_KEY", "sk-zzzzzzzzzz")
    keys, _ = parse_keys("opencode:${MY_KEY}\n")
    assert keys[0].value == "sk-zzzzzzzzzz"


def test_mask_never_leaks_full_key():
    key = Key(provider="opencode", value="sk-1234567890abcdef")
    public = key.public()
    assert "sk-1234567890abcdef" not in json.dumps(public)
    assert key.masked.endswith("cdef")


def test_weighted_round_robin(tmp_path, tmp_env):
    path = tmp_path / "keys.txt"
    path.write_text("fake:key-aaaaaaaa # weight=3\nfake:key-bbbbbbbb # weight=1\n")
    store = KeyStore(path, Config(), state_path=tmp_path / "state.json")
    labels = []
    for _ in range(8):
        key = store.select("fake")
        labels.append(key.value)
    assert labels.count("key-aaaaaaaa") == 6
    assert labels.count("key-bbbbbbbb") == 2


def test_cooldown_and_success(tmp_path):
    path = tmp_path / "keys.txt"
    path.write_text("fake:key-aaaaaaaa\n")
    store = KeyStore(path, Config(), state_path=tmp_path / "state.json")
    key = store.keys[0]
    store.mark_failure(key, "rate", retry_after=30)
    assert not key.available()
    assert store.select("fake") is None
    assert store.next_retry_after("fake") is not None
    store.mark_success(key)
    assert key.available() and store.select("fake") is key


def test_reload_keeps_health(tmp_path):
    path = tmp_path / "keys.txt"
    path.write_text("fake:key-aaaaaaaa\n")
    store = KeyStore(path, Config(), state_path=tmp_path / "state.json")
    store.mark_failure(store.keys[0], "invalid")
    store.save_state(force=True)
    old_strikes = store.keys[0].strikes
    path.write_text("fake:key-aaaaaaaa\nfake:key-bbbbbbbb\n")
    assert store.reload(force=True)
    assert len(store.keys) == 2
    assert store.keys[0].strikes == old_strikes
    assert store.keys[0].last_error == "invalid"


def test_prefix_detection():
    from urai_router.providers import detect_by_prefix

    assert detect_by_prefix("sk-or-v1-abc")[0] == "openrouter"
    assert detect_by_prefix("gsk_abc")[0] == "groq"
    assert detect_by_prefix("sk-ant-abc")[0] == "anthropic"
    assert detect_by_prefix("AIzaabc")[0] == "google"
    assert detect_by_prefix("nvapi-abc")[0] == "nvidia"
    assert "opencode" in detect_by_prefix("sk-abc")


def test_example_keys_file(tmp_path):
    path = tmp_path / "keys.txt"
    write_example_keys(path)
    assert path.exists()
    assert "opencode:sk-" in path.read_text()
