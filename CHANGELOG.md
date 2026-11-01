# Changelog

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
