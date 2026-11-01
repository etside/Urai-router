# Audit: existing open-source API-key routers (November 2026)

Before writing Urai Router, the landscape was checked with the GitHub CLI
(`gh search repos`, `gh repo view`) and by reading the provider/agent docs.
This is a snapshot; stars and licences change.

## Requirements used for the audit

1. Read keys from a plain text file.
2. Rotate keys automatically per request (`401/403/429/5xx`).
3. Understand **OpenCode Zen** free models and their per-model API style.
4. Be usable as **MCP** and as an **Agent Skill** in OpenCode and Kiro CLI.
5. Generate provider/model configuration automatically for those CLIs.
6. Stay lightweight (no database, no Node/Python dependency tree).

## Results

| Project | Stars | Licence | Stack | Scope | Gap vs requirements |
| --- | ---: | --- | --- | --- | --- |
| [BerriAI/litellm](https://github.com/BerriAI/litellm) | 62,455 | Other (MIT core + enterprise) | Python | Full gateway, 100+ providers, budgets, guardrails | Excellent but large; Postgres/Redis for teams; no OpenCode/Kiro auto-config; not a `keys.txt` CLI tool |
| [QuantumNous/new-api](https://github.com/QuantumNous/new-api) | 51,102 | AGPL-3.0 | Go | Key redistribution, quotas, web UI | AGPL; DB + admin server; resale-oriented; no MCP/skill |
| [songquanpeng/one-api](https://github.com/songquanpeng/one-api) | 37,300 | MIT | Go/JS | Unified provider API + key management | Web UI + DB; no automatic per-request rotation from a text file; no MCP/skill |
| [musistudio/claude-code-router](https://github.com/musistudio/claude-code-router) | 37,928 | MIT | TypeScript | Route Claude Code requests to many providers | Claude-shaped; Node runtime; local control plane, not MCP-native |
| [jasonxu114514/opencode2api](https://github.com/jasonxu114514/opencode2api) | 1,376 | GPL-3.0 | Go | OpenCode Zen/Go → OpenAI/Anthropic proxy; key pooling | Zen-only; GPL; no generic keys.txt; no MCP/skill/config writers |
| [routatic/proxy](https://github.com/routatic/proxy) | 983 | AGPL-3.0 | Go | Multi-upstream Claude/OpenCode proxy | AGPL; no MCP; no OpenCode/Kiro config generation |
| [6Kmfi6HP/opencode2api](https://github.com/6Kmfi6HP/opencode2api) | 147 | — | TypeScript | OpenCode Zen → Codex/Claude proxy | Narrow, single purpose |
| [s-berdia/Opencode_router](https://github.com/s-berdia/Opencode_router) | 0 | MIT | TypeScript | Early automatic model routing/quota failover for OpenCode | Immature; OpenCode-only; no MCP/keys.txt |

Adjacent tools were also reviewed:

- `cc-mirror` (2.2k★, MIT) isolates Claude Code profiles; not a key pool.
- `openclaw-billing-proxy` (517★) changes billing path, not key rotation.
- `openclaw-plugin-jules-skills` is the only "multi-key rotation + MCP bridge"
  hit, but it is a plugin for Jules issues, not an LLM gateway.
- `mcp-server-openrouter`, LiteLLM MCP bridges and similar expose **one**
  provider; they do not pool keys or generate OpenCode/Kiro config.

## What was missing

No project combined:

- a tiny, auditable **single-file key list**;
- **automatic provider detection** from key prefixes and a 1-token probe;
- **OpenCode Zen catalogue awareness**, including the per-model
  `api.chat-completions` vs `api.messages` split;
- **MCP-native tools** (`urai_chat`, `urai_models`, `urai_health`, ...);
- **writers** for OpenCode `provider` + `mcp` and Kiro `mcpServers` + agent;
- an **Agent Skill** that tells the agent when and how to route.

The closest architectural fit was `claude-code-router` (local control plane) and
`litellm` (provider normalisation), but both are much larger and neither targets
OpenCode/Kiro configuration.

## Decision

Build **Urai Router** as a new MIT project:

- Python 3.9+ standard library only (no runtime dependencies).
- OpenAI-compatible surface + Anthropic adapter (tools and SSE included).
- MCP stdio server, resources and prompts.
- Config writers that merge into existing files and keep backups.
- GitHub Actions for CI and release builds (no keys in CI).

## Sources checked

- OpenCode docs: providers, MCP servers, Agent Skills, Zen pricing/models.
- `https://opencode.ai/zen/v1/models` (public JSON; pricing + `api` metadata).
- Kiro CLI 2.34.1: `kiro-cli mcp add`, `kiro-cli agent`, agent config example.
- GitHub CLI repository search/view for the projects above.

The audit is intentionally conservative: where a project is AGPL/GPL, it was
not copied or vendored. Urai Router is an independent MIT implementation.
