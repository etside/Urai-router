"""Generate and merge OpenCode, Kiro and Agent Skills configuration."""

from __future__ import annotations

import json
import os
import shutil
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from .config import Config, load_opencode_config
from .keys import KeyStore
from .router import Router
from .util import VERSION, atomic_write, home, load_jsonc, write_json

SKILL_NAME = "urai-router"
PROVIDER_ID = "urai-router"


def skill_source() -> Path:
    return Path(__file__).resolve().parent / "skills" / SKILL_NAME / "SKILL.md"


def mcp_command(cfg: Config, store: KeyStore) -> List[str]:
    command = [sys.executable, "-m", "urai_router", "mcp"]
    if cfg.path and Path(cfg.path).exists():
        command += ["--config", str(cfg.path)]
    command += ["--keys", str(store.path)]
    return command


def mcp_environment(store: KeyStore) -> Dict[str, str]:
    return {"URAI_KEYS_FILE": str(store.path)}


def opencode_model_map(router: Router, limit: int = 300) -> Dict[str, Dict[str, Any]]:
    models = router.usable_models()
    result: Dict[str, Dict[str, Any]] = {}
    for model in models[: max(1, limit)]:
        entry: Dict[str, Any] = {"name": ("★ " if model.free else "") + (model.name or model.id)}
        limit_cfg: Dict[str, int] = {}
        if model.context:
            limit_cfg["context"] = model.context
        if model.output:
            limit_cfg["output"] = model.output
        if limit_cfg:
            entry["limit"] = limit_cfg
        if model.tools is False:
            entry["tool_call"] = False
        result[model.qualified] = entry
    if not result:
        # Offline bootstrap: at least expose the current Zen free rows.
        for mid in ("deepseek-v4.1-flash-free", "glm-5.3-flash-free", "big-pickle"):
            result[f"opencode/{mid}"] = {"name": f"★ {mid}"}
    return result


def _base_url(cfg: Config) -> str:
    host = str(cfg.get("host", "127.0.0.1"))
    port = int(cfg.get("port", 8787))
    return f"http://{host}:{port}"


def build_opencode_patch(cfg: Config, store: KeyStore, router: Router, set_default: bool = False) -> Dict[str, Any]:
    models = opencode_model_map(router, int(cfg.get("opencode.model_limit", 300)))
    token = str(cfg.get("token") or "")
    api_key = token if token else "urai-local"
    provider = {
        "npm": "@ai-sdk/openai-compatible",
        "name": "Urai Router (auto keys)",
        "options": {"baseURL": _base_url(cfg) + "/v1", "apiKey": api_key},
        "models": models,
    }
    patch: Dict[str, Any] = {
        "provider": {PROVIDER_ID: provider},
        "mcp": {
            PROVIDER_ID: {
                "type": "local",
                "command": mcp_command(cfg, store),
                "enabled": True,
                "environment": mcp_environment(store),
            }
        },
    }
    if set_default and models:
        free = [mid for mid in models if mid.startswith("opencode/")]
        chosen = free[0] if free else next(iter(models))
        patch["model"] = f"{PROVIDER_ID}/{chosen}"
        patch["small_model"] = f"{PROVIDER_ID}/{chosen}"
    return patch


def _write_config(path: Path, data: Dict[str, Any], dry_run: bool) -> Dict[str, Any]:
    backup = None
    if path.exists() and not dry_run:
        backup = path.with_name(path.name + f".urai-backup-{int(time.time())}")
        try:
            shutil.copy2(path, backup)
        except OSError:
            backup = None
    if dry_run:
        return {"path": str(path), "changed": False, "dry_run": True, "backup": None}
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(data, indent=2, ensure_ascii=False)
    if path.suffix == ".jsonc" or path.name.endswith(".jsonc"):
        text = (
            f"// Managed by urai-router {VERSION}. Existing settings are preserved; comments in this file\n"
            "// were normalised during merge. A timestamped backup is written next to it.\n"
            + text
            + "\n"
        )
    atomic_write(path, text, mode=0o600)
    return {"path": str(path), "changed": True, "dry_run": False, "backup": str(backup) if backup else None}


def setup_opencode(
    cfg: Config,
    store: KeyStore,
    router: Router,
    project: bool = False,
    explicit: Optional[str] = None,
    set_default: bool = False,
    dry_run: bool = False,
) -> Dict[str, Any]:
    if explicit:
        path = Path(os.path.expanduser(explicit)).resolve()
    elif project:
        path = Path.cwd() / "opencode.json"
    else:
        path = load_opencode_config()
    existing = load_jsonc(path)
    patch = build_opencode_patch(cfg, store, router, set_default=set_default)
    merged = dict(existing)
    merged["$schema"] = merged.get("$schema", "https://opencode.ai/config.json")
    for section, values in patch.items():
        current = merged.get(section)
        if isinstance(current, dict):
            current.update(values)
        else:
            merged[section] = values
    result = _write_config(path, merged, dry_run)
    result["models"] = len(patch["provider"][PROVIDER_ID]["models"])
    result["base_url"] = patch["provider"][PROVIDER_ID]["options"]["baseURL"]
    result["default_model"] = merged.get("model")
    return result


def kiro_mcp_path() -> Path:
    return home() / ".kiro" / "settings" / "mcp.json"


def kiro_agent_path(name: str = SKILL_NAME) -> Path:
    return home() / ".kiro" / "agents" / f"{name}.json"


