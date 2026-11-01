"""A dependency-free MCP (stdio) server exposing the router to agents."""

from __future__ import annotations

import json
import sys
from typing import Any, Callable, Dict, List, Optional

from .config import Config
from .keys import KeyStore
from .providers import provider_summary
from .router import Router
from .util import VERSION, json_bytes

PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")

INSTRUCTIONS = """Urai Router routes OpenAI-compatible chat requests across every API key in your keys.txt.
Use `urai_chat` for completions, `urai_models` to discover models (free OpenCode Zen models are included),
`urai_health` to inspect key health, and `urai_ensure_proxy` to get a local OpenAI-compatible base URL.
Never print API keys: the tools only return masked values."""


def _tool(name: str, description: str, schema: Dict[str, Any]) -> Dict[str, Any]:
    return {"name": name, "description": description, "inputSchema": schema}


TOOLS: List[Dict[str, Any]] = [
    _tool(
        "urai_chat",
        "Send an OpenAI-style chat completion through the automatic key router. Rotates keys on 401/403/429/5xx.",
        {
            "type": "object",
            "properties": {
                "messages": {
                    "type": "array",
                    "description": "OpenAI chat messages",
                    "items": {
                        "type": "object",
                        "properties": {"role": {"type": "string"}, "content": {}},
                        "required": ["role", "content"],
                    },
                },
                "model": {"type": "string", "description": "Model id, provider/model, or auto/free"},
                "provider": {"type": "string", "description": "Optional provider id, e.g. opencode"},
                "system": {"type": "string", "description": "Optional system prompt"},
                "temperature": {"type": "number"},
                "max_tokens": {"type": "integer"},
                "tools": {"type": "array", "description": "Optional OpenAI tool definitions"},
            },
            "required": ["messages"],
        },
    ),
    _tool(
        "urai_models",
        "List the merged model catalogue from all configured providers, including free OpenCode Zen models.",
        {
            "type": "object",
            "properties": {
                "provider": {"type": "string"},
                "free_only": {"type": "boolean"},
                "refresh": {"type": "boolean"},
                "limit": {"type": "integer"},
            },
        },
    ),
    _tool("urai_keys", "Show masked API keys, health, cooldowns and key counts per provider.", {"type": "object", "properties": {}}),
    _tool("urai_health", "Show router health, key file, catalogue freshness and proxy status.", {"type": "object", "properties": {}}),
    _tool("urai_stats", "Show routing statistics (same as health plus catalogue details).", {"type": "object", "properties": {}}),
    _tool(
        "urai_explain",
        "Explain how a model or alias would be routed, which providers qualify and how many keys are ready.",
        {"type": "object", "properties": {"model": {"type": "string"}}},
    ),
    _tool(
        "urai_refresh",
        "Reload keys.txt and refresh provider model catalogues.",
        {"type": "object", "properties": {"force": {"type": "boolean"}}},
    ),
    _tool(
        "urai_ensure_proxy",
        "Start the local OpenAI-compatible proxy in the background if needed and return its base URL.",
        {"type": "object", "properties": {}},
    ),
    _tool(
        "urai_check",
        "Probe one or all keys with a tiny request and report which ones are valid.",
        {"type": "object", "properties": {"provider": {"type": "string"}, "all": {"type": "boolean"}}},
    ),
    _tool(
        "urai_setup",
        "Generate/merge OpenCode, Kiro and skill configuration. dry_run defaults to true.",
        {
            "type": "object",
            "properties": {
                "target": {"type": "string", "enum": ["opencode", "kiro", "skills", "all"]},
                "dry_run": {"type": "boolean"},
                "project": {"type": "boolean"},
                "set_default": {"type": "boolean"},
            },
        },
    ),
]

RESOURCES: List[Dict[str, Any]] = [
    {"uri": "urai://config", "name": "Urai Router config", "description": "Redacted router configuration", "mimeType": "application/json"},
    {"uri": "urai://keys", "name": "Key health", "description": "Masked keys and cooldown state", "mimeType": "application/json"},
    {"uri": "urai://providers", "name": "Providers", "description": "Provider registry with base URLs and auth style", "mimeType": "application/json"},
    {"uri": "urai://models", "name": "Models", "description": "Merged model catalogue", "mimeType": "application/json"},
    {"uri": "urai://stats", "name": "Stats", "description": "Router status and catalogue freshness", "mimeType": "application/json"},
]

