# Security policy

## Key handling

Urai Router reads API keys from a local text file (`keys.txt`) and sends them
only to the provider endpoints configured for that provider. It does not upload,
sync, or persist keys in its state or cache files.

- `keys-state.json` stores fingerprints, counters and cooldowns, never the key.
- API responses (`/v1/urai/keys`, MCP `urai_keys`) return masked values only.
- Logs never include key material.
- `doctor` warns if the keys file is readable by other users.
- The HTTP proxy binds to `127.0.0.1` by default; use `--token` if you must
  expose it beyond loopback.

Recommended permissions:

```bash
chmod 600 ~/.config/urai-router/keys.txt
chmod 700 ~/.config/urai-router
```

## Reporting

Please open a private security advisory on GitHub rather than a public issue for
anything that could leak keys or allow remote code execution. Include the
version (`urai-router version`), platform, and a minimal reproduction with all
secrets redacted.

## Scope

This is a local developer tool. It is not a multi-tenant gateway: there is no
user database, billing, or per-tenant isolation. Do not expose it to the public
internet without a reverse proxy, TLS, and an auth token.
