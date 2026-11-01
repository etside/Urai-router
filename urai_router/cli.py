"""Command line interface for Urai Router."""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import config_writer
from .config import Config, load_config, resolve_keys_file
from .keys import UNKNOWN, KeyStore, write_example_keys
from .providers import PROVIDERS, probe_key, provider_summary
from .router import Router
from .util import VERSION, now

COMMON_DEFAULTS = argparse.SUPPRESS


def _common() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--keys", help="keys.txt path (default: auto-discover)")
    parser.add_argument("--config", help="config.json path")
    parser.add_argument("--state-dir", help="state directory override")
    parser.add_argument("-v", "--verbose", action="store_true", default=COMMON_DEFAULTS, help="verbose logging")
    return parser


def build_parser() -> argparse.ArgumentParser:
    common = _common()
    parser = argparse.ArgumentParser(
        prog="urai-router",
        description="Automatic API-key router + MCP server for OpenCode, Kiro and OpenAI-compatible clients.",
        parents=[common],
    )
    parser.add_argument("--version", action="version", version=f"urai-router {VERSION}")
    sub = parser.add_subparsers(dest="command", required=True)

    def add(name: str, help_text: str, **kwargs: Any) -> argparse.ArgumentParser:
        return sub.add_parser(name, help=help_text, parents=[common], **kwargs)

    p = add("init", "Create a keys.txt template and config directory")
    p.add_argument("--path", help="where to create keys.txt")
    p.add_argument("--force", action="store_true", help="overwrite an existing file")

    p = add("serve", "Run the local OpenAI-compatible proxy (foreground)")
    p.add_argument("--host")
    p.add_argument("--port", type=int)
    p.add_argument("--token", help="require this Bearer token from local clients")
    p.add_argument("--no-detect", action="store_true", help="skip probing unknown keys")
    p.add_argument("--no-preload", action="store_true", help="do not refresh catalogues at startup")

    add("mcp", "Run the MCP stdio server")

    p = add("keys", "Inspect, validate and auto-detect keys")
    keys_sub = p.add_subparsers(dest="keys_command", required=True)
    keys_sub.add_parser("list", help="list masked keys and health", parents=[common])
    kp = keys_sub.add_parser("check", help="probe keys with a tiny request", parents=[common])
    kp.add_argument("--provider")
    kp.add_argument("--timeout", type=float, default=12.0)
    keys_sub.add_parser("detect", help="auto-detect providers for unlabelled keys", parents=[common])

    p = add("models", "List the merged model catalogue")
    p.add_argument("--free", action="store_true", help="free models only")
    p.add_argument("--provider")
    p.add_argument("--refresh", action="store_true")
    p.add_argument("--limit", type=int, default=100)
    p.add_argument("--json", action="store_true")

    p = add("setup", "Write OpenCode, Kiro and skill configuration")
    p.add_argument("target", nargs="?", default="all", choices=["opencode", "kiro", "skills", "all"])
    p.add_argument("--project", action="store_true", help="write project-local OpenCode/skill files")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--set-default", action="store_true", help="set OpenCode's default model to a free Zen model")
    p.add_argument("--opencode-path", help="explicit opencode.json/jsonc path")
    p.add_argument("--start", action="store_true", help="start the background proxy after setup")

    p = add("sync", "Refresh catalogues and regenerate OpenCode's model list")
    p.add_argument("--set-default", action="store_true")
    p.add_argument("--opencode-path")

    p = add("doctor", "Check keys, configs, MCP and proxy health")
    p.add_argument("--json", action="store_true")

    p = add("test", "Send one tiny completion through the router")
    p.add_argument("--model")
    p.add_argument("--prompt", default="Reply with the single word: pong")
    p.add_argument("--max-tokens", type=int, default=32)
    p.add_argument("--json", action="store_true")

    p = add("explain", "Explain how a model would be routed")
    p.add_argument("--model")

    p = add("export", "Print generated configuration for another tool")
    p.add_argument("target", choices=["opencode", "kiro", "env", "mcp", "providers"], default="opencode", nargs="?")
    p.add_argument("--set-default", action="store_true")

    p = add("proxy", "Manage the background proxy")
    proxy_sub = p.add_subparsers(dest="proxy_command", required=True)
    proxy_sub.add_parser("status", help="show proxy status", parents=[common])
    proxy_sub.add_parser("start", help="start the proxy in the background", parents=[common])
    proxy_sub.add_parser("stop", help="stop the background proxy", parents=[common])

    add("version", "Print the version")
    return parser


