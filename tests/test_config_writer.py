import json
from pathlib import Path

from urai_router import config_writer
from urai_router.providers import ModelInfo


def test_opencode_merge_preserves_existing(fake_router, tmp_path):
    path = tmp_path / "opencode.jsonc"
    path.write_text('{\n  // keep me\n  "theme": "dark",\n  "provider": {"other": {"name": "Other"}},\n}\n')
    report = config_writer.setup_opencode(
        fake_router["cfg"],
        fake_router["store"],
        fake_router["router"],
        explicit=str(path),
        set_default=True,
    )
    data = json.loads(path.read_text().split("\n", 2)[2])
    assert data["theme"] == "dark"
    assert "other" in data["provider"]
    provider = data["provider"]["urai-router"]
    assert provider["npm"] == "@ai-sdk/openai-compatible"
    assert provider["options"]["baseURL"].endswith("/v1")
    assert "fake/free-model" in provider["models"]
    assert "urai-router" in data["mcp"]
    assert data["mcp"]["urai-router"]["type"] == "local"
    assert data["model"].startswith("urai-router/")
    assert report["models"] >= 1
    assert Path(report["backup"]).exists()


def test_setup_kiro_writes_mcp_and_agent(fake_router):
    report = config_writer.setup_kiro(fake_router["cfg"], fake_router["store"], fake_router["router"])
    mcp_path = Path(report["mcp"]["path"])
    agent_path = Path(report["agent"]["path"])
    assert mcp_path.exists() and agent_path.exists()
    mcp = json.loads(mcp_path.read_text())
    assert "urai-router" in mcp["mcpServers"]
    assert mcp["mcpServers"]["urai-router"]["command"].endswith("python3") or mcp["mcpServers"]["urai-router"]["command"]
    agent = json.loads(agent_path.read_text())
    assert agent["includeMcpJson"] is True
    assert "@urai-router" in agent["tools"]


def test_setup_skills(fake_router, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    report = config_writer.setup_skills(project=True)
    assert report["ok"]
    for row in report["targets"]:
        assert Path(row["path"]).exists()
    project_skill = tmp_path / ".opencode" / "skills" / "urai-router" / "SKILL.md"
    assert project_skill.exists()
    assert "name: urai-router" in project_skill.read_text()


def test_model_map_contains_free_and_limits(fake_router):
    router = fake_router["router"]
    router.catalog.set(
        "fake",
        [
            ModelInfo(id="free-model", provider="fake", name="Free", free=True, context=1000, output=100, api_style="openai"),
            ModelInfo(id="paid-model", provider="fake", name="Paid", free=False, context=2000, api_style="openai"),
        ],
    )
    models = config_writer.opencode_model_map(router)
    assert models["fake/free-model"]["name"].startswith("★")
    assert models["fake/free-model"]["limit"]["context"] == 1000


def test_export_env(fake_router):
    env = config_writer.export_env(fake_router["cfg"])
    assert env["OPENAI_BASE_URL"].endswith("/v1")
    assert env["OPENAI_API_KEY"] == "urai-local"