PROMPTS: List[Dict[str, Any]] = [
    {
        "name": "route_request",
        "description": "Instructions for routing a coding request through Urai Router",
        "arguments": [{"name": "task", "description": "The task to perform", "required": True}],
    }
]


class McpServer:
    def __init__(self, cfg: Config, store: KeyStore, verbose: bool = False):
        self.cfg = cfg
        self.store = store
        self.router = Router(cfg, store)
        self.verbose = verbose
        self.initialized = False
        self.client_version = PROTOCOL_VERSIONS[0]

    def log(self, message: str) -> None:
        if self.verbose:
            sys.stderr.write(f"[urai-router:mcp] {message}\n")

    # --------------------------------------------------------------- dispatch
    def handle(self, message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        if not isinstance(message, dict):
            return self._error(None, -32600, "invalid request")
        method = message.get("method")
        msg_id = message.get("id")
        if method is None:
            return None
        if isinstance(method, str) and method.startswith("notifications/"):
            return None
        params = message.get("params") or {}
        try:
            if method == "initialize":
                return self._result(msg_id, self._initialize(params))
            if method == "ping":
                return self._result(msg_id, {})
            if method == "tools/list":
                return self._result(msg_id, {"tools": TOOLS})
            if method == "tools/call":
                return self._result(msg_id, self._call_tool(params))
            if method == "resources/list":
                return self._result(msg_id, {"resources": RESOURCES})
            if method == "resources/read":
                return self._result(msg_id, self._read_resource(params))
            if method == "resources/templates/list":
                return self._result(msg_id, {"resourceTemplates": []})
            if method == "prompts/list":
                return self._result(msg_id, {"prompts": PROMPTS})
            if method == "prompts/get":
                return self._result(msg_id, self._get_prompt(params))
            if method in ("shutdown", "exit"):
                return self._result(msg_id, None)
            return self._error(msg_id, -32601, f"method not found: {method}")
        except Exception as exc:  # noqa: BLE001
            self.log(f"error in {method}: {exc}")
            return self._error(msg_id, -32603, f"internal error: {exc}")

    def _initialize(self, params: Dict[str, Any]) -> Dict[str, Any]:
        requested = params.get("protocolVersion")
        version = requested if requested in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0]
        self.client_version = version
        self.initialized = True
        return {
            "protocolVersion": version,
            "capabilities": {
                "tools": {"listChanged": False},
                "resources": {"subscribe": False, "listChanged": False},
                "prompts": {"listChanged": False},
            },
            "serverInfo": {"name": "urai-router", "version": VERSION},
            "instructions": INSTRUCTIONS,
        }

    def _result(self, msg_id: Any, result: Any) -> Dict[str, Any]:
        return {"jsonrpc": "2.0", "id": msg_id, "result": result}

    def _error(self, msg_id: Any, code: int, message: str) -> Dict[str, Any]:
        return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}

    # ------------------------------------------------------------------ tools
    def _call_tool(self, params: Dict[str, Any]) -> Dict[str, Any]:
        name = params.get("name")
        args = params.get("arguments") or {}
        if not isinstance(args, dict):
            args = {}
        handler = getattr(self, f"_tool_{name}", None) if isinstance(name, str) else None
        if handler is None:
            return {"content": [{"type": "text", "text": f"unknown tool: {name}"}], "isError": True}
        try:
            text, structured = handler(args)
            return {
                "content": [{"type": "text", "text": text}],
                "structuredContent": structured,
                "isError": False,
            }
        except Exception as exc:  # noqa: BLE001
            return {
                "content": [{"type": "text", "text": f"{name} failed: {exc}"}],
                "isError": True,
            }

    def _tool_urai_chat(self, args: Dict[str, Any]) -> tuple[str, Dict[str, Any]]:
        messages = args.get("messages") or []
        if not isinstance(messages, list) or not messages:
            raise ValueError("messages must be a non-empty array")
        if args.get("system"):
            messages = [{"role": "system", "content": args["system"]}] + messages
        model = args.get("model")
        provider = args.get("provider")
        if provider:
            model = f"{provider}/{model}" if model and "/" not in str(model) else (model or provider)
        body: Dict[str, Any] = {"messages": messages, "model": model or self.cfg.get("default_model", "auto"), "stream": False}
        for field_name in ("temperature", "max_tokens", "tools", "tool_choice", "top_p"):
            if args.get(field_name) is not None:
                body[field_name] = args[field_name]
        result = self.router.chat(body)
        text = result.body.decode("utf-8", "replace")
        if not result.ok:
            return f"Urai Router error {result.status}: {text}", {"status": result.status, "attempts": result.attempts}
        try:
            parsed = json.loads(text)
        except ValueError:
            return text, {"status": result.status}
        message = ((parsed.get("choices") or [{}])[0]).get("message") or {}
        content = message.get("content") or ""
        if message.get("reasoning_content"):
            content = f"{content}\n\n[reasoning]\n{message['reasoning_content']}"
        if message.get("tool_calls"):
            content += "\n\n[tool_calls]\n" + json.dumps(message["tool_calls"], indent=2)
        meta = {
            "provider": result.provider,
            "model": result.model,
            "key": result.key_label,
            "status": result.status,
            "usage": parsed.get("usage"),
            "attempts": result.attempts,
        }
        return content, meta

    def _tool_urai_models(self, args: Dict[str, Any]) -> tuple[str, Dict[str, Any]]:
        models = self.router.usable_models(force=bool(args.get("refresh")))
        provider = args.get("provider")
        free_only = bool(args.get("free_only"))
        limit = int(args.get("limit") or 80)
        rows = [m for m in models if (not provider or m.provider == provider) and (not free_only or m.free)]
        rows = rows[: max(1, min(limit, 500))]
        lines = [
            f"{m.qualified}  ({'free' if m.free else 'paid'}"
            + (f", ctx={m.context}" if m.context else "")
            + (f", tools={'yes' if m.tools else 'no'}")
            + ")"
            for m in rows
        ]
        return "\n".join(lines), {"count": len(rows), "models": [m.to_dict() for m in rows]}

    def _tool_urai_keys(self, args: Dict[str, Any]) -> tuple[str, Dict[str, Any]]:
        stats = self.store.stats()
        lines = []
        for pid, row in sorted(stats["providers"].items()):
            lines.append(f"{pid}: {row['ready']}/{row['total']} ready, {row['cooling']} cooling, {row['disabled']} disabled")
            for key in row["keys"]:
                lines.append(f"  - {key['label']} {key['key']} [{key['status']}] ok={key['successes']} fail={key['failures']}")
        if not lines:
            lines.append(f"no keys loaded from {stats['keys_file']}")
        return "\n".join(lines), stats

    def _tool_urai_health(self, args: Dict[str, Any]) -> tuple[str, Dict[str, Any]]:
        from .proxy import proxy_status

        status = self.router.status()
        proxy = proxy_status(self.cfg)
        payload = {"router": status, "proxy": proxy}
        text = json.dumps(payload, indent=2)
        return text, payload

    def _tool_urai_stats(self, args: Dict[str, Any]) -> tuple[str, Dict[str, Any]]:
        return self._tool_urai_health(args)

    def _tool_urai_explain(self, args: Dict[str, Any]) -> tuple[str, Dict[str, Any]]:
        payload = self.router.explain(args.get("model"))
        return json.dumps(payload, indent=2), payload

    def _tool_urai_refresh(self, args: Dict[str, Any]) -> tuple[str, Dict[str, Any]]:
        changed = self.store.reload(force=True)
        count = self.router.refresh_catalog(force=bool(args.get("force", True)))
        payload = {"keys_reloaded": changed, "keys": len(self.store.keys), "models": count}
        return json.dumps(payload), payload

    def _tool_urai_ensure_proxy(self, args: Dict[str, Any]) -> tuple[str, Dict[str, Any]]:
        from .proxy import ensure_proxy

        payload = ensure_proxy(self.router, self.cfg)
        text = json.dumps(payload, indent=2)
        if payload.get("running"):
            text = f"OpenAI-compatible base URL: {payload['url']}/v1\n\n{text}"
        return text, payload

    def _tool_urai_check(self, args: Dict[str, Any]) -> tuple[str, Dict[str, Any]]:
        from .providers import PROVIDERS, probe_key

        provider_filter = args.get("provider")
        rows = []
        for key in self.store.keys:
            if provider_filter and key.provider != provider_filter:
                continue
            provider = PROVIDERS.get(key.provider)
            if not provider:
                rows.append({"provider": key.provider, "label": key.label or key.id[:8], "ok": False, "error": "unknown provider"})
                continue
            ok = probe_key(provider, key.value, timeout=12)
            rows.append({"provider": key.provider, "label": key.label or key.id[:8], "key": key.masked, "ok": ok})
        lines = [f"{r['provider']} {r['label']} {r['key']}: {'ok' if r['ok'] else 'FAILED'}" for r in rows]
        return "\n".join(lines) or "no keys", {"results": rows}

    def _tool_urai_setup(self, args: Dict[str, Any]) -> tuple[str, Dict[str, Any]]:
        from .config_writer import setup

        target = str(args.get("target") or "all")
        dry_run = args.get("dry_run", True)
        report = setup(
            self.cfg,
            self.store,
            self.router,
            target=target,
            project=bool(args.get("project")),
            set_default=bool(args.get("set_default")),
            dry_run=bool(dry_run),
        )
        return json.dumps(report, indent=2), report

    # -------------------------------------------------------------- resources
    def _read_resource(self, params: Dict[str, Any]) -> Dict[str, Any]:
        uri = params.get("uri")
        if uri == "urai://config":
            payload = self.cfg.redacted()
        elif uri == "urai://keys":
            payload = self.store.stats()
        elif uri == "urai://providers":
            payload = {"providers": provider_summary()}
        elif uri == "urai://models":
            payload = {"models": [m.to_dict() for m in self.router.all_models()]}
        elif uri == "urai://stats":
            payload = self.router.status()
        else:
            raise ValueError(f"unknown resource: {uri}")
        return {"contents": [{"uri": uri, "mimeType": "application/json", "text": json.dumps(payload, indent=2)}]}

    def _get_prompt(self, params: Dict[str, Any]) -> Dict[str, Any]:
        name = params.get("name")
        if name != "route_request":
            raise ValueError(f"unknown prompt: {name}")
        arguments = params.get("arguments") or {}
        task = arguments.get("task", "the task")
        text = (
            "Use the urai_* MCP tools to complete the task. Prefer `urai_chat` with model `auto` "
            "so free OpenCode Zen models are used first. If a key is rate limited, call `urai_health`, "
            "then retry; the router rotates keys automatically.\n\nTask: " + str(task)
        )
        return {
            "description": "Route a task through Urai Router",
            "messages": [{"role": "user", "content": {"type": "text", "text": text}}],
        }

    # ------------------------------------------------------------------- loop
    def run(self) -> None:
        for raw in sys.stdin:
            line = raw.strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except ValueError:
                response = self._error(None, -32700, "parse error")
                sys.stdout.write(json.dumps(response) + "\n")
                sys.stdout.flush()
                continue
            if isinstance(message, list):
                responses = [self.handle(item) for item in message]
                payload = [r for r in responses if r is not None]
                if payload:
                    sys.stdout.write(json.dumps(payload) + "\n")
                    sys.stdout.flush()
                continue
            response = self.handle(message)
            if response is not None:
                sys.stdout.write(json.dumps(response) + "\n")
                sys.stdout.flush()


def run_mcp(cfg: Config, store: KeyStore, verbose: bool = False) -> None:
    McpServer(cfg, store, verbose=verbose).run()
