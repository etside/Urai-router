"""Configuration and path resolution for Urai Router."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from .util import config_dir, deep_merge, home, load_jsonc, read_json

DEFAULTS: Dict[str, Any] = {
    "host": "127.0.0.1",
    "port": 8787,
    "token": "",
    "keys_file": None,
    "max_attempts": 8,
    "default_model": "auto",
    "prefer_free": True,
    "catalog_ttl": 900,
    "zen_ttl": 300,
    "request_timeout": 120,
    "detect_unknown_keys": True,
    "cooldowns": {
        "rate": 60,
        "invalid": 1800,
        "forbidden": 1800,
        "server": 20,
        "network": 15,
        "max": 3600,
    },
    "aliases": {},
    "providers": {},
    "proxy": {
        "cors": True,
        "enable_ensure": True,
        "log_requests": True,
    },
    "opencode": {
        "path": None,
        "provider_id": "urai-router",
        "set_default": False,
        "model_limit": 300,
    },
    "kiro": {
        "agent_name": "urai-router",
        "scope": "global",
    },
}


def default_config_path() -> Path:
    return config_dir() / "config.json"


def default_keys_candidates() -> List[Path]:
    return [
        Path.cwd() / "keys.txt",
        config_dir() / "keys.txt",
        home() / ".config" / "urai" / "keys.txt",
        home() / ".urai" / "keys.txt",
        home() / "urai-keys.txt",
        home() / "keys.txt",
    ]


def resolve_keys_file(explicit: Optional[str] = None) -> Path:
    if explicit:
        return Path(os.path.expanduser(str(explicit))).resolve()
    env = os.environ.get("URAI_KEYS_FILE")
    if env:
        return Path(os.path.expanduser(env)).resolve()
    for candidate in default_keys_candidates():
        try:
            if candidate.is_file():
                return candidate.resolve()
        except OSError:
            continue
    return (config_dir() / "keys.txt").resolve()


@dataclass
class Config:
    data: Dict[str, Any] = field(default_factory=lambda: deep_merge({}, DEFAULTS))
    path: Path = field(default_factory=default_config_path)

    def get(self, dotted: str, default: Any = None) -> Any:
        node: Any = self.data
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def set(self, dotted: str, value: Any) -> None:
        node = self.data
        parts = dotted.split(".")
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node[parts[-1]] = value

    @property
    def cooldowns(self) -> Dict[str, float]:
        merged = dict(DEFAULTS["cooldowns"])
        merged.update(self.get("cooldowns", {}) or {})
        return {k: float(v) for k, v in merged.items()}

    @property
    def aliases(self) -> Dict[str, str]:
        return {str(k): str(v) for k, v in (self.get("aliases", {}) or {}).items()}

    @property
    def provider_overrides(self) -> Dict[str, Dict[str, Any]]:
        raw = self.get("providers", {}) or {}
        return {str(k): dict(v) for k, v in raw.items() if isinstance(v, dict)}

    def redacted(self) -> Dict[str, Any]:
        result = dict(self.data)
        if result.get("token"):
            result["token"] = "***"
        return result


def load_config(explicit: Optional[str] = None, create: bool = False) -> Config:
    path = Path(os.path.expanduser(str(explicit))).resolve() if explicit else default_config_path()
    raw = read_json(path, {}) or {}
    if not isinstance(raw, dict):
        raw = {}
    cfg = Config(data=deep_merge({}, DEFAULTS), path=path)
    cfg.data = deep_merge(cfg.data, raw)
    if create and not path.exists():
        from .util import write_json

        write_json(path, cfg.data, mode=0o600)
    return cfg


def load_opencode_config(explicit: Optional[str] = None) -> Path:
    if explicit:
        return Path(os.path.expanduser(str(explicit))).resolve()
    base = Path(os.environ.get("XDG_CONFIG_HOME") or (home() / ".config")) / "opencode"
    for name in ("opencode.jsonc", "opencode.json"):
        candidate = base / name
        if candidate.exists():
            return candidate
    return base / "opencode.jsonc"
