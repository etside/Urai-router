"""keys.txt parsing, health tracking, cooldowns and automatic detection."""

from __future__ import annotations

import os
import shlex
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from .config import Config
from .providers import PROVIDERS, detect_by_prefix, probe_key, provider_id
from .util import expand_env, fingerprint, now, read_json, state_dir, write_json

UNKNOWN = "unknown"


@dataclass
class Key:
    provider: str
    value: str
    label: str = ""
    weight: int = 1
    disabled: bool = False
    source: str = ""
    # runtime, keyed by fingerprint and persisted without the secret
    successes: int = 0
    failures: int = 0
    strikes: int = 0
    cooldown_until: float = 0.0
    last_used: float = 0.0
    last_error: str = ""

    @property
    def id(self) -> str:
        return fingerprint(self.value)

    @property
    def masked(self) -> str:
        if len(self.value) <= 10:
            return self.value[:2] + "***"
        return self.value[:6] + "..." + self.value[-4:]

    def available(self, at: Optional[float] = None) -> bool:
        return not self.disabled and self.cooldown_until <= (at if at is not None else now())

    def status(self, at: Optional[float] = None) -> str:
        if self.disabled:
            return "disabled"
        remaining = self.cooldown_until - (at if at is not None else now())
        if remaining > 0:
            return f"cooling {int(remaining)}s"
        return "ready"

    def public(self, at: Optional[float] = None) -> Dict[str, Any]:
        return {
            "provider": self.provider,
            "label": self.label or self.id[:8],
            "key": self.masked,
            "fingerprint": self.id,
            "weight": self.weight,
            "disabled": self.disabled,
            "status": self.status(at),
            "successes": self.successes,
            "failures": self.failures,
            "strikes": self.strikes,
            "cooldown_until": round(self.cooldown_until, 1),
            "last_used": round(self.last_used, 1),
            "last_error": self.last_error,
        }


@dataclass
class ParseIssue:
    line: int
    text: str
    reason: str


def _parse_annotation(text: str) -> Tuple[str, int, bool]:
    label = ""
    weight = 1
    disabled = False
    for token in shlex.split(text or ""):
        low = token.lower()
        if low in ("disabled", "off", "skip"):
            disabled = True
        elif low.startswith("label="):
            label = token.split("=", 1)[1]
        elif low.startswith("weight="):
            try:
                weight = max(0, int(token.split("=", 1)[1]))
            except ValueError:
                pass
        elif token.startswith("@"):
            label = token[1:]
        elif token.startswith("*"):
            try:
                weight = max(0, int(token[1:]))
            except ValueError:
                pass
    return label, weight or 1, disabled


def parse_key_line(line: str) -> Optional[Tuple[str, str, str, int, bool]]:
    """Parse one keys.txt line into (provider, key, label, weight, disabled)."""
    raw = line.strip()
    if not raw or raw.startswith("#"):
        return None
    main, _, comment = raw.partition("#")
    main = main.strip()
    if not main:
        return None
    label, weight, disabled = _parse_annotation(comment)

    provider = ""
    key = ""
    if ":" in main:
        head, tail = main.split(":", 1)
        if provider_id(head) or head.lower() == UNKNOWN:
            provider = provider_id(head) or UNKNOWN
            key = tail.strip()
    if not key and "=" in main:
        head, tail = main.split("=", 1)
        if provider_id(head) or head.lower() == UNKNOWN:
            provider = provider_id(head) or UNKNOWN
            key = tail.strip()
    if not key and "," in main:
        head, tail = main.split(",", 1)
        if provider_id(head) or head.lower() == UNKNOWN:
            provider = provider_id(head) or UNKNOWN
            key = tail.strip()
    if not key:
        parts = main.split(None, 1)
        if len(parts) == 2 and (provider_id(parts[0]) or parts[0].lower() == UNKNOWN):
            provider = provider_id(parts[0]) or UNKNOWN
            key = parts[1].strip()
        else:
            # bare key plus optional @label / *weight tokens
            tokens = shlex.split(main)
            if not tokens:
                return None
            key = tokens[0]
            extra_label, extra_weight, extra_disabled = _parse_annotation(" ".join(tokens[1:]))
            label = label or extra_label
            weight = extra_weight if extra_weight != 1 else weight
            disabled = disabled or extra_disabled
    key = key.strip().strip('"').strip("'")
    # annotations may sit outside the comment: `provider:key @backup *2`
    try:
        key_tokens = shlex.split(key)
    except ValueError:
        key_tokens = [key]
    if key_tokens:
        key = key_tokens[0]
        extra_label, extra_weight, extra_disabled = _parse_annotation(" ".join(key_tokens[1:]))
        label = label or extra_label
        weight = extra_weight if extra_weight != 1 else weight
        disabled = disabled or extra_disabled
    if not key:
        return None
    if not provider:
        matches = detect_by_prefix(key)
        provider = matches[0] if len(matches) == 1 else UNKNOWN
    return provider, key, label, weight, disabled