def _kiro_server(cfg: Config, store: KeyStore) -> Dict[str, Any]:
    return {
        "command": mcp_command(cfg, store)[0],
        "args": mcp_command(cfg, store)[1:],
        "env": mcp_environment(store),
        "disabled": False,
        "timeout": 30000,
    }


def setup_kiro(cfg: Config, store: KeyStore, router: Router, dry_run: bool = False) -> Dict[str, Any]:
    server = _kiro_server(cfg, store)
    mcp_path = kiro_mcp_path()
    existing = load_jsonc(mcp_path)
    servers = existing.get("mcpServers")
    if not isinstance(servers, dict):
        servers = {}
    servers[PROVIDER_ID] = server
    merged = dict(existing)
    merged["mcpServers"] = servers
    mcp_result = _write_config(mcp_path, merged, dry_run)

    agent_name = str(cfg.get("kiro.agent_name", SKILL_NAME))
    agent_path = kiro_agent_path(agent_name)
    prompt = (
        "You are a coding agent using Urai Router. For any LLM call that should use the user's pooled API keys, "
        "use the `urai_*` MCP tools. Prefer `urai_chat` with model `auto` (free OpenCode Zen models first). "
        "Use `urai_models` to discover models, `urai_health` for key state, and `urai_ensure_proxy` when a local "
        "OpenAI-compatible endpoint is needed. Never reveal full API keys."
    )
    agent = {
        "name": agent_name,
        "description": "Agent wired to Urai Router's automatic API-key router over MCP",
        "prompt": prompt,
        "mcpServers": {PROVIDER_ID: server},
        "tools": ["read", "write", "shell", "grep", "glob", "@" + PROVIDER_ID],
        "toolAliases": {},
        "allowedTools": [
            f"@{PROVIDER_ID}/urai_health",
            f"@{PROVIDER_ID}/urai_models",
            f"@{PROVIDER_ID}/urai_keys",
            f"@{PROVIDER_ID}/urai_explain",
        ],
        "resources": [],
        "toolsSettings": {},
        "includeMcpJson": True,
        "model": None,
    }
    agent_result = _write_config(agent_path, agent, dry_run)
    return {
        "mcp": mcp_result,
        "agent": agent_result,
        "server": server,
        "kiro_installed": bool(shutil.which("kiro-cli")),
    }


def skill_targets(project: bool = False) -> List[Path]:
    targets = [
        home() / ".config" / "opencode" / "skills" / SKILL_NAME / "SKILL.md",
        home() / ".agents" / "skills" / SKILL_NAME / "SKILL.md",
        home() / ".claude" / "skills" / SKILL_NAME / "SKILL.md",
    ]
    if project:
        targets += [
            Path.cwd() / ".opencode" / "skills" / SKILL_NAME / "SKILL.md",
            Path.cwd() / ".agents" / "skills" / SKILL_NAME / "SKILL.md",
            Path.cwd() / ".claude" / "skills" / SKILL_NAME / "SKILL.md",
        ]
    return targets


def setup_skills(project: bool = False, dry_run: bool = False) -> Dict[str, Any]:
    source = skill_source()
    if not source.exists():
        return {"ok": False, "error": f"skill template missing: {source}", "targets": []}
    text = source.read_text(encoding="utf-8")
    rows = []
    for target in skill_targets(project):
        if not dry_run:
            target.parent.mkdir(parents=True, exist_ok=True)
            atomic_write(target, text, mode=0o644)
        rows.append({"path": str(target), "dry_run": dry_run})
    return {"ok": True, "source": str(source), "targets": rows}


def export_opencode(cfg: Config, store: KeyStore, router: Router, set_default: bool = False) -> Dict[str, Any]:
    return build_opencode_patch(cfg, store, router, set_default=set_default)


def export_kiro(cfg: Config, store: KeyStore) -> Dict[str, Any]:
    return {"mcpServers": {PROVIDER_ID: _kiro_server(cfg, store)}}


def export_env(cfg: Config) -> Dict[str, str]:
    base = _base_url(cfg) + "/v1"
    token = str(cfg.get("token") or "") or "urai-local"
    return {
        "OPENAI_BASE_URL": base,
        "OPENAI_API_BASE": base,
        "OPENAI_API_KEY": token,
        "URAI_ROUTER_URL": base,
    }


def setup(
    cfg: Config,
    store: KeyStore,
    router: Router,
    target: str = "all",
    project: bool = False,
    set_default: bool = False,
    dry_run: bool = False,
    explicit: Optional[str] = None,
) -> Dict[str, Any]:
    target = (target or "all").lower()
    report: Dict[str, Any] = {"version": VERSION, "target": target, "dry_run": dry_run, "actions": {}}
    if target in ("opencode", "all"):
        report["actions"]["opencode"] = setup_opencode(
            cfg, store, router, project=project, explicit=explicit, set_default=set_default, dry_run=dry_run
        )
    if target in ("kiro", "all"):
        report["actions"]["kiro"] = setup_kiro(cfg, store, router, dry_run=dry_run)
    if target in ("skills", "all"):
        report["actions"]["skills"] = setup_skills(project=project, dry_run=dry_run)
    report["keys_file"] = str(store.path)
    report["models_available"] = len(router.usable_models())
    report["proxy_url"] = _base_url(cfg)
    return report
