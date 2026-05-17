# Personal KIS deployment

This deployment keeps the two web apps on separate internal origins and exposes
only the Caddy listeners on the host Tailscale interface.

## Files

- `compose.yml`
- `Caddyfile`
- `.env.production`
- `scripts/windows/bootstrap-kis-stack.ps1`

## One-time host setup

1. Create `C:\kis-stack\config` and `C:\kis-stack\logs`.
2. Put the KIS runtime config files under `C:\kis-stack\config`.
3. Copy `.env.production.example` to `.env.production`.
4. Generate the Basic Auth hash:

   ```powershell
   docker run --rm caddy:2.10-alpine caddy hash-password --plaintext "replace-me"
   ```

5. Fill `CADDY_AUTH_USER` and `CADDY_AUTH_HASH` in `.env.production`.
   Keep the hash single-quoted so Docker Compose passes each `$` through unchanged.
6. Set `CADDY_BIND_IP` in `.env.production` to this Windows machine's Tailscale
   IPv4 address, for example `100.x.y.z`.
7. Pull the Lean image once before first use:

   ```powershell
   docker pull quantconnect/lean:latest
   ```

## Start the stack

```powershell
docker compose --env-file .env.production up -d --build
```

Only Caddy is published on the host, and both listeners are bound to the
Tailscale interface:

- `${CADDY_BIND_IP}:8081` for Strategy Builder
- `${CADDY_BIND_IP}:8082` for Backtester

The four app containers stay on the internal Compose network.

Use the Tailscale device DNS name or IP plus port from other tailnet devices:

- `http://your-device.your-tailnet.ts.net:8081`
- `http://your-device.your-tailnet.ts.net:8082`
- `http://100.x.y.z:8081`
- `http://100.x.y.z:8082`

## Windows startup

Use a dedicated Windows account that can auto-login after reboot, then register
`scripts/windows/bootstrap-kis-stack.ps1` in Task Scheduler for startup or
logon. The script starts Docker Desktop, waits until it reports `running`,
and starts Compose.

When the repository is kept inside WSL, run the script from its checked-in
location or pass the WSL UNC path as `-RepoRoot`. The script resolves the repo
root from its own location by default.

Tailscale Services are optional and are disabled by default. Do not pass
`-EnableTailscaleServices` unless this Windows machine is already configured as
a tagged node permitted by the tailnet ACL. For normal operation, use the
Tailscale device DNS/IP plus `:8081` or `:8082` instead.

## Verification

```powershell
docker compose --env-file .env.production ps
curl.exe -I "http://$($env:CADDY_BIND_IP):8081"
curl.exe -u "your_username:plain-password" "http://$($env:CADDY_BIND_IP):8081/api/health"
curl.exe -u "your_username:plain-password" "http://$($env:CADDY_BIND_IP):8082/api/health"
```

Expected behavior:

- unauthenticated requests return `401`
- authenticated requests reach the intended backend
- Strategy Builder order actions append JSONL files under `C:\kis-stack\logs\orders`
- Backtester uses the named `backtester_workspace` volume for Lean data and results
