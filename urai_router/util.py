"""Small stdlib-only helpers used across Urai Router."""

from __future__ import annotations

import hashlib
import http.client
import json
import os
import re
import tempfile
import time
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, Optional

VERSION = "0.1.0"

MASK_RE = re.compile(r"^(.{6}).*(.{4})$")


def now() -> float:
    return time.time()


def now_ms() -> int:
    return int(time.time() * 1000)


def home() -> Path:
    return Path(os.path.expanduser("~"))


def config_dir() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or str(home() / ".config")
    return Path(base) / "urai-router"


def state_dir() -> Path:
    override = os.environ.get("URAI_STATE_DIR")
    if override:
        path = Path(os.path.expanduser(override))
    else:
        base = os.environ.get("XDG_STATE_HOME") or str(home() / ".local" / "state")
        path = Path(base) / "urai-router"
    path.mkdir(parents=True, exist_ok=True)
    return path


def cache_dir() -> Path:
    base = os.environ.get("XDG_CACHE_HOME") or str(home() / ".cache")
    path = Path(base) / "urai-router"
    path.mkdir(parents=True, exist_ok=True)
    return path


def mask_secret(value: str) -> str:
    """Mask an API key for logs/UI. Never return the full value."""
    if not value:
        return ""
    if len(value) <= 10:
        return value[:2] + "*" * max(1, len(value) - 2)
    return value[:6] + "..." + value[-4:]


