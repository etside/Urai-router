# Changelog

## Unreleased — FreeLLMAPI free-token gateway

- New `freellmapi` provider: one unified `freellmapi-…` key in front of ~87
  free LLM providers, OpenAI-compatible at `http://127.0.0.1:3001/v1`.
- Automatic `freellmapi-` key detection and `${FREELLMAPI_KEY}` expansion.
- Live catalogue discovery via `/v1/models?execution_status=ready`, with a
  short 120 s TTL so `ready` / `needsKey` / `exhausted` stays current.
- `auto`, `auto:fast`, `auto:smart`, `auto:reliable`, `auto:balanced`,
  `auto:cheap` and named `auto:<profile>` chains are marked free and ranked
  first, so `model=auto` sends one request to `freellmapi/auto` instead of
  spraying individual gateway models.
- Monthly token/request accounting through `GET /v1/usage`:
  `urai-router keys balance --provider freellmapi [--json]`.
- Configurable gateway: `FREELLMAPI_BASE_URL`, `FREELLMAPI_MODELS_QUERY`,
  `freellmapi.base_url`, `freellmapi.models_query`, plus the usual
  `providers.freellmapi.*` overrides.
- Fixed global `--keys` / `--config` / `--state-dir` being swallowed by
  subcommand defaults.

## 0.1.0 — 2026-11-01

Initial release.

- `keys.txt` parser: `provider:key`, bare keys, `${ENV}`, labels and weights.
- Automatic provider detection by prefix and 1-token probe.
- Weighted round-robin key selection with persistent cooldowns and strikes.
- OpenAI-compatible proxy (`/v1/chat/completions`, `/v1/models`) with SSE.
- Anthropic Messages adapter (tools + SSE) for Anthropic and Zen Claude models.
- OpenCode Zen catalogue parsing: free detection and per-model endpoints.
- MCP stdio server with 10 tools, 5 resources and a prompt.
- OpenCode config writer (provider + MCP + generated model map).
- Kiro config writer (`mcp.json` + agent profile).
- Agent Skill installation for OpenCode, `.agents` and `.claude`.
- `doctor`, `keys check/detect`, `models`, `test`, `explain`, `export`,
  `sync`, `proxy start/status/stop`.
- GitHub Actions CI and tag-driven release with `gh`.