def _context(args: argparse.Namespace) -> tuple[Config, KeyStore]:
    cfg = load_config(getattr(args, "config", None))
    for field_name in ("host", "port", "token"):
        value = getattr(args, field_name, None)
        if value not in (None, ""):
            cfg.set(field_name, value)
    if getattr(args, "no_detect", False):
        cfg.set("detect_unknown_keys", False)
    if getattr(args, "state_dir", None):
        os.environ["URAI_STATE_DIR"] = str(Path(args.state_dir).expanduser())
    from .providers import apply_provider_overrides

    apply_provider_overrides(cfg.provider_overrides)
    keys_file = resolve_keys_file(getattr(args, "keys", None) or cfg.get("keys_file"))
    store = KeyStore(keys_file, cfg)
    return cfg, store


def _router(cfg: Config, store: KeyStore) -> Router:
    return Router(cfg, store)


def _print_json(value: Any) -> None:
    print(json.dumps(value, indent=2, ensure_ascii=False))


# --------------------------------------------------------------------- commands
def cmd_init(args: argparse.Namespace) -> int:
    cfg = load_config(getattr(args, "config", None))
    path = Path(args.path).expanduser() if args.path else resolve_keys_file(cfg.get("keys_file"))
    existed = path.exists()
    if existed and not args.force:
        print(f"keys file already exists: {path}")
    else:
        write_example_keys(path)
        print(f"created keys file template: {path}")
    if not cfg.path.exists():
        cfg.path.parent.mkdir(parents=True, exist_ok=True)
        from .util import write_json

        write_json(cfg.path, cfg.data, mode=0o600)
        print(f"created config: {cfg.path}")
    print("\nAdd one key per line, for example:\n  opencode:sk-...\n  openrouter:sk-or-v1-...\n  groq:gsk_...")
    print(f"\nThen run: urai-router setup all --start\nKeys file: {path}")
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    cfg, store = _context(args)
    router = _router(cfg, store)
    if not getattr(args, "no_preload", False):
        threading.Thread(target=router.refresh_catalog, kwargs={"force": False}, daemon=True).start()
    from .proxy import serve

    serve(router, cfg, host=getattr(args, "host", None), port=getattr(args, "port", None))
    return 0


def cmd_mcp(args: argparse.Namespace) -> int:
    cfg, store = _context(args)
    from .mcp_server import run_mcp

    run_mcp(cfg, store, verbose=bool(getattr(args, "verbose", False)))
    return 0


def _fmt_key_rows(stats: Dict[str, Any]) -> str:
    lines: List[str] = []
    if not stats["providers"]:
        lines.append(f"no keys loaded from {stats['keys_file']}")
    for pid, row in sorted(stats["providers"].items()):
        lines.append(f"{pid:12} ready {row['ready']}/{row['total']}  cooling {row['cooling']}  disabled {row['disabled']}")
        for key in row["keys"]:
            lines.append(f"  {key['key']:18} {key['label']:12} {key['status']:14} ok={key['successes']} fail={key['failures']} {key['last_error']}")
    if stats.get("issues"):
        lines.append("\nparse issues:")
        for issue in stats["issues"]:
            lines.append(f"  line {issue['line']}: {issue['reason']} -> {issue['text'][:60]}")
    if stats.get("unknown"):
        lines.append(f"\n{stats['unknown']} key(s) have an unknown provider; run `urai-router keys detect`.")
    return "\n".join(lines)