def fingerprint(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


def expand_env(text: str) -> str:
    """Expand ${VAR}, $VAR and %VAR% without failing on unknown vars."""

    def _repl(match: re.Match[str]) -> str:
        name = match.group(1) or match.group(2) or match.group(3)
        return os.environ.get(name, "")

    return re.sub(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}|\$([A-Za-z_][A-Za-z0-9_]*)|%([A-Za-z_][A-Za-z0-9_]*)%", _repl, text)


def atomic_write(path: Path, data: str, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".urai-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    finally:
        try:
            if os.path.exists(tmp):
                os.unlink(tmp)
        except OSError:
            pass


def read_json(path: Path, default: Any = None) -> Any:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


def write_json(path: Path, value: Any, mode: int = 0o600) -> None:
    atomic_write(path, json.dumps(value, indent=2, sort_keys=True) + "\n", mode=mode)


def strip_jsonc(text: str) -> str:
    """Remove // and /* */ comments plus trailing commas from JSONC.

    A deliberately small scanner: it tracks strings and escapes so URLs such as
    ``https://...`` inside strings are preserved.
    """
    out: list[str] = []
    i = 0
    n = len(text)
    in_string = False
    escape = False
    while i < n:
        ch = text[i]
        if in_string:
            out.append(ch)
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            i += 1
            continue
        if ch == '"':
            in_string = True
            out.append(ch)
            i += 1
            continue
        if ch == "/" and i + 1 < n and text[i + 1] == "/":
            while i < n and text[i] not in "\r\n":
                i += 1
            continue
        if ch == "/" and i + 1 < n and text[i + 1] == "*":
            i += 2
            while i + 1 < n and not (text[i] == "*" and text[i + 1] == "/"):
                i += 1
            i += 2
            continue
        out.append(ch)
        i += 1
    cleaned = "".join(out)
    # remove trailing commas before } or ]
    cleaned = re.sub(r",(\s*[}\]])", r"\1", cleaned)
    return cleaned


def load_jsonc(path: Path) -> Dict[str, Any]:
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return {}
    try:
        value = json.loads(strip_jsonc(raw))
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def deep_merge(base: Dict[str, Any], extra: Dict[str, Any]) -> Dict[str, Any]:
    result = dict(base)
    for key, value in (extra or {}).items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def parse_seconds(value: Any, default: float) -> float:
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().lower()
    try:
        if text.endswith("ms"):
            return float(text[:-2]) / 1000.0
        if text.endswith("s"):
            return float(text[:-1])
        if text.endswith("m"):
            return float(text[:-1]) * 60
        if text.endswith("h"):
            return float(text[:-1]) * 3600
        return float(text)
    except ValueError:
        return default


def json_bytes(value: Any) -> bytes:
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def sse_event(payload: Any, event: Optional[str] = None) -> bytes:
    prefix = f"event: {event}\n" if event else ""
    if isinstance(payload, str):
        data = payload
    else:
        data = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
    return (prefix + "data: " + data + "\n\n").encode("utf-8")


@dataclass
class UpstreamResponse:
    status: int
    headers: Dict[str, str]
    reason: str
    raw: http.client.HTTPResponse
    connection: http.client.HTTPConnection

    def read(self) -> bytes:
        return self.raw.read()

    def close(self) -> None:
        try:
            self.raw.close()
        except Exception:
            pass
        try:
            self.connection.close()
        except Exception:
            pass


def http_request(
    url: str,
    method: str = "GET",
    headers: Optional[Dict[str, str]] = None,
    body: Optional[bytes] = None,
    timeout: float = 60.0,
) -> UpstreamResponse:
    """Perform one HTTP request and return a streaming-capable response."""
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError(f"unsupported URL scheme: {parsed.scheme!r}")
    host = parsed.hostname or ""
    port = parsed.port
    path = urllib.parse.urlunsplit(("", "", parsed.path or "/", parsed.query, ""))
    if parsed.scheme == "https":
        conn: http.client.HTTPConnection = http.client.HTTPSConnection(host, port, timeout=timeout)
    else:
        conn = http.client.HTTPConnection(host, port, timeout=timeout)
    hdrs = {"User-Agent": f"urai-router/{VERSION}"}
    for key, value in (headers or {}).items():
        if value is not None:
            hdrs[key] = value
    conn.request(method, path, body=body, headers=hdrs)
    resp = conn.getresponse()
    response_headers = {k.lower(): v for k, v in resp.getheaders()}
    return UpstreamResponse(resp.status, response_headers, resp.reason, resp, conn)


def request_json(
    url: str,
    method: str = "GET",
    headers: Optional[Dict[str, str]] = None,
    payload: Any = None,
    timeout: float = 60.0,
) -> tuple[int, Any, Dict[str, str]]:
    body = None
    hdrs = dict(headers or {})
    if payload is not None:
        body = json_bytes(payload)
        hdrs.setdefault("Content-Type", "application/json")
    resp = http_request(url, method=method, headers=hdrs, body=body, timeout=timeout)
    try:
        data = resp.read()
    finally:
        resp.close()
    try:
        parsed = json.loads(data.decode("utf-8", "replace")) if data else None
    except ValueError:
        parsed = None
    return resp.status, parsed, resp.headers


def iter_sse_json(response: UpstreamResponse) -> Iterable[Dict[str, Any]]:
    """Yield decoded JSON objects from an SSE stream."""
    data_lines: list[str] = []
    while True:
        line = response.raw.readline()
        if not line:
            break
        try:
            text = line.decode("utf-8", "replace").rstrip("\r\n")
        except Exception:
            continue
        if not text:
            if data_lines:
                chunk = "\n".join(data_lines)
                data_lines = []
                try:
                    yield json.loads(chunk)
                except ValueError:
                    pass
            continue
        if text.startswith(":"):
            continue
        if text.startswith("data:"):
            data_lines.append(text[5:].lstrip())
        # event:/id:/retry: lines are ignored; payload carries its own type
    if data_lines:
        chunk = "\n".join(data_lines)
        try:
            yield json.loads(chunk)
        except ValueError:
            pass


def first_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                if item.get("type") == "text":
                    parts.append(str(item.get("text", "")))
                elif "text" in item:
                    parts.append(str(item.get("text", "")))
        return "".join(parts)
    if content is None:
        return ""
    return str(content)
