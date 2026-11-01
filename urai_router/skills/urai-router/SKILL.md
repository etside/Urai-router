---
name: urai-router
description: Route LLM requests across every API key in keys.txt, fail over on 401/403/429/5xx, discover free OpenCode Zen models, and expose the whole pool as an OpenAI-compatible endpoint or MCP tools. Use when a task needs model selection, key rotation, free-model discovery, provider failover, or when the user mentions Urai Router, OpenCode Zen free models, or key pooling.
license: MIT
compatibility: opencode, kiro
metadata:
  audience: developers
  workflow: llm-routing
---

# Urai Router

Urai Router turns a plain `keys.txt` into a working multi-provider LLM gateway.
It never prints full keys, never commits them, and gives every client one
OpenAI-compatible API (`http://127.0.0.1:8787/v1`).

## When to use this skill

- The user asks to use free OpenCode Zen models or any free model.
- A request failed with 401/403/429/5xx and another key/provider may work.
- The user wants one endpoint for OpenCode, Kiro, scripts, or another CLI.
- The user wants to know which providers/models their keys unlock.
- The user wants to add or validate keys.

## First steps

Always inspect state before changing anything:

```bash
urai-router doctor
urai-router keys list
urai-router models --free
```

Keys live in `~/.config/urai-router/keys.txt` (or `$URAI_KEYS_FILE`). One key
per line, `provider:key`; a bare key is auto-detected:

```text
opencode:sk-...
openrouter:sk-or-v1-...
groq:gsk_...
deepseek:sk-...
anthropic:sk-ant-...
google:AIza...
```

## Use the MCP tools

Prefer MCP tools when they are available:

| Tool | Purpose |
| --- | --- |
| `urai_chat` | One chat completion through the router; rotates keys automatically |
| `urai_models` | List merged models; `free_only: true` shows free Zen rows |
| `urai_keys` | Masked key health, cooldowns, counts |
| `urai_health` | Router + proxy + catalogue status |
| `urai_explain` | Show which provider/key pool a model would use |
| `urai_refresh` | Reload keys and catalogues |
| `urai_ensure_proxy` | Start the local OpenAI-compatible proxy |
| `urai_check` | Probe keys for validity |
| `urai_setup` | Generate OpenCode/Kiro/skill config (`dry_run` defaults true) |

Call `urai_chat` with `model: "auto"` to prefer free models, or
`model: "opencode/deepseek-v4.1-flash-free"` for an explicit route.
The response metadata includes `provider`, `model`, `key` (label only) and
`attempts`, which is enough to explain failover without exposing secrets.

## Use the HTTP endpoint

```bash
urai-router serve            # foreground, 127.0.0.1:8787
# or
urai-router proxy start      # background + pid/port in ~/.local/state/urai-router

curl -s http://127.0.0.1:8787/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"auto","messages":[{"role":"user","content":"ping"}]}'
```

OpenAI SDKs only need:

```bash
export OPENAI_BASE_URL=http://127.0.0.1:8787/v1
export OPENAI_API_KEY=urai-local     # or the configured router token
```

## Model syntax

- `auto` or `best` - free models first, then any usable model.
- `free` / `urai/free` - free models only.
- `opencode/<id>` - OpenCode Zen (free rows end in `-free`).
- `openrouter/<id>` - OpenRouter.
- `<id>` - resolved across the catalogue when unambiguous.
- Aliases from config: `"aliases": {"fast": "opencode/deepseek-v4.1-flash-free"}`.

## Configure clients

```bash
urai-router setup all --start          # OpenCode + Kiro + skill, start proxy
urai-router setup opencode --set-default
urai-router setup kiro
urai-router setup skills --project     # project-local .opencode/.agents/.claude
urai-router sync                       # refresh model list after keys change
```

- OpenCode gets a custom `urai-router` provider plus an MCP server entry.
- Kiro gets `~/.kiro/settings/mcp.json` and `~/.kiro/agents/urai-router.json`.
- Skills are copied to `~/.config/opencode/skills/`, `~/.agents/skills/`,
  and `~/.claude/skills/`.

## Failover rules

- `401/403` - mark key invalid, rotate immediately.
- `429` - honour `Retry-After`, exponential cooldown, rotate.
- `5xx/408/529` - short exponential cooldown, rotate.
- `400/422` - request/model problem, do not blame the key.
- Cooldowns persist in `~/.local/state/urai-router/keys-state.json`.
- `keys.txt` is hot-reloaded when its mtime changes.

## Safety

- Never echo a full key; use `urai-router keys list` (masked).
- Do not commit `keys.txt`; keep mode `600`.
- Keep the proxy on `127.0.0.1`. If exposed, set a token:
  `urai-router serve --token "$(openssl rand -hex 24)"`.
- Treat upstream errors as data; do not paste key material into prompts.

## Troubleshooting

```bash
urai-router doctor --json
urai-router keys detect          # identify unlabelled keys
urai-router keys check           # 1-token probe per key
urai-router test --model auto
urai-router models --refresh
```

If OpenCode does not show models, run `urai-router sync` and restart OpenCode.
If Kiro does not see the server, run `kiro-cli mcp list` and check
`~/.kiro/settings/mcp.json`.