def cmd_keys(args: argparse.Namespace) -> int:
    cfg, store = _context(args)
    command = args.keys_command
    if command == "list":
        print(_fmt_key_rows(store.stats()))
        return 0
    if command == "detect":
        results = store.detect_unknown(force=True)
        if not results:
            print("all keys already have a provider")
            return 0
        for fp, pid in results.items():
            print(f"{fp}: {pid}")
        unknown = [k for k in store.keys if k.provider == UNKNOWN]
        if unknown:
            print(f"\n{len(unknown)} key(s) could not be identified. Prefix them explicitly, e.g. openrouter:sk-...")
            return 1
        return 0
    if command == "check":
        rows = []
        keys = [k for k in store.keys if not args.provider or k.provider == args.provider]
        if not keys:
            print(f"no keys to check in {store.path}")
            return 1

        def check_one(key: Any) -> Dict[str, Any]:
            provider = PROVIDERS.get(key.provider)
            if not provider:
                return {"key": key, "ok": False, "error": "unknown provider"}
            ok = probe_key(provider, key.value, timeout=args.timeout)
            return {"key": key, "ok": ok, "error": "" if ok else "probe failed"}

        with ThreadPoolExecutor(max_workers=min(8, len(keys))) as pool:
            futures = [pool.submit(check_one, key) for key in keys]
            for future in as_completed(futures):
                rows.append(future.result())
        rows.sort(key=lambda row: (row["key"].provider, row["key"].label))
        failed = 0
        for row in rows:
            key = row["key"]
            status = "ok" if row["ok"] else "FAILED"
            if row["ok"]:
                store.mark_success(key)
            else:
                failed += 1
            print(f"{status:6} {key.provider:12} {key.label or key.id[:8]:12} {key.masked} {row['error']}")
        store.save_state(force=True)
        return 0 if failed == 0 else 1
    return 2


def cmd_models(args: argparse.Namespace) -> int:
    cfg, store = _context(args)
    router = _router(cfg, store)
    models = router.usable_models(force=args.refresh)
    if args.free:
        models = [m for m in models if m.free]
    if args.provider:
        models = [m for m in models if m.provider == args.provider]
    models = models[: max(1, args.limit)]
    if args.json:
        _print_json([m.to_dict() for m in models])
        return 0
    if not models:
        print("no models found. Add keys with `urai-router init`, then `urai-router models --refresh`.")
        return 1
    print(f"{'FREE':4} {'PROVIDER':12} {'MODEL':52} {'CTX':>8} TOOLS")
    for model in models:
        print(
            f"{'yes' if model.free else '-':4} {model.provider:12} {model.qualified[:52]:52} "
            f"{model.context or 0:8} {'yes' if model.tools else 'no'}"
        )
    return 0


def cmd_setup(args: argparse.Namespace) -> int:
    cfg, store = _context(args)
    router = _router(cfg, store)
    report = config_writer.setup(
        cfg,
        store,
        router,
        target=args.target,
        project=args.project,
        set_default=args.set_default,
        dry_run=args.dry_run,
        explicit=args.opencode_path,
    )
    if args.start and not args.dry_run:
        from .proxy import ensure_proxy

        report["proxy"] = ensure_proxy(router, cfg)
    _print_json(report)
    if not store.keys:
        print(f"\nNo keys yet. Add lines to {store.path} then re-run `urai-router setup all --start`.", file=sys.stderr)
    return 0


