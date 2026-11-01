# Urai Router

**One `keys.txt` → every LLM provider, one local endpoint, automatic key rotation.**

Urai Router is a small, dependency-free Python gateway for people who keep several
LLM API keys. It reads a plain text file, auto-detects each provider, discovers the
models each key unlocks (including **free OpenCode Zen** models), rotates keys on
`401`, `403`, `429` and `5xx`, and exposes everything as:

- an **OpenAI-compatible HTTP API** at `http://127.0.0.1:8787/v1`
- an **MCP stdio server** for OpenCode, Kiro CLI and any MCP client
- an **Agent Skill** (`SKILL.md`) that teaches an agent how to use it
- **generated OpenCode and Kiro configuration** so both tools understand the
  provider and model list without manual editing

No database. No Node modules. No Docker. Python 3.9+ standard library only.

```bash
urai-router init                      # create ~/.config/urai-router/keys.txt
$EDITOR ~/.config/urai-router/keys.txt
urai-router setup all --start         # OpenCode + Kiro + skill, start proxy
urai-router doctor
```

---

## Why this exists (audit first)

The project started with an audit of existing open-source routers. The summary is
in [`docs/AUDIT.md`](docs/AUDIT.md). In short:

| Project | Stars | License | What it is | Why not just use it |
| --- | ---: | --- | --- | --- |
| [BerriAI/litellm](https://github.com/BerriAI/litellm) | 62k | MIT-ish | Full AI gateway / SDK | Heavy, needs Postgres/Redis for teams; not a local MCP+skill |
| [songquanpeng/one-api](https://github.com/songquanpeng/one-api) | 37k | MIT | Key redistribution server | Web UI + DB, aimed at resale, not CLI agents |
| [QuantumNous/new-api](https://github.com/QuantumNous/new-api) | 51k | AGPL-3.0 | Fork of one-api | AGPL, DB, server-first |
| [musistudio/claude-code-router](https://github.com/musistudio/claude-code-router) | 38k | MIT | Claude Code routing | Node/TS, Claude-shaped, not MCP-native |
| [jasonxu114514/opencode2api](https://github.com/jasonxu114514/opencode2api) | 1.4k | GPL-3.0 | OpenCode Zen proxy | Go, GPL, Zen-only, no keys.txt/MCP config |
| [routatic/proxy](https://github.com/routatic/proxy) | 983 | AGPL-3.0 | Multi-upstream proxy | AGPL, Go, no MCP/skill surface |

None of them combine all four requirements: **plain `keys.txt`**, **free OpenCode
Zen discovery**, **MCP-native agent tools**, and **automatic OpenCode/Kiro config**.
Urai Router is deliberately the small tool that does those four things.

---

## Install

```bash
git clone https://github.com/etside/Urai-router.git
cd Urai-router
python3 -m pip install --user -e .        # or: pipx install .
urai-router --version
```

For a zero-install run: `python3 -m urai_router --help` from the repo.

---

## keys.txt

Default search order:

1. `--keys PATH`
2. `$URAI_KEYS_FILE`
3. `./keys.txt`
4. `~/.config/urai-router/keys.txt`
5. `~/.config/urai/keys.txt`
6. `~/.urai/keys.txt`
7. `~/urai-keys.txt`
8. `~/keys.txt`

```text
# provider:key    (provider prefix is optional when the key is recognizable)
opencode:sk-...
openrouter:sk-or-v1-...
groq:gsk_...
deepseek:sk-...
anthropic:sk-ant-...
google:AIza...
xai:xai-...

# labels and weights control rotation
opencode:sk-...        # label=main weight=3
opencode:sk-...        # label=backup weight=1
```

- `#` starts a comment; `label=`, `weight=`, `@label`, `*weight` are parsed.
- `${ENV_VAR}` is expanded.
- Duplicate keys are ignored.
- An unlabelled key is auto-detected by prefix, then by a 1-token probe.
- `keys.txt` is hot-reloaded when its mtime changes.
- The file is never sent anywhere and never written into logs; `keys list` shows
  masked values only.

Keep it private:

```bash
chmod 600 ~/.config/urai-router/keys.txt
```

---

## Providers understood automatically

Registered out of the box:

`opencode` (Zen), `openrouter`, `openai`, `deepseek`, `groq`, `anthropic`,
`google` (Gemini OpenAI endpoint), `xai`, `nvidia`, `moonshot`, `mistral`,
`together`, `fireworks`, `cerebras`, `huggingface`, `zai`, `minimax`,
`deepinfra`, `perplexity`, `github` models, `nebius`, `sambanova`, `cohere`,
and local `ollama` / `lmstudio` / `llamacpp`.

Add any OpenAI-compatible provider in `~/.config/urai-router/config.json`:

```json
{
  "providers": {
    "my-gateway": {
      "name": "My Gateway",
      "base_url": "https://gateway.example.com/v1",
      "api_style": "openai",
      "default_models": ["my-model"]
    }
  }
}
```

Then use `my-gateway:key...` in `keys.txt`.

### OpenCode Zen free models

`GET https://opencode.ai/zen/v1/models` is public and returns pricing metadata.
Urai Router reads it, marks `cost.text.input.price == 0` as free, and reads the
per-model `api` block so OpenAI-style models use `/chat/completions` while
Anthropic-style models use `/messages` (translated both ways, including tools
and SSE streaming).

You still need one Zen API key to call the models; the catalogue itself is public.
Free rows are time-limited by OpenCode, so run `urai-router sync` periodically.

---

## Routing

Model reference syntax:

| Reference | Meaning |
| --- | --- |
| `auto` / `best` | free models first, then any usable model |
| `free` / `urai/free` | free models only |
| `opencode/deepseek-v4.1-flash-free` | explicit provider + model |
| `openrouter/vendor/model:free` | OpenRouter model ids keep their slash |
| `deepseek-chat` | resolved across the catalogue when unambiguous |
| alias | any key in `config.json` → `aliases` |

Failover policy:

| Upstream | Action |
| --- | --- |
| `401` / `403` | mark key invalid, rotate immediately |
| `429` | honour `Retry-After`, exponential cooldown, rotate |
| `408` / `425` / `5xx` / `529` | short exponential cooldown, rotate |
| `400` / `422` | request or model problem — do not blame the key |
| `404` | try the model's alternative endpoint, then report |

Cooldowns and counters persist in
`~/.local/state/urai-router/keys-state.json` (no key material inside).

---

## Use as an OpenAI-compatible API

```bash
urai-router serve              # foreground: 127.0.0.1:8787
urai-router proxy start        # background, writes pid/port + proxy.log
urai-router proxy status
urai-router proxy stop
```

```bash
curl -s http://127.0.0.1:8787/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"auto","messages":[{"role":"user","content":"say pong"}]}'
```

```python
from openai import OpenAI

client = OpenAI(base_url="http://127.0.0.1:8787/v1", api_key="urai-local")
print(client.chat.completions.create(
    model="auto",
    messages=[{"role": "user", "content": "hello"}],
).choices[0].message.content)
```

Endpoints:

- `GET /health`
- `GET /v1/models` (`?free=1`, `?provider=opencode`, `?refresh=1`)
- `POST /v1/chat/completions` (streaming supported)
- `GET /v1/urai/stats` · `/v1/urai/keys` (masked) · `/v1/urai/providers`
- `GET /v1/urai/explain?model=auto`
- `POST /v1/urai/reload` · `/v1/urai/test`

Bind to `127.0.0.1` by default. If you expose the port, set a token:

```bash
urai-router serve --token "$(openssl rand -hex 24)"
```

---

## OpenCode integration

```bash
urai-router setup opencode --set-default
```

This merges the following into `~/.config/opencode/opencode.jsonc` (or `.json`),
preserving existing settings and writing a timestamped backup:

- a custom provider `urai-router` using `@ai-sdk/openai-compatible`
- `options.baseURL = http://127.0.0.1:8787/v1`
- a `models` map generated from every usable/free model (context + output limits,
  ★ marks free rows)
- an `mcp.urai-router` local server entry

Then restart OpenCode. Models appear as
`urai-router/opencode/deepseek-v4.1-flash-free`, `urai-router/auto`, etc.
`--set-default` points `model` and `small_model` at the first free Zen model.

Refresh after adding keys or when Zen changes its free list:

```bash
urai-router sync
```

Project-local config instead of global:

```bash
urai-router setup opencode --project
```

Dry run and diff-friendly export:

```bash
urai-router setup opencode --dry-run
urai-router export opencode
```

---

## Kiro CLI integration

```bash
urai-router setup kiro
```

Writes:

- `~/.kiro/settings/mcp.json` → `mcpServers.urai-router` (stdio, `urai-router mcp`)
- `~/.kiro/agents/urai-router.json` → an agent that includes the MCP server and
  the `urai_*` tools, with `@urai-router` in `tools`

Verify:

```bash
kiro-cli mcp list
kiro-cli chat --agent urai-router
```

Kiro keeps its own model backend; the router is used through the MCP tools
(`urai_chat`, `urai_models`, `urai_health`, ...). That is intentional: Kiro does
not expose a custom OpenAI provider setting.

---

## MCP server

`urai-router mcp` speaks JSON-RPC over stdio (newline-delimited, MCP 2025-06-18,
also compatible with 2025-03-26 / 2024-11-05).

Tools:

| Tool | Purpose |
| --- | --- |
| `urai_chat` | chat completion through the router, key rotation included |
| `urai_models` | merged catalogue, `free_only` supported |
| `urai_keys` | masked health / cooldowns |
| `urai_health` | keys + catalogue + proxy status |
| `urai_stats` | alias for health |
| `urai_explain` | show routing for a model |
| `urai_refresh` | reload keys and catalogues |
| `urai_ensure_proxy` | start the local HTTP proxy and return its URL |
| `urai_check` | probe keys |
| `urai_setup` | generate OpenCode/Kiro/skill config (`dry_run` default true) |

Resources: `urai://config`, `urai://keys`, `urai://providers`, `urai://models`,
`urai://stats`.

MCP client snippet (OpenCode style):

```jsonc
{
  "mcp": {
    "urai-router": {
      "type": "local",
      "command": ["urai-router", "mcp"],
      "enabled": true
    }
  }
}
```

MCP client snippet (Kiro style):

```json
{
  "mcpServers": {
    "urai-router": {
      "command": "urai-router",
      "args": ["mcp"],
      "disabled": false,
      "timeout": 30000
    }
  }
}
```

---

## Skill

`urai-router setup skills` copies the Agent Skill to:

- `~/.config/opencode/skills/urai-router/SKILL.md`
- `~/.agents/skills/urai-router/SKILL.md`
- `~/.claude/skills/urai-router/SKILL.md`

Use `--project` to also write `.opencode/`, `.agents/` and `.claude/` variants.
The skill teaches an agent when to call `urai_*`, how to route free models, and
how to report failover without leaking keys.

---

## CLI reference

```text
urai-router init [--path PATH] [--force]
urai-router serve [--host H] [--port P] [--token T] [--no-detect] [--no-preload]
urai-router mcp
urai-router keys list|check|detect
urai-router models [--free] [--provider P] [--refresh] [--json]
urai-router setup [opencode|kiro|skills|all] [--project] [--dry-run] [--set-default] [--start]
urai-router sync [--set-default]
urai-router doctor [--json]
urai-router test [--model M] [--prompt TEXT] [--json]
urai-router explain [--model M]
urai-router export [opencode|kiro|env|mcp|providers]
urai-router proxy start|status|stop
urai-router version
```

Global flags: `--keys`, `--config`, `--state-dir`, `-v`.

---

## Configuration

`~/.config/urai-router/config.json` (created on demand):

```json
{
  "host": "127.0.0.1",
  "port": 8787,
  "token": "",
  "max_attempts": 8,
  "default_model": "auto",
  "prefer_free": true,
  "catalog_ttl": 900,
  "zen_ttl": 300,
  "request_timeout": 120,
  "cooldowns": {
    "rate": 60,
    "invalid": 1800,
    "server": 20,
    "network": 15,
    "max": 3600
  },
  "aliases": {
    "fast": "opencode/deepseek-v4.1-flash-free"
  },
  "providers": {},
  "proxy": { "cors": true, "log_requests": true }
}
```

State and cache:

- `~/.local/state/urai-router/keys-state.json` — health, cooldowns (no secrets)
- `~/.local/state/urai-router/proxy.json` — pid, port, url
- `~/.local/state/urai-router/proxy.log`
- `~/.cache/urai-router/catalog.json` — model metadata cache

---

## Architecture

```text
keys.txt ──▶ KeyStore ──▶ Router ──▶ Provider adapter ──▶ upstream
                │           │                              │
                │           ├─ model resolution            ├─ OpenAI chat/completions
                │           ├─ retry / cooldown            └─ Anthropic messages (+SSE)
                │           └─ free-first auto
                ▼
      proxy (:8787/v1)   MCP stdio   OpenCode config   Kiro config   SKILL.md
```

- `urai_router/providers.py` — registry, catalogue parsing, request building
- `urai_router/keys.py` — parsing, rotation, cooldowns, auto-detection, state
- `urai_router/router.py` — model resolution, failover, streaming
- `urai_router/proxy.py` — HTTP server + background process management
- `urai_router/mcp_server.py` — MCP tools/resources/prompts
- `urai_router/config_writer.py` — OpenCode/Kiro/skill writers
- `urai_router/adapters.py` — OpenAI ↔ Anthropic translation

---

## Security

- Keys are read from a local file and used only in `Authorization` / `x-api-key`
  headers to the provider you configured.
- Logs and API responses only contain masked keys or labels.
- `keys-state.json` and `catalog.json` never contain key material.
- `.gitignore` excludes `keys.txt`, `.env`, and state.
- The proxy binds to loopback by default; add `--token` if you change that.
- `doctor` warns when the keys file is group/world-readable.

Report vulnerabilities privately; see `SECURITY.md`.

---

## GitHub Actions

- **CI** (`.github/workflows/ci.yml`): compile, unit tests against a local fake
  upstream, package build, zipapp smoke test. No network keys required.
- **Release** (`.github/workflows/release.yml`): on a `v*` tag, builds sdist,
  wheel and `urai-router.pyz`, then creates a GitHub release with `GH_TOKEN`
  (the token supplied by GitHub Actions). Manual `workflow_dispatch` supported.

Create the repo and push (what was used here):

```bash
gh repo create etside/Urai-router --public --source=. --remote=origin --push
```

---

## Troubleshooting

| Symptom | Fix |
| --- | --- |
| `no usable route` | add keys, then `urai-router keys list` and `doctor` |
| models empty | `urai-router models --refresh`; check network |
| OpenCode shows no models | `urai-router sync`, restart OpenCode |
| all keys cooling | add keys or wait; check `urai-router keys list` |
| 401 from every key | `urai-router keys check`, fix/remove invalid keys |
| Zen free list changed | `urai-router sync` |
| Kiro doesn't see MCP | `kiro-cli mcp list`; re-run `setup kiro` |
| proxy not reachable | `urai-router proxy status`; see `proxy.log` |

---

## License

MIT. See `LICENSE`.
