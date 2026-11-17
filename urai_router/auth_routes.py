"""Credential-profile routing for non-LLM services (Colab, Kaggle, media).

The existing KeyStore rotates LLM API keys. This module complements it with
*auth profiles*: named credential sources for tools that authenticate
differently (OAuth session files, ADC, env keys, mounted token files).

Design rules:
  * profiles contain NAMES and PATHS only — never secret values;
  * detection is presence-only (no network calls, no token echo);
  * selection is deterministic (priority, then round-robin) and honours an
    explicit preferred profile so callers can switch credentials on demand;
  * state is persisted mode 600 next to the router's other state.
"""

from __future__ import annotations

import os
import queue
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .util import now, read_json, state_dir, write_json


@dataclass
class AuthProfile:
    name: str
    service: str                       # colab | kaggle | media | urai | llm
    kind: str                          # oauth-session | adc | env | file | router-keys
    priority: int = 10
    env_keys: Tuple[str, ...] = ()     # all required when require_all_env=True
    require_all_env: bool = True
    paths: Tuple[str, ...] = ()        # any existing path satisfies the profile
    binary: Optional[str] = None       # required executable on PATH
    home: Optional[str] = None         # isolated HOME for multi-account OAuth
    description: str = ""

    @property
    def marker(self) -> Path:
        """Presence file written after a successful browser auth for this profile."""
        base = Path(self.home) if self.home else _home()
        return base / ".config" / "colab-cli" / "urai-auth-ok"

    def public(self, available: bool, missing: Sequence[str]) -> Dict[str, Any]:
        return {
            "name": self.name,
            "service": self.service,
            "kind": self.kind,
            "priority": self.priority,
            "available": available,
            "missing": list(missing),
            "description": self.description,
            "requires": {
                "env": list(self.env_keys),
                "paths": list(self.paths),
                "binary": self.binary,
            },
        }


def _home() -> Path:
    return Path(os.path.expanduser("~"))


def default_profiles(keys_path: Optional[Path] = None) -> List[AuthProfile]:
    home = _home()
    return [
        AuthProfile(
            name="colab-oauth", service="colab", kind="oauth-session", priority=10,
            binary="colab",
            paths=(str(home / ".config/colab-cli/urai-auth-ok"),),
            description="Default Google account (browser auth via `auth launch colab-oauth`)",
        ),
        *[
            AuthProfile(
                name=f"colab-oauth-{n}", service="colab", kind="oauth-session",
                priority=10 + n,
                binary="colab",
                home=str(home / ".urai" / "colab-profiles" / f"colab-oauth-{n}"),
                paths=(str(home / ".urai" / "colab-profiles" / f"colab-oauth-{n}"
                           / ".config/colab-cli/urai-auth-ok"),),
                description=f"Additional Google account #{n} — isolated HOME + keyring",
            )
            for n in range(2, int(os.environ.get("URAI_COLAB_PROFILES", "4")) + 1)
        ],
        AuthProfile(
            name="colab-adc", service="colab", kind="adc", priority=20,
            binary="colab",
            env_keys=("GOOGLE_APPLICATION_CREDENTIALS",),
            require_all_env=False,
            paths=(str(home / ".config/gcloud/application_default_credentials.json"),),
            description="Application Default Credentials (headless)",
        ),
        AuthProfile(
            name="kaggle-file", service="kaggle", kind="file", priority=10,
            paths=(str(home / ".kaggle/kaggle.json"),),
            description="Kaggle API token file (must be mode 600)",
        ),
        AuthProfile(
            name="kaggle-env", service="kaggle", kind="env", priority=20,
            env_keys=("KAGGLE_USERNAME", "KAGGLE_KEY"),
            description="KAGGLE_USERNAME + KAGGLE_KEY environment variables",
        ),
        AuthProfile(
            name="dashscope", service="media", kind="env", priority=10,
            env_keys=("DASHSCOPE_API_KEY",),
            description="Wan 3.0 video relay key",
        ),
        AuthProfile(
            name="ark", service="media", kind="env", priority=20,
            env_keys=("ARK_API_KEY",),
            description="Seedream 4.0 image relay key",
        ),
        AuthProfile(
            name="urai-api", service="urai", kind="env", priority=10,
            env_keys=("URAI_API_KEY", "URAI_MASTER_KEY"),
            require_all_env=False,
            description="Local Urai API key / master key",
        ),
        AuthProfile(
            name="llm-keys", service="llm", kind="router-keys", priority=10,
            paths=(str(keys_path) if keys_path else str(home / ".config/urai-router/keys.txt"),),
            description="Existing urai-router keys.txt (LLM key rotation)",
        ),
    ]