def cmd_sync(args: argparse.Namespace) -> int:
    cfg, store = _context(args)
    router = _router(cfg, store)
    count = router.refresh_catalog(force=True)
    report = config_writer.setup(
        cfg,
        store,
        router,
        target="opencode",
        set_default=args.set_default,
        explicit=args.opencode_path,
    )
    _print_json({"models": count, "opencode": report})
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    cfg, store = _context(args)
    router = _router(cfg, store)
    from .proxy import proxy_status

    keys_stat = os.stat(store.path) if store.path.exists() else None
    mode = oct(keys_stat.st_mode & 0o777) if keys_stat else None
    world_readable = bool(keys_stat and (keys_stat.st_mode & 0o077))
    opencode_path = config_writer.load_opencode_config(getattr(args, "opencode_path", None))
    opencode_cfg = config_writer.load_jsonc(opencode_path)
    kiro_path = config_writer.kiro_mcp_path()
    kiro_cfg = config_writer.load_jsonc(kiro_path)
    skill = config_writer.skill_source()
    stats = store.stats()
    report = {
        "version": VERSION,
        "python": sys.version.split()[0],
        "keys_file": {
            "path": str(store.path),
            "exists": store.path.exists(),
            "total": stats["total"],
            "unknown": stats["unknown"],
            "providers": {pid: {"ready": row["ready"], "total": row["total"], "cooling": row["cooling"]} for pid, row in stats["providers"].items()},
            "mode": mode,
            "warning": "keys file is readable by other users; chmod 600" if world_readable else None,
        },
        "catalog": {
            pid: {"models": len(models), "free": len([m for m in models if m.free]), "stale": router.catalog.stale(pid), "error": router.catalog.errors.get(pid)}
            for pid, models in router.catalog.models.items()
        },
        "proxy": proxy_status(cfg),
        "opencode": {
            "path": str(opencode_path),
            "exists": opencode_path.exists(),
            "has_provider": config_writer.PROVIDER_ID in (opencode_cfg.get("provider") or {}),
            "has_mcp": config_writer.PROVIDER_ID in (opencode_cfg.get("mcp") or {}),
            "model": opencode_cfg.get("model"),
        },
        "kiro": {
            "path": str(kiro_path),
            "exists": kiro_path.exists(),
            "has_server": config_writer.PROVIDER_ID in (kiro_cfg.get("mcpServers") or {}),
            "cli": bool(__import__("shutil").which("kiro-cli")),
        },
        "skill": {"source": str(skill), "exists": skill.exists()},
        "config": str(cfg.path),
    }
    if args.json:
        _print_json(report)
    else:
        print(f"Urai Router {VERSION}  python {report['python']}")
        print(f"keys file : {store.path} ({'ok' if store.path.exists() else 'MISSING'}, {stats['total']} keys, mode {mode})")
        if world_readable:
            print("  WARNING: chmod 600 this file; it contains secrets")
        if stats["unknown"]:
            print(f"  WARNING: {stats['unknown']} key(s) unknown; run `urai-router keys detect`")
        for pid, row in report["keys_file"]["providers"].items():
            print(f"  {pid:12} {row['ready']}/{row['total']} ready")
        print(f"proxy     : {report['proxy'].get('url', 'not running')}")
        print(f"opencode  : {opencode_path} provider={'yes' if report['opencode']['has_provider'] else 'no'} mcp={'yes' if report['opencode']['has_mcp'] else 'no'}")
        print(f"kiro      : {kiro_path} server={'yes' if report['kiro']['has_server'] else 'no'} cli={'yes' if report['kiro']['cli'] else 'no'}")
        print(f"skill     : {'ok' if report['skill']['exists'] else 'MISSING'} {skill}")
        if report["catalog"]:
            print("catalog   :")
            for pid, row in report["catalog"].items():
                extra = f" error={row['error']}" if row["error"] else ""
                print(f"  {pid:12} {row['models']:4} models, {row['free']:3} free{extra}")
        else:
            print("catalog   : empty (run `urai-router models --refresh`)")
        print(f"config    : {cfg.path}")
    problems = 0
    if not store.path.exists() or stats["total"] == 0:
        problems += 1
    if stats["unknown"]:
        problems += 1
    if world_readable:
        problems += 1
    return 0 if problems == 0 else 1


