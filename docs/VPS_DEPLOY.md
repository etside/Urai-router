# Live deployment: `wearimpressive-vps` (Tailscale)

The production instance runs on the operator's VPS, reached over Tailscale as
`wearimpressive-vps` (`100.124.118.90`, user `reeha`). The directory is a normal
git checkout of this repository, so upgrades are a `git pull` plus a restart.

## Layout

| What | Where |
| --- | --- |
| `urai-router` checkout | `/home/reeha/urai-router` |
| `urai-router` process | pm2 `urai-router` → `python3 -m urai_router serve --port 8792` |
| `urai-router` keys | `/home/reeha/.config/urai-router/keys.txt` (mode `0600`) |
| `urai-router` config | `/home/reeha/.config/urai-router/config.json` |
| FreeLLMAPI checkout | `/home/reeha/freellmapi` |
| FreeLLMAPI process | pm2 `freellmapi` → `node server/dist/index.js`, bound to `127.0.0.1:3011` |
| FreeLLMAPI data | `/home/reeha/freellmapi/server/data/freeapi.db` (SQLite) |
| FreeLLMAPI env | `/home/reeha/freellmapi/.env` (`ENCRYPTION_KEY`, `PORT=3011`) |
| Dashboard login | `/home/reeha/.config/freellmapi/dashboard.txt` (mode `0600`) |

Port `3001`, FreeLLMAPI's default, is already used on this host by another
service, so the gateway listens on `3011`. `urai-router` points at it through
`freellmapi.base_url` in `config.json`:

```json
{
  "freellmapi": {
    "base_url": "http://127.0.0.1:3011/v1"
  }
}
```

The unified `freellmapi-…` key lives in `keys.txt` like any other provider key;
the `freellmapi-` prefix is detected automatically.

## Upgrading urai-router

```bash
ssh reeha@100.124.118.90        # Tailscale
cd ~/urai-router
git pull --ff-only origin main
pm2 restart urai-router --update-env
curl -s http://127.0.0.1:8792/health
```

No `pip install` is needed: the pm2 process runs `python3 -m urai_router` from
the checkout itself.

## Upgrading FreeLLMAPI

```bash
cd ~/freellmapi
git pull --ff-only
npm install --no-audit --no-fund
npm run build -w server          # npm run build -w client for the dashboard UI
pm2 restart freellmapi
```

## Verification

```bash
cd ~/urai-router
python3 -m urai_router doctor
python3 -m urai_router models --provider freellmapi --refresh
python3 -m urai_router explain --model auto          # freellmapi/auto first
python3 -m urai_router test --model freellmapi/auto
python3 -m urai_router test --model auto
python3 -m urai_router keys balance --provider freellmapi
```

`keys balance` reads `GET /v1/usage` on the gateway and prints the monthly
request/token ledger for the unified key.

## Notes

- pm2 processes are saved with `pm2 save` and restart on boot via the enabled
  `pm2-reeha` systemd unit.
- The gateway binds to loopback only; it is not exposed on the public IP.
- Never copy `keys.txt`, `.env`, or `dashboard.txt` into the repository.