def parse_keys(text: str) -> Tuple[List[Key], List[ParseIssue]]:
    keys: List[Key] = []
    issues: List[ParseIssue] = []
    seen: Set[str] = set()
    for number, line in enumerate(text.splitlines(), 1):
        expanded = expand_env(line)
        try:
            parsed = parse_key_line(expanded)
        except ValueError as exc:
            issues.append(ParseIssue(number, line.strip(), f"cannot parse: {exc}"))
            continue
        if not parsed:
            continue
        provider, value, label, weight, disabled = parsed
        fp = fingerprint(value)
        if fp in seen:
            issues.append(ParseIssue(number, line.strip(), "duplicate key skipped"))
            continue
        seen.add(fp)
        if len(value) < 8:
            issues.append(ParseIssue(number, line.strip(), "key looks too short"))
            continue
        keys.append(Key(provider=provider, value=value, label=label, weight=weight, disabled=disabled))
    return keys, issues


class KeyStore:
    def __init__(self, path: Path, cfg: Optional[Config] = None, state_path: Optional[Path] = None):
        self.path = Path(path)
        self.cfg = cfg
        self.state_path = state_path or (state_dir() / "keys-state.json")
        self.keys: List[Key] = []
        self.issues: List[ParseIssue] = []
        self.detected: Dict[str, str] = {}
        self._rr: Dict[str, int] = {}
        self._mtime = 0.0
        self._lock = threading.RLock()
        self._last_save = 0.0
        self._detected_lock = threading.Lock()
        self.reload(force=True)

    # ------------------------------------------------------------------ load
    def _read_text(self) -> str:
        try:
            return self.path.read_text(encoding="utf-8")
        except OSError:
            return ""

    def reload(self, force: bool = False) -> bool:
        try:
            mtime = self.path.stat().st_mtime
        except OSError:
            mtime = 0.0
        with self._lock:
            if not force and mtime == self._mtime and self.keys:
                return False
            self._mtime = mtime
            runtime = {key.id: key for key in self.keys}
            state = read_json(self.state_path, {}) or {}
            if isinstance(state.get("detected"), dict):
                self.detected.update({str(k): str(v) for k, v in state["detected"].items()})
            parsed, issues = parse_keys(self._read_text())
            for key in parsed:
                old = runtime.get(key.id)
                if old:
                    key.successes = old.successes
                    key.failures = old.failures
                    key.strikes = old.strikes
                    key.cooldown_until = old.cooldown_until
                    key.last_used = old.last_used
                    key.last_error = old.last_error
                if key.provider == UNKNOWN and key.id in self.detected:
                    key.provider = self.detected[key.id]
                key.source = str(self.path)
            self.keys = parsed
            self.issues = issues
            return True

    def maybe_reload(self) -> bool:
        try:
            mtime = self.path.stat().st_mtime
        except OSError:
            mtime = 0.0
        if mtime != self._mtime:
            return self.reload(force=True)
        return False

    # --------------------------------------------------------------- access
    def providers(self) -> List[str]:
        return sorted({key.provider for key in self.keys})

    def keys_for(self, provider: str) -> List[Key]:
        return [key for key in self.keys if key.provider == provider]

    def available_keys(self, provider: str, exclude: Optional[Set[str]] = None) -> List[Key]:
        at = now()
        exclude = exclude or set()
        return [
            key
            for key in self.keys_for(provider)
            if key.available(at) and key.id not in exclude and key.weight > 0
        ]

    def select(self, provider: str, exclude: Optional[Set[str]] = None) -> Optional[Key]:
        exclude = exclude or set()
        with self._lock:
            pool: List[Key] = []
            for key in self.available_keys(provider, exclude):
                pool.extend([key] * max(1, min(20, key.weight)))
            if not pool:
                return None
            index = self._rr.get(provider, 0)
            key = pool[index % len(pool)]
            self._rr[provider] = index + 1
            return key

    def next_retry_after(self, provider: str) -> Optional[float]:
        times = [
            key.cooldown_until - now()
            for key in self.keys_for(provider)
            if not key.disabled and key.cooldown_until > now()
        ]
        return min(times) if times else None

    # --------------------------------------------------------------- health
    def mark_success(self, key: Key) -> None:
        with self._lock:
            key.successes += 1
            key.strikes = 0
            key.cooldown_until = 0.0
            key.last_used = now()
            key.last_error = ""
        self.save_state()

    def mark_failure(self, key: Key, kind: str, retry_after: Optional[float] = None) -> None:
        cooldowns = self.cfg.cooldowns if self.cfg else {"rate": 60, "invalid": 1800, "forbidden": 1800, "server": 20, "network": 15, "max": 3600}
        with self._lock:
            key.failures += 1
            key.strikes += 1
            key.last_used = now()
            key.last_error = kind
            base = float(cooldowns.get(kind, cooldowns.get("server", 20)))
            ceiling = float(cooldowns.get("max", 3600))
            if retry_after and retry_after > 0:
                delay = min(retry_after, ceiling)
            elif kind in ("invalid", "forbidden"):
                delay = min(base, ceiling)
            else:
                delay = min(base * (2 ** max(0, key.strikes - 1)), ceiling)
            key.cooldown_until = now() + delay
        self.save_state()

    def mark_disabled(self, key: Key, disabled: bool = True) -> None:
        with self._lock:
            key.disabled = disabled
        self.save_state()

    # -------------------------------------------------------------- detect
    def detect_unknown(self, force: bool = False, timeout: float = 10.0) -> Dict[str, str]:
        unknown = [key for key in self.keys if key.provider == UNKNOWN]
        if not unknown:
            return {}
        with self._detected_lock:
            pending = [key for key in unknown if force or key.id not in self.detected]
            if not pending:
                return {k.id: k.provider for k in unknown}
            results: Dict[str, str] = {}

            def probe_one(key: Key) -> Tuple[str, str]:
                candidates = detect_by_prefix(key.value)
                if not candidates:
                    candidates = ["opencode", "openrouter", "openai", "deepseek", "groq", "anthropic", "google"]
                for pid in candidates:
                    provider = PROVIDERS.get(pid)
                    if not provider or provider.keyless:
                        continue
                    if probe_key(provider, key.value, timeout=timeout):
                        return key.id, pid
                return key.id, UNKNOWN

            with ThreadPoolExecutor(max_workers=min(6, len(pending))) as pool:
                futures = [pool.submit(probe_one, key) for key in pending]
                for future in as_completed(futures):
                    try:
                        fp, pid = future.result()
                    except Exception:
                        continue
                    if pid != UNKNOWN:
                        results[fp] = pid
                        self.detected[fp] = pid
            for key in self.keys:
                if key.provider == UNKNOWN and key.id in self.detected:
                    key.provider = self.detected[key.id]
            self.save_state()
            return results

    # --------------------------------------------------------------- stats
    def stats(self) -> Dict[str, Any]:
        at = now()
        by_provider: Dict[str, Dict[str, Any]] = {}
        for key in self.keys:
            row = by_provider.setdefault(
                key.provider,
                {"provider": key.provider, "total": 0, "ready": 0, "cooling": 0, "disabled": 0, "keys": []},
            )
            row["total"] += 1
            if key.disabled:
                row["disabled"] += 1
            elif key.available(at):
                row["ready"] += 1
            else:
                row["cooling"] += 1
            row["keys"].append(key.public(at))
        return {
            "keys_file": str(self.path),
            "keys_file_exists": self.path.is_file(),
            "total": len(self.keys),
            "unknown": len([k for k in self.keys if k.provider == UNKNOWN]),
            "providers": by_provider,
            "issues": [issue.__dict__ for issue in self.issues],
        }

    # --------------------------------------------------------------- state
    def save_state(self, force: bool = False) -> None:
        at = now()
        with self._lock:
            if not force and at - self._last_save < 1.0:
                return
            self._last_save = at
            payload = {
                "version": 1,
                "updated_at": at,
                "detected": self.detected,
                "rotation": self._rr,
                "keys": {
                    key.id: {
                        "provider": key.provider,
                        "successes": key.successes,
                        "failures": key.failures,
                        "strikes": key.strikes,
                        "cooldown_until": key.cooldown_until,
                        "last_used": key.last_used,
                        "last_error": key.last_error,
                        "disabled": key.disabled,
                    }
                    for key in self.keys
                },
            }
        try:
            write_json(self.state_path, payload, mode=0o600)
        except OSError:
            pass


def write_example_keys(path: Path) -> None:
    template = """# Urai Router keys file
# Format: provider:key   (the provider prefix is optional when the key is unique)
# One key per line. Never commit this file.
#
# opencode:sk-...
# openrouter:sk-or-v1-...
# groq:gsk_...
# deepseek:sk-...
# anthropic:sk-ant-...
# google:AIza...
# xai:xai-...
# openai:sk-proj-...
#
# Optional annotations in the comment:
# opencode:sk-...        # label=main weight=2
# opencode:sk-...        # label=backup weight=1
"""
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_text(template, encoding="utf-8")
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