def cmd_test(args: argparse.Namespace) -> int:
    cfg, store = _context(args)
    router = _router(cfg, store)
    body = {
        "model": args.model or cfg.get("default_model", "auto"),
        "messages": [{"role": "user", "content": args.prompt}],
        "max_tokens": args.max_tokens,
        "stream": False,
    }
    result = router.chat(body)
    if args.json:
        _print_json({"status": result.status, "provider": result.provider, "model": result.model, "key": result.key_label, "body": result.body.decode("utf-8", "replace"), "attempts": result.attempts})
        return 0 if result.ok else 1
    if not result.ok:
        print(f"FAILED {result.status}: {result.body.decode('utf-8', 'replace')}")
        for attempt in result.attempts:
            print(f"  attempt {attempt}")
        return 1
    try:
        payload = json.loads(result.body)
        message = ((payload.get("choices") or [{}])[0]).get("message") or {}
        content = message.get("content") or ""
    except ValueError:
        content = result.body.decode("utf-8", "replace")
    print(content)
    print(f"\n[{result.provider}/{result.model} via {result.key_label}, {len(result.attempts)} attempt(s)]", file=sys.stderr)
    return 0


def cmd_explain(args: argparse.Namespace) -> int:
    cfg, store = _context(args)
    router = _router(cfg, store)
    _print_json(router.explain(args.model))
    return 0


def cmd_export(args: argparse.Namespace) -> int:
    cfg, store = _context(args)
    router = _router(cfg, store)
    if args.target == "opencode":
        _print_json(config_writer.export_opencode(cfg, store, router, set_default=args.set_default))
    elif args.target == "kiro":
        _print_json(config_writer.export_kiro(cfg, store))
    elif args.target == "mcp":
        _print_json({"mcpServers": config_writer.export_kiro(cfg, store)["mcpServers"]})
    elif args.target == "providers":
        _print_json({"providers": provider_summary()})
    else:
        for name, value in config_writer.export_env(cfg).items():
            print(f"export {name}={value!r}")
    return 0


def cmd_proxy(args: argparse.Namespace) -> int:
    cfg, store = _context(args)
    router = _router(cfg, store)
    from .proxy import ensure_proxy, proxy_status, runtime_path

    if args.proxy_command == "status":
        _print_json(proxy_status(cfg))
        return 0
    if args.proxy_command == "start":
        _print_json(ensure_proxy(router, cfg))
        return 0
    if args.proxy_command == "stop":
        info = proxy_status(cfg)
        pid = info.get("pid")
        if not info.get("running") or not pid:
            print("proxy is not running")
            return 1
        try:
            os.kill(int(pid), signal.SIGTERM)
        except OSError as exc:
            print(f"could not stop pid {pid}: {exc}")
            return 1
        try:
            runtime_path().unlink()
        except OSError:
            pass
        print(f"stopped urai-router proxy (pid {pid})")
        return 0
    return 2


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    command = args.command
    try:
        if command == "version":
            print(f"urai-router {VERSION}")
            return 0
        handlers = {
            "init": cmd_init,
            "serve": cmd_serve,
            "mcp": cmd_mcp,
            "keys": cmd_keys,
            "models": cmd_models,
            "setup": cmd_setup,
            "sync": cmd_sync,
            "doctor": cmd_doctor,
            "test": cmd_test,
            "explain": cmd_explain,
            "export": cmd_export,
            "proxy": cmd_proxy,
        }
        handler = handlers.get(command)
        if not handler:
            parser.print_help()
            return 2
        return handler(args)
    except KeyboardInterrupt:
        return 130
    except BrokenPipeError:
        return 0
    except Exception as exc:  # noqa: BLE001
        if getattr(args, "verbose", False):
            raise
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