class AuthRouter:
    """Detect and switch credential profiles without exposing their values."""

    def __init__(self, keys_path: Optional[Path] = None, state_path: Optional[Path] = None):
        self.state_path = Path(state_path) if state_path else state_dir() / "auth-routes.json"
        self.profiles: List[AuthProfile] = default_profiles(keys_path)
        self._active: Dict[str, str] = {}
        self._rr: Dict[str, int] = {}
        self._lock = threading.RLock()
        self._load()

    # ---------------------------------------------------------------- state
    def _load(self) -> None:
        data = read_json(self.state_path, {}) or {}
        if isinstance(data.get("active"), dict):
            self._active = {str(k): str(v) for k, v in data["active"].items()}
        if isinstance(data.get("rotation"), dict):
            self._rr = {str(k): int(v) for k, v in data["rotation"].items()}

    def _save(self) -> None:
        write_json(self.state_path, {
            "version": 1,
            "updated_at": now(),
            "active": self._active,
            "rotation": self._rr,
        }, mode=0o600)

    # -------------------------------------------------------------- detect
    def detect(self, name: Optional[str] = None) -> List[Dict[str, Any]]:
        rows: List[Dict[str, Any]] = []
        for profile in self.profiles:
            if name and profile.name != name:
                continue
            missing: List[str] = []
            if profile.binary and not shutil.which(profile.binary):
                missing.append(f"binary:{profile.binary}")
            key_present = [bool(os.environ.get(k)) for k in profile.env_keys]
            if profile.env_keys:
                ok = all(key_present) if profile.require_all_env else any(key_present)
                if not ok:
                    for key, present in zip(profile.env_keys, key_present):
                        if not present:
                            missing.append(f"env:{key}")
            if profile.paths and not any(Path(p).exists() for p in profile.paths):
                missing.append("path:" + ",".join(profile.paths))
            rows.append(profile.public(available=not missing, missing=missing))
        return rows

    def available(self, service: str) -> List[AuthProfile]:
        by_name = {row["name"]: row for row in self.detect()}
        return [p for p in self.profiles
                if p.service == service and by_name.get(p.name, {}).get("available")]

    def active(self, service: str) -> Optional[str]:
        with self._lock:
            return self._active.get(service)

    def switch(self, name: str) -> Dict[str, Any]:
        profile = next((p for p in self.profiles if p.name == name), None)
        if not profile:
            return {"ok": False, "error": f"unknown profile: {name}"}
        row = next(r for r in self.detect(name) if r["name"] == name)
        if not row["available"]:
            return {"ok": False, "error": f"profile not available: {name}", "missing": row["missing"]}
        with self._lock:
            self._active[profile.service] = profile.name
            self._save()
        return {"ok": True, "service": profile.service, "active": profile.name}

    def select(self, service: str, preferred: Optional[str] = None,
               exclude: Sequence[str] = ()) -> Optional[AuthProfile]:
        """Pick a usable profile. Explicit `preferred` wins when available."""
        pool = [p for p in self.available(service) if p.name not in set(exclude)]
        if not pool:
            return None
        if preferred:
            # An explicit credential request is a contract: if it is not
            # usable, report failure instead of silently using a different
            # identity. Callers can then retry without `preferred`.
            for profile in pool:
                if profile.name == preferred:
                    return profile
            return None
        pinned = self.active(service)
        if pinned:
            for profile in pool:
                if profile.name == pinned:
                    return profile
        pool.sort(key=lambda p: (-p.priority, p.name))
        if len(pool) == 1:
            return pool[0]
        with self._lock:
            index = self._rr.get(service, 0)
            self._rr[service] = index + 1
            self._save()
        return pool[index % len(pool)]

    # ---------------------------------------------------------- multi-auth
    def launch(self, name: str, code: Optional[str] = None,
               open_browser: bool = True, wait_seconds: int = 180) -> Dict[str, Any]:
        """Authenticate a Colab profile in a browser, in an isolated HOME.

        Each Google account gets its own HOME (and therefore its own keyring
        token) and its own session-state file, so profiles can be switched
        without overwriting each other. Pass `code` for scripted auth; with no
        code the caller is prompted interactively after the browser opens.
        """
        profile = next((p for p in self.profiles if p.name == name), None)
        if not profile:
            return {"ok": False, "error": f"unknown profile: {name}"}
        if profile.service != "colab" or profile.kind != "oauth-session":
            return {"ok": False, "error": f"profile is not a Colab OAuth profile: {name}"}
        if not shutil.which("colab"):
            return {"ok": False, "error": "colab CLI not found on PATH (see docs/COLAB_GPU.md)"}

        home = Path(profile.home) if profile.home else _home()
        config = home / ".config" / "colab-cli" / "sessions.json"
        config.parent.mkdir(parents=True, exist_ok=True)
        env = os.environ.copy()
        env["HOME"] = str(home)  # isolates the keyring token per account

        try:
            proc = subprocess.Popen(
                ["colab", "--config", str(config), "usage"],
                env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, text=True, bufsize=1,
            )
        except OSError as exc:
            return {"ok": False, "error": f"cannot start colab: {exc}"}

        output: List[str] = []
        lines: "queue.Queue[Optional[str]]" = queue.Queue()

        def _reader() -> None:
            assert proc.stdout is not None
            for line in proc.stdout:
                output.append(line)
                lines.put(line)
            lines.put(None)

        threading.Thread(target=_reader, daemon=True).start()

        url: Optional[str] = None
        deadline = time.time() + 45
        while time.time() < deadline:
            try:
                line = lines.get(timeout=1)
            except queue.Empty:
                if proc.poll() is not None:
                    break
                continue
            if line is None:
                break
            if "https://accounts.google.com/o/oauth2/auth" in line:
                url = line.strip()
                break

        opened: Optional[str] = None
        if url and open_browser:
            for candidate in ("termux-open-url", "xdg-open"):
                exe = shutil.which(candidate)
                if exe:
                    try:
                        subprocess.run([exe, url], check=False, capture_output=True, timeout=15)
                        opened = candidate
                    except (OSError, subprocess.TimeoutExpired):
                        pass
                    break
            if not opened:
                try:
                    import webbrowser
                    webbrowser.open(url)
                    opened = "webbrowser"
                except Exception:
                    pass

        answer = (code or "").strip()
        if not answer:
            try:
                answer = input("Paste the authorization code from the browser: ").strip()
            except EOFError:
                proc.kill()
                return {"ok": False, "error": "no code supplied; pass --code", "browser": opened}
        if not answer:
            proc.kill()
            return {"ok": False, "error": "empty authorization code", "browser": opened}
        try:
            assert proc.stdin is not None
            proc.stdin.write(answer + "\n")
            proc.stdin.flush()
            proc.stdin.close()
        except OSError as exc:
            proc.kill()
            return {"ok": False, "error": f"cannot send code: {exc}", "browser": opened}

        try:
            rc = proc.wait(timeout=wait_seconds)
        except subprocess.TimeoutExpired:
            proc.kill()
            return {"ok": False, "error": f"auth timed out after {wait_seconds}s", "browser": opened}

        if rc != 0:
            return {"ok": False, "profile": name, "exit": rc, "browser": opened,
                    "output": "".join(output)[-1200:]}
        profile.marker.parent.mkdir(parents=True, exist_ok=True)
        profile.marker.write_text("ok\n", encoding="utf-8")
        try:
            profile.marker.chmod(0o600)
        except OSError:
            pass
        return {"ok": True, "profile": name, "browser": opened, "marker": str(profile.marker)}

    def status(self) -> Dict[str, Any]:
        rows = self.detect()
        services: Dict[str, Any] = {}
        for row in rows:
            svc = services.setdefault(row["service"], {"profiles": 0, "available": 0,
                                                       "active": None, "recommended": None})
            svc["profiles"] += 1
            if row["available"]:
                svc["available"] += 1
                if svc["recommended"] is None:
                    svc["recommended"] = row["name"]
            if self.active(row["service"]) == row["name"]:
                svc["active"] = row["name"]
        return {
            "state_file": str(self.state_path),
            "services": services,
            "profiles": rows,
        }
